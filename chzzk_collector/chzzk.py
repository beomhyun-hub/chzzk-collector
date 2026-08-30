"""치지직 Open API 클라이언트.

- GET /open/v1/lives 를 next 커서로 끝까지 순회
- 429 / 5xx / 타임아웃은 지수 백오프로 재시도
- 재시도해도 실패하면 "거기까지 모은 것"을 돌려주고 partial 로 표시
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import Config

log = logging.getLogger(__name__)

BASE_URL = "https://openapi.chzzk.naver.com"
LIVES_PATH = "/open/v1/lives"
CHANNELS_PATH = "/open/v1/channels"
CATEGORY_SEARCH_PATH = "/open/v1/categories/search"

# 재시도할 상태코드 (429=한도초과, 5xx=서버 문제). 4xx 는 재시도해도 소용없음.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ChzzkError(Exception):
    """재시도해도 복구되지 않는 오류."""


@dataclass
class FetchResult:
    lives: list[dict[str, Any]] = field(default_factory=list)
    pages_fetched: int = 0
    complete: bool = False          # 컷오프나 목록 끝까지 정상 도달했는가
    stop_reason: str = ""
    error: str | None = None
    last_page_min_ccu: int | None = None


class ChzzkClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "Client-Id": cfg.chzzk_client_id,
            "Client-Secret": cfg.chzzk_client_secret,
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------------
    def _get(self, path: str, params: dict) -> dict:
        """재시도가 붙은 GET. content 부분만 돌려준다."""
        last_error = ""

        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                resp = self.session.get(
                    BASE_URL + path, params=params, timeout=self.cfg.request_timeout
                )
            except requests.Timeout:
                last_error = f"타임아웃({self.cfg.request_timeout}초)"
                log.warning("  %s - %d/%d회차", last_error, attempt, self.cfg.max_retries)
                self._backoff(attempt)
                continue
            except requests.RequestException as e:
                last_error = f"네트워크 오류: {e}"
                log.warning("  %s - %d/%d회차", last_error, attempt, self.cfg.max_retries)
                self._backoff(attempt)
                continue

            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError:
                    raise ChzzkError(f"JSON 이 아닌 응답: {resp.text[:200]}")
                content = body.get("content")
                if content is None:
                    raise ChzzkError(f"응답에 content 가 없음: {str(body)[:200]}")
                return content

            # 인증/권한 문제는 재시도해도 절대 안 풀리므로 즉시 중단
            if resp.status_code in (401, 403):
                raise ChzzkError(
                    f"HTTP {resp.status_code} 인증 실패. "
                    f"Client-Id/Secret 을 확인하세요. 응답: {resp.text[:200]}"
                )

            if resp.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {resp.status_code}"
                # 429 면 서버가 알려준 대기시간을 우선 존중
                wait = self._retry_after(resp)
                log.warning("  %s - %d/%d회차, %.1f초 대기",
                            last_error, attempt, self.cfg.max_retries, wait)
                time.sleep(wait)
                continue

            raise ChzzkError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        raise ChzzkError(f"{self.cfg.max_retries}회 재시도 실패 - {last_error}")

    def _backoff(self, attempt: int) -> None:
        time.sleep(min(2 ** attempt, 30))

    def _retry_after(self, resp) -> float:
        raw = resp.headers.get("Retry-After")
        if raw:
            try:
                return min(float(raw), 60.0)
            except ValueError:
                pass
        return 5.0

    # ------------------------------------------------------------------
    def fetch_lives(self) -> FetchResult:
        """동접 컷오프에 도달할 때까지 라이브 목록 전체를 가져온다."""
        cfg = self.cfg
        result = FetchResult()
        seen_live_ids: set[int] = set()
        cursor: str | None = None

        log.info("라이브 목록 수집 시작 - %s", cfg.summary())

        while result.pages_fetched < cfg.max_pages:
            params: dict[str, Any] = {"size": cfg.page_size}
            if cursor:
                params["next"] = cursor

            try:
                content = self._get(LIVES_PATH, params)
            except ChzzkError as e:
                # 여기까지 모은 건 그대로 저장한다 (부분 성공)
                result.error = str(e)
                result.stop_reason = f"{result.pages_fetched + 1}페이지에서 실패"
                log.error("페이지 수집 중단: %s", e)
                return result

            data = content.get("data") or []
            result.pages_fetched += 1

            if not data:
                result.complete = True
                result.stop_reason = "목록 끝 (빈 페이지)"
                break

            # 페이지를 넘기는 사이 목록이 밀려서 같은 방송이 두 번 나올 수 있다.
            # DB 기본키가 (live_id, collected_at) 이므로 여기서 걸러야 한다.
            page_min_ccu = None
            for item in data:
                live_id = item.get("liveId")
                if live_id is None or live_id in seen_live_ids:
                    continue
                seen_live_ids.add(live_id)
                result.lives.append(item)
                ccu = item.get("concurrentUserCount") or 0
                page_min_ccu = ccu if page_min_ccu is None else min(page_min_ccu, ccu)

            if page_min_ccu is not None:
                result.last_page_min_ccu = page_min_ccu

            if result.pages_fetched % 20 == 0:
                log.info("  %d페이지 / 누적 %d건 / 현재 동접 %s명",
                         result.pages_fetched, len(result.lives), page_min_ccu)

            # 목록이 동접 내림차순이므로, 컷오프 아래로 내려가면 더 볼 필요가 없다
            if page_min_ccu is not None and page_min_ccu < cfg.min_concurrent_users:
                result.complete = True
                result.stop_reason = f"동접 {cfg.min_concurrent_users}명 미만 도달"
                break

            cursor = (content.get("page") or {}).get("next")
            if not cursor:
                result.complete = True
                result.stop_reason = "목록 끝 (커서 없음)"
                break

            time.sleep(cfg.page_delay_seconds)

        else:
            result.complete = True
            result.stop_reason = f"최대 페이지({cfg.max_pages}) 도달"
            log.warning("최대 페이지 상한에 걸렸습니다. MAX_PAGES 를 늘려야 할 수 있습니다.")

        log.info("수집 완료 - %d페이지 / %d건 / 사유: %s",
                 result.pages_fetched, len(result.lives), result.stop_reason)
        return result

    # ------------------------------------------------------------------
    def find_category_poster(self, name: str, category_id: str) -> str | None:
        """카테고리 이름으로 검색해 포스터 이미지 URL 을 찾는다.

        lives 응답의 liveCategory 가 이 API 의 categoryId 와 같다는 것을 확인했으므로
        (scripts/test_category_api.py), 검색 결과 중 id 가 일치하는 것만 채택한다.
        이름이 비슷한 다른 카테고리의 이미지를 잘못 가져오지 않기 위해서다.
        """
        if not name or not category_id:
            return None
        try:
            content = self._get(CATEGORY_SEARCH_PATH, {"query": name, "size": 10})
        except ChzzkError as e:
            log.warning("  카테고리 '%s' 검색 실패: %s", name, e)
            return None

        for item in content.get("data") or []:
            if item.get("categoryId") == category_id:
                return item.get("posterImageUrl") or None
        return None

    # ------------------------------------------------------------------
    def fetch_channels(self, channel_ids: list[str]) -> list[dict[str, Any]]:
        """채널 정보(팔로워 수 포함) 조회. 지금은 호출하지 않고, 나중에 붙일 자리.

        GET /open/v1/channels?channelIds=A&channelIds=B  (한 번에 최대 20개)
        """
        out: list[dict[str, Any]] = []
        for i in range(0, len(channel_ids), 20):
            chunk = channel_ids[i:i + 20]
            content = self._get(CHANNELS_PATH, {"channelIds": chunk})
            out.extend(content.get("data") or [])
            time.sleep(self.cfg.page_delay_seconds)
        return out
