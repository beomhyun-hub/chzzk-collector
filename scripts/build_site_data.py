"""사이트(GitHub Pages)가 읽을 JSON 을 만든다. DB 를 읽기만 한다.

    python scripts/build_site_data.py [출력폴더]

기본 출력 폴더는 `site/data`. 만드는 파일:

    overview.json              첫 화면에 필요한 모든 것
    channel/<channel_id>.json  채널 상세 (15분 간격 24시간 추이)
    category/<category_id>.json 카테고리 상세 (1시간 간격 24시간 추이)

이미지는 URL 그대로 넣는다. 아티팩트 시안과 달리 사이트는 외부 이미지를
불러올 수 있으므로 base64 로 심지 않는다(`dump_preview.py --images` 와 다른 점).
"""

import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from chzzk_collector.config import Config  # noqa: E402

WINDOW_HOURS = 24
TOP_LIVES = 60       # 첫 화면 표에 넣을 방송 수
DETAIL_CHANNELS = 40  # 상세 페이지를 만들 채널 수
DETAIL_CATS = 24      # 상세 페이지를 만들 카테고리 수
RUN_LIMIT = 12
PAGE = 1000           # PostgREST 한 번에 주는 최대 행 수


class Api:
    def __init__(self, cfg):
        self.base = cfg.supabase_url + "/rest/v1"
        self.s = requests.Session()
        self.s.headers.update({
            "apikey": cfg.supabase_key,
            "Authorization": "Bearer " + cfg.supabase_key,
        })
        self.calls = 0

    def get(self, path):
        self.calls += 1
        r = self.s.get(self.base + path, timeout=60)
        r.raise_for_status()
        return r.json()

    def get_all(self, path):
        """1000행 제한을 넘겨 전부 받는다."""
        out, offset = [], 0
        sep = "&" if "?" in path else "?"
        while True:
            rows = self.get("{}{}limit={}&offset={}".format(path, sep, PAGE, offset))
            out.extend(rows)
            if len(rows) < PAGE:
                return out
            offset += PAGE


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def write(root, rel, obj):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    return p.stat().st_size


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else
                   Path(__file__).resolve().parent.parent / "site" / "data")
    api = Api(Config.from_env())

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=WINDOW_HOURS)

    # ── 파이프라인 상태 / 수집 로그 ────────────────────────────────
    status = api.get("/v_pipeline_status?select=*")[0]
    runs = api.get(
        "/collection_run?select=run_id,started_at,status,pages_fetched,"
        "rows_inserted,duration_ms&order=run_id.desc&limit={}".format(RUN_LIMIT))

    # ── 전체 추이 (수집 시각별 합계) ───────────────────────────────
    snaps = api.get_all(
        "/live_snapshot?select=collected_at,concurrent_user_count"
        "&collected_at=gte.{}&order=collected_at.asc".format(iso(since)))
    by_tick = {}
    for r in snaps:
        t = by_tick.setdefault(r["collected_at"], [0, 0])
        t[0] += r["concurrent_user_count"]
        t[1] += 1
    ticks = sorted(by_tick)
    if not ticks:
        raise SystemExit("최근 {}시간 안에 수집된 스냅샷이 없습니다.".format(WINDOW_HOURS))
    series = [{"at": t, "ccu": by_tick[t][0], "lives": by_tick[t][1]} for t in ticks]
    latest = ticks[-1]

    # ── 가장 최근 스냅샷의 방송 목록 ───────────────────────────────
    top = api.get(
        "/live_snapshot?select=concurrent_user_count,live_id,category_id,"
        "live_session(live_title,open_date,peak_concurrent_user_count,"
        "channel(channel_id,channel_name,channel_image_url)),"
        "category(category_id,category_type,live_category_value,poster_image_url)"
        "&collected_at=eq.{}&order=concurrent_user_count.desc&limit={}".format(
            quote(latest, safe=""), TOP_LIVES))

    lives = []
    for r in top:
        ls = r.get("live_session") or {}
        ch = ls.get("channel") or {}
        cat = r.get("category") or {}
        lives.append({
            "live_id": r["live_id"],
            "ch": ch.get("channel_name") or "—",
            "ch_id": ch.get("channel_id"),
            "img": ch.get("channel_image_url"),
            "title": ls.get("live_title") or "",
            "ccu": r["concurrent_user_count"],
            "peak": ls.get("peak_concurrent_user_count") or r["concurrent_user_count"],
            "cat": cat.get("live_category_value") or "미분류",
            "cat_id": cat.get("category_id"),
            "type": cat.get("category_type") or "NONE",
            "open": ls.get("open_date"),
        })

    # ── 카테고리 점유율 (최신 집계 시간대) ─────────────────────────
    hours = api.get("/agg_category_hourly?select=bucket_hour&order=bucket_hour.desc&limit=1")
    latest_hour = hours[0]["bucket_hour"] if hours else None
    cats = []
    if latest_hour:
        rows = api.get(
            "/agg_category_hourly?select=avg_ccu,peak_ccu,live_count,channel_count,"
            "category(category_id,category_type,live_category_value,poster_image_url)"
            "&bucket_hour=eq.{}&order=avg_ccu.desc&limit=30".format(
                quote(latest_hour, safe="")))
        for c in rows:
            cat = c.get("category") or {}
            cats.append({
                "id": cat.get("category_id"),
                "name": cat.get("live_category_value") or "미분류",
                "type": cat.get("category_type") or "NONE",
                "img": cat.get("poster_image_url"),
                "ccu": int(round(float(c["avg_ccu"]))),
                "peak": c["peak_ccu"],
                "lives": c["live_count"],
                "channels": c["channel_count"],
            })

    order = ["GAME", "ETC", "ENTERTAINMENT", "NONE"]
    tot = {}
    for c in cats:
        tot[c["type"]] = tot.get(c["type"], 0) + c["ccu"]
    bytype = [{"type": t, "ccu": tot[t]} for t in order if t in tot]

    overview = {
        "generated_at": iso(now),
        "latest": latest,
        "latest_hour": latest_hour,
        "window_hours": WINDOW_HOURS,
        "status": status,
        "series": series,
        "lives": lives,
        "cats": cats,
        "bytype": bytype,
        "runs": [{"id": r["run_id"], "at": r["started_at"], "st": r["status"],
                  "pages": r["pages_fetched"], "rows": r["rows_inserted"],
                  "ms": r["duration_ms"]} for r in runs],
        "detail_channels": [l["ch_id"] for l in lives[:DETAIL_CHANNELS] if l["ch_id"]],
        "detail_cats": [c["id"] for c in cats[:DETAIL_CATS] if c["id"]],
    }

    total = write(out_dir, "overview.json", overview)
    files = 1

    # ── 채널 상세: 15분 간격 24시간 추이 ───────────────────────────
    ch_ids = overview["detail_channels"]
    if ch_ids:
        sessions = api.get_all(
            "/live_session?select=live_id,channel_id,live_title,open_date"
            "&channel_id=in.({})&last_seen_at=gte.{}".format(
                ",".join(quote(c, safe="") for c in ch_ids), iso(since)))
        live_to_ch = {s["live_id"]: s["channel_id"] for s in sessions}
        if live_to_ch:
            rows = api.get_all(
                "/live_snapshot?select=live_id,collected_at,concurrent_user_count,category_id"
                "&live_id=in.({})&collected_at=gte.{}&order=collected_at.asc".format(
                    ",".join(str(i) for i in live_to_ch), iso(since)))
        else:
            rows = []

        cat_name = {c["id"]: c["name"] for c in cats}
        per_ch = {}
        for r in rows:
            cid = live_to_ch.get(r["live_id"])
            if cid is None:
                continue
            d = per_ch.setdefault(cid, {})
            slot = d.setdefault(r["collected_at"], [0, None])
            slot[0] += r["concurrent_user_count"]
            slot[1] = r["category_id"]

        by_id = {}
        for l in lives:
            by_id.setdefault(l["ch_id"], l)
        for cid in ch_ids:
            head = by_id.get(cid, {})
            pts = per_ch.get(cid, {})
            s = [{"at": t, "ccu": v[0]} for t, v in sorted(pts.items())]
            vals = [p["ccu"] for p in s] or [head.get("ccu", 0)]
            total += write(out_dir, "channel/{}.json".format(cid), {
                "id": cid,
                "name": head.get("ch"),
                "img": head.get("img"),
                "title": head.get("title"),
                "cat": head.get("cat"),
                "cat_id": head.get("cat_id"),
                "type": head.get("type"),
                "open": head.get("open"),
                "now": head.get("ccu"),
                "peak": max(vals),
                "avg": int(round(sum(vals) / len(vals))),
                "ticks": len(s),
                "latest": latest,
                "series": s,
                "cats": sorted({cat_name.get(v[1]) for v in pts.values()
                                if v[1] in cat_name}),
            })
            files += 1

    # ── 카테고리 상세: 1시간 간격 24시간 추이 ──────────────────────
    cat_ids = overview["detail_cats"]
    if cat_ids:
        rows = api.get_all(
            "/agg_category_hourly?select=bucket_hour,category_id,avg_ccu,peak_ccu,"
            "live_count,channel_count&category_id=in.({})&bucket_hour=gte.{}"
            "&order=bucket_hour.asc".format(
                ",".join(str(i) for i in cat_ids), iso(since)))
        per_cat = {}
        for r in rows:
            per_cat.setdefault(r["category_id"], []).append({
                "at": r["bucket_hour"],
                "ccu": int(round(float(r["avg_ccu"]))),
                "peak": r["peak_ccu"],
                "lives": r["live_count"],
            })
        head_by_id = {c["id"]: c for c in cats}
        for cid in cat_ids:
            head = head_by_id.get(cid, {})
            s = per_cat.get(cid, [])
            total += write(out_dir, "category/{}.json".format(cid), {
                "id": cid,
                "name": head.get("name"),
                "type": head.get("type"),
                "img": head.get("img"),
                "now": head.get("ccu"),
                # 시간 평균 계열의 최고점. agg 의 peak_ccu 는 카테고리 합계가 아니라
                # 그 시간대 단일 방송의 최고치라서 여기 섞으면 뜻이 어긋난다.
                "peak": max([p["ccu"] for p in s] or [head.get("ccu", 0)]),
                "peak_live": max([p["peak"] for p in s] or [head.get("peak", 0)]),
                "lives": head.get("lives"),
                "channels": head.get("channels"),
                "latest_hour": latest_hour,
                "series": s,
                "top": [{"ch": l["ch"], "ch_id": l["ch_id"], "img": l["img"],
                         "title": l["title"], "ccu": l["ccu"]}
                        for l in lives if l["cat_id"] == cid][:10],
            })
            files += 1

    print("{} 파일 {:,} bytes · REST 호출 {}회 · 최신 수집 {}".format(
        files, total, api.calls, latest), file=sys.stderr)


if __name__ == "__main__":
    main()
