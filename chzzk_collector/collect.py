"""수집 실행 진입점.

    python -m chzzk_collector.collect

동작 순서
    1. collection_run 에 '시작' 기록
    2. 치지직 API 를 동접 컷오프까지 순회
    3. 채널 / 카테고리 / 방송 / 스냅샷으로 정리해 일괄 저장
    4. 집계 갱신 + 보존기간 지난 원본 삭제
    5. collection_run 에 성공/부분성공/실패와 저장 건수 기록

종료 코드
    0 = 성공 또는 부분 성공(다음 회차에 자연히 복구됨)
    1 = 실패 (한 건도 저장 못 함)
"""

import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

from .chzzk import ChzzkClient, ChzzkError
from .config import Config, ConfigError
from .db import DbError, SupabaseDB

log = logging.getLogger("collect")

KST_SUFFIX = "+09:00"


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def parse_open_date(raw: str | None) -> str | None:
    """'2026-08-30 16:57:31' (한국시간, 타임존 표기 없음) -> ISO8601 +09:00.

    이걸 안 해주면 UTC 로 해석돼서 방송 시작 시각이 9시간 어긋납니다.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw.endswith("Z") or "+" in raw[10:]:
        return raw  # 이미 타임존이 붙어 있으면 그대로
    return raw.replace(" ", "T") + KST_SUFFIX


def build_rows(
    lives: list[dict[str, Any]],
    collected_at: str,
    category_map: dict[tuple[str, str, str], int],
) -> tuple[list[dict], list[dict], list[dict]]:
    """API 응답을 channel / live_session / live_snapshot 행으로 변환."""
    channels: dict[str, dict] = {}
    sessions: dict[int, dict] = {}
    snapshots: list[dict] = []

    for item in lives:
        channel_id = item.get("channelId")
        live_id = item.get("liveId")
        if not channel_id or live_id is None:
            continue

        # 채널 (같은 채널이 여러 번 나와도 마지막 것 하나만)
        channels[channel_id] = {
            "channel_id": channel_id,
            "channel_name": item.get("channelName") or "",
            "channel_image_url": item.get("channelImageUrl"),
            "last_seen_at": collected_at,
        }

        # 방송
        sessions[live_id] = {
            "live_id": live_id,
            "channel_id": channel_id,
            "live_title": item.get("liveTitle"),
            "open_date": parse_open_date(item.get("openDate")),
            "adult": bool(item.get("adult")),
            "tags": item.get("tags") or [],
            "thumbnail_url": item.get("liveThumbnailImageUrl"),
            "last_seen_at": collected_at,
        }

        # 스냅샷
        key = (
            item.get("categoryType") or "",
            item.get("liveCategory") or "",
            item.get("liveCategoryValue") or "",
        )
        snapshots.append({
            "live_id": live_id,
            "collected_at": collected_at,
            "category_id": category_map.get(key),
            "concurrent_user_count": item.get("concurrentUserCount") or 0,
        })

    return list(channels.values()), list(sessions.values()), snapshots


def main() -> int:
    setup_logging()
    started = time.time()

    try:
        cfg = Config.from_env()
    except ConfigError as e:
        log.error("설정 오류\n%s", e)
        return 1

    db = SupabaseDB(cfg)
    chzzk = ChzzkClient(cfg)

    try:
        run_id = db.start_run()
    except DbError as e:
        log.error("DB 연결 실패 - 수집을 시작할 수 없습니다: %s", e)
        return 1
    log.info("수집 회차 시작 (run_id=%d)", run_id)

    collected_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    status = "failed"
    error_message: str | None = None
    inserted = 0
    channels_n = 0
    fetched = None

    try:
        # --- 1) API 수집 -------------------------------------------------
        fetched = chzzk.fetch_lives()
        if fetched.error:
            error_message = fetched.error

        if not fetched.lives:
            raise ChzzkError(fetched.error or "라이브를 한 건도 가져오지 못했습니다.")

        # --- 2) 카테고리 사전 정리 ---------------------------------------
        keys = {
            (item.get("categoryType") or "",
             item.get("liveCategory") or "",
             item.get("liveCategoryValue") or "")
            for item in fetched.lives
        }
        category_map = db.ensure_categories(keys)

        # --- 3) 변환 + 저장 ----------------------------------------------
        channels, sessions, snapshots = build_rows(
            fetched.lives, collected_at, category_map
        )
        log.info("저장 시작 - 채널 %d / 방송 %d / 스냅샷 %d",
                 len(channels), len(sessions), len(snapshots))

        # 외래키 순서: channel -> live_session -> live_snapshot
        channels_n = db.upsert_channels(channels)
        db.upsert_live_sessions(sessions)
        inserted = db.insert_snapshots(snapshots)

        status = "partial" if fetched.error else "success"

        # --- 4) 집계 + 보존정책 ------------------------------------------
        try:
            rollup = db.run_rollup()
            log.info("집계 갱신 - 시간별 %s행 / 일별 %s행 / 원본 삭제 %s행",
                     rollup.get("hours_updated"), rollup.get("days_updated"),
                     rollup.get("rows_deleted"))
        except DbError as e:
            # 집계 실패는 수집 실패가 아니다. 원본은 이미 저장됐고 다음 회차에 다시 계산된다.
            log.error("집계 갱신 실패(원본 데이터는 정상 저장됨): %s", e)
            if status == "success":
                status = "partial"
            error_message = (error_message or "") + f" | 집계 실패: {e}"

    except (ChzzkError, DbError) as e:
        error_message = str(e)
        log.error("수집 실패: %s", e)
    except Exception as e:  # noqa: BLE001 - 예상 못한 오류도 로그를 남기고 넘어가야 함
        error_message = f"예상치 못한 오류: {e!r}"
        log.exception("수집 중 예상치 못한 오류")

    # --- 5) 회차 로그 마무리 ---------------------------------------------
    duration_ms = int((time.time() - started) * 1000)
    db.finish_run(
        run_id,
        finished_at=datetime.now(timezone.utc).isoformat(),
        status=status,
        pages_fetched=fetched.pages_fetched if fetched else 0,
        lives_collected=len(fetched.lives) if fetched else 0,
        rows_inserted=inserted,
        channels_upserted=channels_n,
        last_page_min_ccu=fetched.last_page_min_ccu if fetched else None,
        duration_ms=duration_ms,
        error_message=str(error_message)[:2000] if error_message else None,
    )

    log.info("=" * 60)
    log.info("결과: %s | 저장 %d건 | 소요 %.1f초 | run_id=%d",
             status.upper(), inserted, duration_ms / 1000, run_id)
    if error_message:
        log.info("비고: %s", str(error_message)[:300])
    log.info("=" * 60)

    # partial 은 다음 15분에 자연히 복구되므로 실패로 처리하지 않는다
    return 0 if status in ("success", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
