"""Supabase(PostgREST) 저장 계층.

수집 로직과 분리해 둔 이유:
  - 나중에 Vercel 대시보드를 붙일 때 읽기 전용 접근을 여기에 얹기 쉽게 하려고
  - 수집 스크립트를 손대지 않고 저장 방식만 바꿀 수 있게 하려고

supabase-py 대신 requests 로 PostgREST 를 직접 호출합니다.
의존성이 requests 하나뿐이라 GitHub Actions 에서 설치가 빠르고 깨질 일이 적습니다.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import quote

import requests

from .config import Config

log = logging.getLogger(__name__)

BATCH_SIZE = 500          # 한 번의 요청에 보낼 최대 행 수
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 524}


class DbError(Exception):
    pass


def _chunks(rows: list, size: int) -> Iterable[list]:
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


class SupabaseDB:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.supabase_url + "/rest/v1"
        self.session = requests.Session()
        self.session.headers.update({
            "apikey": cfg.supabase_key,
            "Authorization": "Bearer " + cfg.supabase_key,
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, *, prefer: str | None = None,
                 json: Any = None, label: str = "") -> requests.Response:
        headers = {"Prefer": prefer} if prefer else None
        last_error = ""

        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                resp = self.session.request(
                    method, self.base + path, headers=headers, json=json,
                    timeout=self.cfg.request_timeout * 3,  # 대량 쓰기는 여유를 더 준다
                )
            except requests.RequestException as e:
                last_error = f"네트워크 오류: {e}"
                log.warning("  DB %s %s - %d/%d회차",
                            label or path, last_error, attempt, self.cfg.max_retries)
                time.sleep(min(2 ** attempt, 30))
                continue

            if resp.status_code < 300:
                return resp

            if resp.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {resp.status_code}"
                log.warning("  DB %s %s - %d/%d회차",
                            label or path, last_error, attempt, self.cfg.max_retries)
                time.sleep(min(2 ** attempt, 30))
                continue

            raise DbError(
                f"DB {label or path} 실패 (HTTP {resp.status_code}): {resp.text[:400]}"
            )

        raise DbError(f"DB {label or path} {self.cfg.max_retries}회 재시도 실패 - {last_error}")

    def _upsert(self, table: str, rows: list[dict], on_conflict: str,
                returning: bool = False) -> list[dict]:
        """중복이면 갱신하는 일괄 저장. 500행씩 나눠 보낸다."""
        if not rows:
            return []
        prefer = "resolution=merge-duplicates,return=" + ("representation" if returning else "minimal")
        out: list[dict] = []
        for chunk in _chunks(rows, BATCH_SIZE):
            resp = self._request(
                "POST", f"/{table}?on_conflict={on_conflict}",
                prefer=prefer, json=chunk, label=f"{table} upsert",
            )
            if returning and resp.text:
                out.extend(resp.json())
        return out

    # ------------------------------------------------------------------
    # 수집 회차 로그
    # ------------------------------------------------------------------
    def start_run(self) -> int:
        resp = self._request(
            "POST", "/collection_run", prefer="return=representation",
            json=[{"status": "running"}], label="collection_run 시작",
        )
        return resp.json()[0]["run_id"]

    def finish_run(self, run_id: int, **fields) -> None:
        # 로그 기록 실패가 수집 전체를 실패로 만들지 않도록 감싼다
        try:
            self._request(
                "PATCH", f"/collection_run?run_id=eq.{run_id}",
                prefer="return=minimal", json=fields, label="collection_run 종료",
            )
        except DbError as e:
            log.error("수집 로그 기록 실패(수집 자체는 영향 없음): %s", e)

    # ------------------------------------------------------------------
    # 카테고리 사전
    # ------------------------------------------------------------------
    def load_category_map(self) -> dict[tuple[str, str, str], int]:
        """(type, category, value) -> category_id 매핑을 통째로 읽어온다."""
        resp = self._request(
            "GET",
            "/category?select=category_id,category_type,live_category,live_category_value"
            "&limit=100000",
            label="category 조회",
        )
        return {
            (r["category_type"], r["live_category"], r["live_category_value"]): r["category_id"]
            for r in resp.json()
        }

    def ensure_categories(self, keys: set[tuple[str, str, str]]) -> dict[tuple[str, str, str], int]:
        """처음 보는 카테고리만 새로 등록하고, 전체 매핑을 돌려준다."""
        mapping = self.load_category_map()
        missing = sorted(keys - set(mapping))
        if not missing:
            return mapping

        log.info("새 카테고리 %d개 등록: %s",
                 len(missing), ", ".join(m[2] or "(미분류)" for m in missing[:5]))
        rows = [
            {"category_type": t, "live_category": c, "live_category_value": v}
            for (t, c, v) in missing
        ]
        created = self._upsert(
            "category", rows,
            on_conflict="category_type,live_category,live_category_value",
            returning=True,
        )
        for r in created:
            mapping[(r["category_type"], r["live_category"], r["live_category_value"])] = \
                r["category_id"]
        return mapping

    # ------------------------------------------------------------------
    # 본 데이터
    # ------------------------------------------------------------------
    def upsert_channels(self, rows: list[dict]) -> int:
        self._upsert("channel", rows, on_conflict="channel_id")
        return len(rows)

    def upsert_live_sessions(self, rows: list[dict]) -> int:
        self._upsert("live_session", rows, on_conflict="live_id")
        return len(rows)

    def insert_snapshots(self, rows: list[dict]) -> int:
        # on_conflict 를 걸어 두면 같은 회차를 다시 돌려도 중복 에러가 나지 않는다
        self._upsert("live_snapshot", rows, on_conflict="live_id,collected_at")
        return len(rows)

    # ------------------------------------------------------------------
    def categories_missing_poster(self, limit: int) -> list[dict]:
        """포스터 이미지가 아직 없는 카테고리를 오래된 것부터 가져온다.

        한 번 찾지 못한 카테고리는 poster_checked_at 이 기록되므로,
        7일이 지나기 전에는 다시 조회하지 않는다.
        """
        if limit <= 0:
            return []
        # 시각의 '+09:00' 같은 부분이 쿼리스트링에서 공백으로 해석되지 않도록 인코딩한다
        cutoff = quote(
            (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(), safe="")
        resp = self._request(
            "GET",
            "/category?select=category_id,live_category,live_category_value"
            "&poster_image_url=is.null"
            "&live_category=neq."
            "&or=(poster_checked_at.is.null,poster_checked_at.lt.{})"
            "&order=poster_checked_at.nullsfirst&limit={}".format(cutoff, limit),
            label="포스터 대상 조회",
        )
        return resp.json()

    def update_category_poster(self, category_id: int, url: str | None) -> None:
        self._request(
            "PATCH", "/category?category_id=eq.{}".format(category_id),
            prefer="return=minimal",
            json={
                "poster_image_url": url,
                "poster_checked_at": datetime.now(timezone.utc).isoformat(),
            },
            label="포스터 저장",
        )

    # ------------------------------------------------------------------
    def run_rollup(self) -> dict:
        """집계 갱신 + 보존기간 지난 원본 삭제."""
        resp = self._request(
            "POST", "/rpc/run_rollup",
            json={"p_retention_days": self.cfg.retention_days},
            label="run_rollup",
        )
        data = resp.json()
        return data[0] if isinstance(data, list) and data else data
