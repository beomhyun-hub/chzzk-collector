"""수집된 데이터 점검.

    python scripts/check_data.py

DB 를 읽기만 합니다. 아무것도 바꾸지 않습니다.
"""

import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from chzzk_collector.config import Config  # noqa: E402

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def head(title):
    print("\n" + "=" * 68)
    print("  " + title)
    print("=" * 68)


def main():
    cfg = Config.from_env()
    base = cfg.supabase_url + "/rest/v1"
    headers = {
        "apikey": cfg.supabase_key,
        "Authorization": "Bearer " + cfg.supabase_key,
    }

    def get(path):
        r = requests.get(base + path, headers=headers, timeout=30)
        if r.status_code != 200:
            print("  [조회 실패] {} -> HTTP {} {}".format(path, r.status_code, r.text[:200]))
            return []
        return r.json()

    # ------------------------------------------------------------------
    head("파이프라인 현황")
    for row in get("/v_pipeline_status?select=*"):
        for k, v in row.items():
            print("  {:<14} {}".format(k, v))

    # ------------------------------------------------------------------
    head("최근 수집 회차")
    runs = get("/collection_run?select=run_id,started_at,status,pages_fetched,"
               "lives_collected,rows_inserted,duration_ms,last_page_min_ccu,error_message"
               "&order=run_id.desc&limit=5")
    print("  {:<5} {:<21} {:<8} {:>5} {:>7} {:>7} {:>7}".format(
        "run", "시작(UTC)", "상태", "페이지", "수집", "저장", "초"))
    for r in runs:
        print("  {:<5} {:<21} {:<8} {:>5} {:>7} {:>7} {:>7.1f}".format(
            r["run_id"], (r["started_at"] or "")[:19], r["status"],
            r["pages_fetched"], r["lives_collected"], r["rows_inserted"],
            (r["duration_ms"] or 0) / 1000))
        if r.get("error_message"):
            print("        오류: {}".format(r["error_message"][:120]))

    # ------------------------------------------------------------------
    head("동접 상위 방송 10개 (가장 최근 스냅샷)")
    latest = get("/live_snapshot?select=collected_at&order=collected_at.desc&limit=1")
    if latest:
        at = latest[0]["collected_at"]
        at_q = quote(at, safe="")
        print("  기준 시각(UTC): {}\n".format(at))
        rows = get(
            "/live_snapshot?select=concurrent_user_count,"
            "live_session(live_title,channel(channel_name)),category(live_category_value)"
            "&collected_at=eq.{}&order=concurrent_user_count.desc&limit=10".format(at_q)
        )
        for r in rows:
            ls = r.get("live_session") or {}
            ch = (ls.get("channel") or {}).get("channel_name", "?")
            cat = (r.get("category") or {}).get("live_category_value") or "미분류"
            print("  {:>7,}명  [{:<14}] {:<12} {}".format(
                r["concurrent_user_count"], cat[:14], str(ch)[:12],
                str(ls.get("live_title"))[:34]))

    # ------------------------------------------------------------------
    head("카테고리 점유율 상위 10개 (최근 집계 시간대)")
    hours = get("/agg_category_hourly?select=bucket_hour&order=bucket_hour.desc&limit=1")
    if hours:
        h = hours[0]["bucket_hour"]
        h_q = quote(h, safe="")
        print("  기준 시간대(UTC): {}\n".format(h))
        rows = get(
            "/agg_category_hourly?select=avg_ccu,peak_ccu,live_count,channel_count,"
            "category(live_category_value)"
            "&bucket_hour=eq.{}&order=avg_ccu.desc&limit=10".format(h_q)
        )
        total = sum(float(r["avg_ccu"]) for r in rows) or 1
        print("  {:<18} {:>10} {:>8} {:>7} {:>7}".format(
            "카테고리", "평균동접", "점유율", "방송수", "채널수"))
        for r in rows:
            cat = (r.get("category") or {}).get("live_category_value") or "미분류"
            avg = float(r["avg_ccu"])
            print("  {:<18} {:>10,.0f} {:>7.1f}% {:>7} {:>7}".format(
                cat[:18], avg, avg / total * 100, r["live_count"], r["channel_count"]))

    # ------------------------------------------------------------------
    head("평균 동접 상위 채널 10개 (오늘, KST 기준)")
    rows = get("/agg_channel_daily?select=bucket_date,avg_ccu,peak_ccu,live_count,"
               "snapshot_count,channel(channel_name),category(live_category_value)"
               "&order=bucket_date.desc,avg_ccu.desc&limit=10")
    print("  {:<14} {:>9} {:>9} {:>6} {:>7}  {}".format(
        "채널", "평균동접", "최고동접", "방송수", "스냅샷", "주력 카테고리"))
    for r in rows:
        ch = (r.get("channel") or {}).get("channel_name", "?")
        cat = (r.get("category") or {}).get("live_category_value") or "-"
        print("  {:<14} {:>9,.0f} {:>9,} {:>6} {:>7}  {}".format(
            str(ch)[:14], float(r["avg_ccu"]), r["peak_ccu"],
            r["live_count"], r["snapshot_count"], cat[:16]))

    # ------------------------------------------------------------------
    head("live_session 통계 갱신 여부")
    rows = get("/live_session?select=live_id,peak_concurrent_user_count,snapshot_count"
               "&order=peak_concurrent_user_count.desc&limit=1")
    if rows and rows[0]["peak_concurrent_user_count"] > 0:
        print("  [OK] 방송별 최고동접/스냅샷수가 채워지고 있습니다.")
    else:
        print("  [주의] 아직 0입니다.")
        print("         sql/01_schema.sql 을 Supabase 에 다시 한 번 실행해야 채워집니다.")
        print("         (run_rollup 함수에 방송 통계 갱신 단계를 추가했습니다)")

    print()


if __name__ == "__main__":
    main()
