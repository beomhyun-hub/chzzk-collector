"""dump_preview.py 출력을 대시보드 페이지의 DATA 형식으로 바꾼다.

    python scripts/dump_preview.py --images > preview_data.json
    python scripts/build_dashboard_data.py preview_data.json > dashboard_data.json

페이지가 쓰는 키 이름은 짧게 줄인 것이라 dump 출력이 그대로 들어가지 않는다.
이 스크립트가 그 대응을 한 곳에 모아둔 것이다. (읽기 전용, 네트워크 안 씀)
"""

import io
import json
import sys

RUN_LIMIT = 12   # 수집 로그 패널에 보일 회차 수
CAT_LIMIT = 20   # 카테고리 랭킹은 페이지에서 다시 12개로 자른다


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "preview_data.json"
    d = json.load(io.open(src, encoding="utf-8"))

    series = [
        {"at": s["at"], "ccu": s["total_ccu"], "lives": s["live_count"]}
        for s in d["series"]
    ]

    cats = []
    for c in d["category_share"][:CAT_LIMIT]:
        cat = c["category"]
        cats.append({
            "name": cat["live_category_value"],
            "type": cat["category_type"],
            "img": cat.get("img"),
            "ccu": int(round(c["avg_ccu"])),
            "lives": c["live_count"],
            "peak": c["peak_ccu"],
        })

    lives = []
    for l in d["top_lives"]:
        ls, cat = l["live_session"], l["category"]
        lives.append({
            "ch": ls["channel"]["channel_name"],
            "img": ls["channel"].get("img"),
            "title": ls.get("live_title") or "",
            "ccu": l["concurrent_user_count"],
            "cat": cat["live_category_value"],
            "type": cat["category_type"],
            "open": ls["open_date"],
        })

    runs = [
        {"id": r["run_id"], "at": r["started_at"], "st": r["status"],
         "pages": r["pages_fetched"], "rows": r["rows_inserted"],
         "ms": r["duration_ms"]}
        for r in d["runs"][:RUN_LIMIT]
    ]

    # 페이지 범례 순서를 고정한다 — 색이 회차마다 바뀌면 읽기 나쁘다.
    order = ["GAME", "ETC", "ENTERTAINMENT", "NONE"]
    bytype = [{"type": t, "ccu": int(round(d["by_type"][t]))}
              for t in order if t in d["by_type"]]

    out = {
        "status": d["status"],
        "series": series,
        "cats": cats,
        "lives": lives,
        "runs": runs,
        "bytype": bytype,
        "latest": d["latest_at"],
    }
    json.dump(out, io.open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False),
              ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
