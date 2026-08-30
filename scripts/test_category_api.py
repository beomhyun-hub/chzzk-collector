"""카테고리 검색 API 로 포스터 이미지를 가져올 수 있는지 검증.

    python scripts/test_category_api.py

DB 는 읽기만 합니다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from chzzk_collector.config import Config  # noqa: E402

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

SEARCH_PATH = "/open/v1/categories/search"


def main():
    cfg = Config.from_env()
    ch = {
        "Client-Id": cfg.chzzk_client_id,
        "Client-Secret": cfg.chzzk_client_secret,
        "Content-Type": "application/json",
    }
    sh = {"apikey": cfg.supabase_key, "Authorization": "Bearer " + cfg.supabase_key}

    # DB 에 쌓인 카테고리 중 동접 상위 것들을 표본으로
    rows = requests.get(
        cfg.supabase_url + "/rest/v1/category"
        "?select=category_id,category_type,live_category,live_category_value"
        "&live_category_value=neq.&limit=1000",
        headers=sh, timeout=30,
    ).json()

    # 스냅샷이 많은 순으로 정렬하려면 조인이 필요하니, 여기서는 앞에서부터 12개만
    sample = rows[:12]
    print("DB 에 저장된 카테고리 표본 {}개로 대조합니다.\n".format(len(sample)))

    hit = miss = 0
    for r in sample:
        name = r["live_category_value"]
        resp = requests.get(
            "https://openapi.chzzk.naver.com" + SEARCH_PATH,
            headers=ch, params={"query": name, "size": 10}, timeout=10,
        )
        if resp.status_code != 200:
            print("  [실패] {:<22} HTTP {} {}".format(name[:22], resp.status_code, resp.text[:120]))
            miss += 1
            continue

        data = (resp.json().get("content") or {}).get("data") or []
        # 우리가 가진 liveCategory 코드와 categoryId 가 같은 것을 찾는다
        exact = next((d for d in data if d.get("categoryId") == r["live_category"]), None)
        byname = next((d for d in data if d.get("categoryValue") == name), None)
        found = exact or byname

        if not found:
            print("  [매칭실패] {:<22} 검색결과 {}건: {}".format(
                name[:22], len(data), [d.get("categoryValue") for d in data][:3]))
            miss += 1
            continue

        how = "categoryId 일치" if exact else "이름 일치(ID 다름: {} vs {})".format(
            found.get("categoryId"), r["live_category"])
        poster = found.get("posterImageUrl")
        print("  [OK] {:<22} {:<28} 포스터: {}".format(
            name[:22], how, (poster or "없음")[:52]))
        hit += 1

    print("\n결과: {}건 성공 / {}건 실패".format(hit, miss))
    if hit:
        print("-> category 테이블에 poster_image_url 을 채워 넣을 수 있습니다.")


if __name__ == "__main__":
    main()
