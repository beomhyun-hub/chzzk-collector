"""
2단계 확인: Supabase 연결 / 권한 / 실제 쓰기 검증

테스트용 가짜 데이터를 넣었다가 반드시 지웁니다. 실제 데이터에는 영향이 없습니다.

사용법:
    python scripts/test_db.py
"""

import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

TEST_CHANNEL_ID = "__conn_test_channel__"
TEST_LIVE_ID = -999_999_999  # 실제 liveId 와 절대 겹치지 않는 음수

failures = []


def head(title):
    print("\n" + "=" * 62)
    print("  " + title)
    print("=" * 62)


def ok(msg):
    print("  [OK]   " + msg)


def bad(msg):
    print("  [FAIL] " + msg)
    failures.append(msg)


def load_config():
    load_dotenv()
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

    if not url or not key or "PASTE_" in url or "PASTE_" in key:
        print("\n[중단] .env 의 SUPABASE_URL / SUPABASE_SERVICE_KEY 가 비어있습니다.")
        sys.exit(1)
    if url.endswith("/rest/v1"):
        url = url[: -len("/rest/v1")]
        print("  (SUPABASE_URL 뒤의 /rest/v1 은 자동으로 떼고 사용합니다)")
    if key.startswith(("sb_publishable_", "eyJ")) and "publishable" in key:
        bad("publishable 키로 보입니다. 쓰기가 불가능합니다.")

    print("  URL : {}".format(url))
    print("  KEY : {}... ({}자)".format(key[:12], len(key)))
    return url, key


def rest(method, path, key, base, **kwargs):
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    }
    headers.update(kwargs.pop("extra_headers", {}))
    return requests.request(
        method, base + "/rest/v1" + path, headers=headers, timeout=15, **kwargs
    )


def show_error(resp):
    print("        HTTP {} - {}".format(resp.status_code, resp.text[:400]))


def main():
    head("Supabase 연결 검증")
    base, key = load_config()

    # ---------------- 1. 읽기 ----------------
    head("검증 1 - 읽기 권한 / 테이블 존재 확인")
    tables = [
        "channel", "category", "live_session", "live_snapshot",
        "collection_run", "agg_category_hourly", "agg_channel_daily",
    ]
    for t in tables:
        r = rest("GET", "/{}?select=*&limit=1".format(t), key, base)
        if r.status_code == 200:
            ok("{:<20} 조회 성공".format(t))
        else:
            bad("{:<20} 조회 실패".format(t))
            show_error(r)

    # 미리 넣어둔 '미분류' 카테고리 확인
    r = rest("GET", "/category?select=category_id,live_category_value", key, base)
    if r.status_code == 200:
        rows = r.json()
        ok("category 테이블 {}행 (미분류 시드 1행이 정상)".format(len(rows)))
    else:
        bad("category 조회 실패")
        show_error(r)

    # ---------------- 2. 집계 함수 ----------------
    head("검증 2 - 집계/보존정책 함수 run_rollup()")
    r = rest("POST", "/rpc/run_rollup", key, base, json={})
    if r.status_code == 200:
        ok("run_rollup() 호출 성공 -> {}".format(r.text[:200]))
    else:
        bad("run_rollup() 호출 실패")
        show_error(r)

    # ---------------- 3. 쓰기 ----------------
    head("검증 3 - 쓰기 권한 (테스트 데이터 넣고 바로 지움)")
    now = datetime.now(timezone.utc).isoformat()

    # 3-1 channel upsert
    r = rest(
        "POST", "/channel?on_conflict=channel_id", key, base,
        json=[{
            "channel_id": TEST_CHANNEL_ID,
            "channel_name": "connection test",
            "last_seen_at": now,
        }],
        extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    if r.status_code in (200, 201, 204):
        ok("channel  upsert 성공")
    else:
        bad("channel  upsert 실패")
        show_error(r)

    # 3-2 live_session upsert (channel 외래키가 걸려있는지도 같이 검증됨)
    r = rest(
        "POST", "/live_session?on_conflict=live_id", key, base,
        json=[{
            "live_id": TEST_LIVE_ID,
            "channel_id": TEST_CHANNEL_ID,
            "live_title": "connection test",
            "open_date": now,
            "adult": False,
            "tags": ["test"],
            "last_seen_at": now,
        }],
        extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    if r.status_code in (200, 201, 204):
        ok("live_session upsert 성공 (외래키 정상)")
    else:
        bad("live_session upsert 실패")
        show_error(r)

    # 3-3 live_snapshot insert - 실제 수집이 하는 것과 동일한 형태
    cat = rest("GET", "/category?select=category_id&limit=1", key, base)
    cat_id = cat.json()[0]["category_id"] if cat.status_code == 200 and cat.json() else None
    r = rest(
        "POST", "/live_snapshot?on_conflict=live_id,collected_at", key, base,
        json=[{
            "live_id": TEST_LIVE_ID,
            "collected_at": now,
            "category_id": cat_id,
            "concurrent_user_count": 123,
        }],
        extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    if r.status_code in (200, 201, 204):
        ok("live_snapshot insert 성공")
    else:
        bad("live_snapshot insert 실패")
        show_error(r)

    # 3-4 collection_run 로그 기록
    r = rest(
        "POST", "/collection_run", key, base,
        json=[{"status": "success", "lives_collected": 0, "rows_inserted": 0}],
        extra_headers={"Prefer": "return=representation"},
    )
    run_id = None
    if r.status_code in (200, 201):
        run_id = r.json()[0]["run_id"]
        ok("collection_run 기록 성공 (run_id={})".format(run_id))
    else:
        bad("collection_run 기록 실패")
        show_error(r)

    # ---------------- 4. 정리 ----------------
    head("검증 4 - 테스트 데이터 삭제 (원상복구)")
    # live_snapshot 은 live_session 삭제 시 cascade 로 같이 지워짐
    for path, label in [
        ("/live_session?live_id=eq.{}".format(TEST_LIVE_ID), "live_session(+snapshot cascade)"),
        ("/channel?channel_id=eq.{}".format(TEST_CHANNEL_ID), "channel"),
    ]:
        r = rest("DELETE", path, key, base,
                 extra_headers={"Prefer": "return=minimal"})
        if r.status_code in (200, 204):
            ok("{} 삭제 완료".format(label))
        else:
            bad("{} 삭제 실패 - 수동 정리 필요".format(label))
            show_error(r)

    if run_id is not None:
        r = rest("DELETE", "/collection_run?run_id=eq.{}".format(run_id), key, base,
                 extra_headers={"Prefer": "return=minimal"})
        if r.status_code in (200, 204):
            ok("collection_run 테스트 행 삭제 완료")

    # 최종 상태
    r = rest("GET", "/v_pipeline_status?select=*", key, base)
    if r.status_code == 200 and r.json():
        s = r.json()[0]
        print("\n  현재 DB 상태:")
        for k, v in s.items():
            print("    {:<16} {}".format(k, v))
        if s.get("스냅샷_행수") == 0 and s.get("채널_수") == 0:
            ok("테스트 데이터가 남지 않고 깨끗하게 정리됨")
        else:
            bad("테스트 데이터가 남아있습니다: 스냅샷 {}행 / 채널 {}행".format(
                s.get("스냅샷_행수"), s.get("채널_수")))

    head("검증 결과 요약")
    if failures:
        print("  실패 {}건:".format(len(failures)))
        for f in failures:
            print("    - " + f)
        sys.exit(1)
    print("  통과. DB 준비 완료. 3단계(수집 스크립트)로 넘어가도 됩니다.")


if __name__ == "__main__":
    main()
