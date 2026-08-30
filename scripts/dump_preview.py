"""대시보드 시안에 넣을 실제 데이터를 JSON 으로 뽑는다. (읽기 전용)

    python scripts/dump_preview.py > preview_data.json
"""

import base64
import io as _io
import json
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from chzzk_collector.config import Config  # noqa: E402


def main():
    cfg = Config.from_env()
    base = cfg.supabase_url + "/rest/v1"
    h = {"apikey": cfg.supabase_key, "Authorization": "Bearer " + cfg.supabase_key}

    def get(path):
        r = requests.get(base + path, headers=h, timeout=60)
        r.raise_for_status()
        return r.json()

    def get_all(path, page=1000):
        """PostgREST 는 한 번에 최대 1000행만 주므로 나눠서 전부 받는다."""
        out, offset = [], 0
        sep = "&" if "?" in path else "?"
        while True:
            rows = get("{}{}limit={}&offset={}".format(path, sep, page, offset))
            out.extend(rows)
            if len(rows) < page:
                return out
            offset += page

    out = {}

    out["status"] = get("/v_pipeline_status?select=*")[0]

    out["runs"] = get(
        "/collection_run?select=run_id,started_at,finished_at,status,pages_fetched,"
        "lives_collected,rows_inserted,duration_ms,last_page_min_ccu,error_message"
        "&order=run_id.desc&limit=20"
    )

    # 수집 시각별 전체 동접 (시계열)
    all_snaps = get_all("/live_snapshot?select=collected_at,concurrent_user_count")
    by_tick: dict[str, list[int]] = {}
    for r in all_snaps:
        by_tick.setdefault(r["collected_at"], []).append(r["concurrent_user_count"])
    ticks = sorted(by_tick)
    out["series"] = [
        {"at": t, "total_ccu": sum(by_tick[t]), "live_count": len(by_tick[t])}
        for t in ticks
    ]

    # 최신 스냅샷 채널 랭킹
    latest = ticks[-1]
    out["latest_at"] = latest
    out["top_lives"] = get(
        "/live_snapshot?select=concurrent_user_count,"
        "live_session(live_id,live_title,open_date,"
        "channel(channel_id,channel_name,channel_image_url)),"
        "category(category_type,live_category_value)"
        "&collected_at=eq.{}&order=concurrent_user_count.desc&limit=25".format(
            quote(latest, safe=""))
    )

    # 카테고리 점유율 (최신 집계 시간대)
    hours = get("/agg_category_hourly?select=bucket_hour&order=bucket_hour.desc&limit=1")
    if hours:
        hb = hours[0]["bucket_hour"]
        out["latest_hour"] = hb
        out["category_share"] = get(
            "/agg_category_hourly?select=avg_ccu,peak_ccu,live_count,channel_count,"
            "category(category_type,live_category,live_category_value)"
            "&bucket_hour=eq.{}&order=avg_ccu.desc&limit=20".format(quote(hb, safe=""))
        )

    # 채널 일별 집계
    out["channel_daily"] = get(
        "/agg_channel_daily?select=bucket_date,avg_ccu,peak_ccu,live_count,snapshot_count,"
        "channel(channel_name),category(live_category_value)"
        "&order=bucket_date.desc,avg_ccu.desc&limit=25"
    )

    # 카테고리 타입 분포 (GAME / ETC / SPORTS)
    out["by_type"] = {}
    for row in out.get("category_share", []):
        t = (row.get("category") or {}).get("category_type") or ""
        out["by_type"][t] = out["by_type"].get(t, 0) + float(row["avg_ccu"])

    # 아티팩트는 외부 이미지를 못 불러오므로, 시안용으로 프로필 사진을 작게 줄여
    # 페이지 안에 직접 심는다. 실제 대시보드에서는 URL 그대로 쓰면 된다.
    if "--images" in sys.argv:
        from PIL import Image

        def shrink(url, box, fmt="WEBP", quality=78):
            raw = requests.get(url, timeout=15)
            raw.raise_for_status()
            im = Image.open(_io.BytesIO(raw.content)).convert("RGB")
            im.thumbnail(box, Image.LANCZOS)
            buf = _io.BytesIO()
            im.save(buf, format=fmt, quality=quality, method=6)
            return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

        # 카테고리 포스터 — DB 컬럼이 아직 없을 수 있으므로 검색 API 로 직접 가져온다
        ch_headers = {
            "Client-Id": cfg.chzzk_client_id,
            "Client-Secret": cfg.chzzk_client_secret,
            "Content-Type": "application/json",
        }
        for c in out.get("category_share", []):
            cat = c.get("category") or {}
            cat["img"] = None
            name, cid = cat.get("live_category_value"), cat.get("live_category")
            if not name or not cid:
                continue
            try:
                r = requests.get(
                    "https://openapi.chzzk.naver.com/open/v1/categories/search",
                    headers=ch_headers, params={"query": name, "size": 10}, timeout=15)
                r.raise_for_status()
                hit = next((d for d in (r.json().get("content") or {}).get("data") or []
                            if d.get("categoryId") == cid), None)
                if hit and hit.get("posterImageUrl"):
                    cat["img"] = shrink(hit["posterImageUrl"], (108, 144))
            except Exception as e:
                print("  포스터 실패: {} ({})".format(name, e), file=sys.stderr)

        for item in out["top_lives"]:
            ch = (item.get("live_session") or {}).get("channel") or {}
            url = ch.get("channel_image_url")
            ch["img"] = None
            if not url:
                continue
            try:
                raw = requests.get(url, timeout=15)
                raw.raise_for_status()
                im = Image.open(_io.BytesIO(raw.content)).convert("RGB")
                im.thumbnail((72, 72), Image.LANCZOS)
                buf = _io.BytesIO()
                im.save(buf, format="WEBP", quality=78, method=6)
                ch["img"] = "data:image/webp;base64," + \
                    base64.b64encode(buf.getvalue()).decode("ascii")
            except Exception as e:
                print("  이미지 실패: {} ({})".format(ch.get("channel_name"), e),
                      file=sys.stderr)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
