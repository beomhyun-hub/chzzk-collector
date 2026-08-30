"""
1단계: 치지직 Open API 응답 구조 검증 스크립트

이 스크립트는 DB에 아무것도 저장하지 않습니다. API가 문서대로 동작하는지만 확인합니다.

사용법:
    python scripts/test_api.py            # 기본 검증 (API 호출 4~5회)
    python scripts/test_api.py --depth    # + 동접 5명까지 몇 페이지인지 실측 (호출 많음)
"""

import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

# 콘솔에 표시할 수 없는 문자(이모지 등)가 있어도 죽지 않도록
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

BASE_URL = "https://openapi.chzzk.naver.com"
LIVES_PATH = "/open/v1/lives"
CHANNELS_PATH = "/open/v1/channels"

# 우리가 DB에 저장할 필드 (문서 기준)
EXPECTED_LIVE_FIELDS = {
    "liveId",
    "liveTitle",
    "liveThumbnailImageUrl",
    "concurrentUserCount",
    "openDate",
    "adult",
    "tags",
    "categoryType",
    "liveCategory",
    "liveCategoryValue",
    "channelId",
    "channelName",
    "channelImageUrl",
}
EXPECTED_CHANNEL_FIELDS = {
    "channelId",
    "channelName",
    "channelImageUrl",
    "followerCount",
    "verifiedMark",
}

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


def warn(msg):
    print("  [주의] " + msg)


def build_headers():
    load_dotenv()
    client_id = os.getenv("CHZZK_CLIENT_ID", "").strip()
    client_secret = os.getenv("CHZZK_CLIENT_SECRET", "").strip()

    placeholder = ("PASTE_" in client_id or "PASTE_" in client_secret
                   or "여기에" in client_id)
    if not client_id or not client_secret or placeholder:
        print("\n[중단] .env 파일의 CHZZK_CLIENT_ID / CHZZK_CLIENT_SECRET 이 비어있거나")
        print("       아직 PASTE_... 자리표시자 그대로입니다.")
        print("       D:\\06_chzzk-collector\\.env 를 메모장으로 열어 실제 키를 넣어주세요.")
        sys.exit(1)
    if client_id.startswith(("'", '"')) or client_secret.startswith(("'", '"')):
        print("\n[중단] 키에 따옴표가 붙어 있습니다. .env 에서 따옴표를 지워주세요.")
        sys.exit(1)

    print("  Client-Id 앞 6자리: {}...  (길이 {})".format(client_id[:6], len(client_id)))
    print("  Client-Secret 길이: {}".format(len(client_secret)))
    return {
        "Client-Id": client_id,
        "Client-Secret": client_secret,
        "Content-Type": "application/json",
    }


def call(headers, path, params=None):
    """API 1회 호출. (성공여부, 파싱된 body) 반환"""
    url = BASE_URL + path
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
    except requests.RequestException as e:
        bad("네트워크 오류: {}".format(e))
        return False, None

    print("  -> GET {} {}  ...  HTTP {}".format(path, params or "", resp.status_code))

    if resp.status_code == 401:
        bad("401 인증 실패 - Client ID/Secret 이 틀렸거나 앱이 아직 사용 가능 상태가 아닙니다.")
        print("        서버 응답: " + resp.text[:300])
        return False, None
    if resp.status_code == 403:
        bad("403 권한 없음 - 개발자센터에서 앱 승인이 필요할 수 있습니다.")
        print("        서버 응답: " + resp.text[:300])
        return False, None
    if resp.status_code == 429:
        bad("429 호출 한도 초과 - 잠시 뒤 다시 시도하세요.")
        return False, None
    if resp.status_code != 200:
        bad("예상치 못한 상태코드 {}: {}".format(resp.status_code, resp.text[:300]))
        return False, None

    try:
        body = resp.json()
    except ValueError:
        bad("JSON 파싱 실패: " + resp.text[:300])
        return False, None

    return True, body


def check_envelope(body):
    """{code, message, content} 껍데기 구조 확인"""
    if not isinstance(body, dict):
        bad("응답 최상위가 dict 가 아님: {}".format(type(body)))
        return None
    missing = {"code", "content"} - set(body.keys())
    if missing:
        bad("응답 껍데기에 {} 키가 없습니다. 실제 키: {}".format(sorted(missing), list(body.keys())))
        return None
    ok("응답 껍데기 확인: code={}, message={!r}, 최상위 키={}".format(
        body.get("code"), body.get("message"), list(body.keys())))
    return body["content"]


def test_lives_first_page(headers):
    head("검증 1 - 라이브 목록 첫 페이지 (GET /open/v1/lives)")
    success, body = call(headers, LIVES_PATH, {"size": 20})
    if not success:
        return None

    content = check_envelope(body)
    if content is None:
        return None

    if "data" not in content:
        bad("content 안에 'data' 가 없습니다. 실제 키: {}".format(list(content.keys())))
        return None
    data = content["data"]
    if not isinstance(data, list):
        bad("content.data 가 리스트가 아님: {}".format(type(data)))
        return None
    ok("content.data 리스트 확인 - {}건 (size=20 요청)".format(len(data)))

    page = content.get("page")
    if isinstance(page, dict) and "next" in page:
        ok("content.page.next 확인 - 커서 값: {}...".format(str(page["next"])[:40]))
    else:
        bad("content.page.next 가 없습니다. content 키: {}, page={!r}".format(
            list(content.keys()), page))

    if not data:
        warn("라이브가 0건입니다. 새벽 시간대라면 정상일 수 있습니다.")
        return content

    # --- 필드 검증 ---
    sample = data[0]
    actual = set(sample.keys())
    missing = EXPECTED_LIVE_FIELDS - actual
    extra = actual - EXPECTED_LIVE_FIELDS

    if missing:
        bad("문서에 있는데 응답에 없는 필드: {}".format(sorted(missing)))
    else:
        ok("기대한 {}개 필드 모두 존재".format(len(EXPECTED_LIVE_FIELDS)))
    if extra:
        warn("문서에 없던 추가 필드 발견: {}  (저장 대상에 넣을지 검토)".format(sorted(extra)))

    # --- 타입 검증 ---
    checks = [
        ("concurrentUserCount", int, sample.get("concurrentUserCount")),
        ("channelId", str, sample.get("channelId")),
        ("openDate", str, sample.get("openDate")),
        ("adult", bool, sample.get("adult")),
        ("tags", list, sample.get("tags")),
    ]
    for name, expected_type, value in checks:
        if value is None:
            warn("{} 값이 null 입니다 (스키마에서 NULL 허용 필요)".format(name))
        elif not isinstance(value, expected_type):
            bad("{} 타입이 {} 가 아니라 {} 입니다 (값: {!r})".format(
                name, expected_type.__name__, type(value).__name__, value))
        else:
            ok("{}: {} 확인 (예: {!r})".format(name, expected_type.__name__, value))

    null_cat = [d for d in data if not d.get("liveCategory")]
    if null_cat:
        warn("카테고리가 비어있는 방송 {}건 - DB 컬럼은 NULL 허용으로 만듭니다".format(len(null_cat)))

    print("\n  --- 실제 응답 샘플 1건 (원본) ---")
    print(json.dumps(sample, ensure_ascii=False, indent=2))

    print("\n  --- 동접 상위 5개 ---")
    for d in data[:5]:
        print("    {:>7,}명  [{}]  {} - {}".format(
            d.get("concurrentUserCount") or 0,
            d.get("liveCategoryValue") or "-",
            d.get("channelName"),
            str(d.get("liveTitle"))[:40],
        ))

    return content


def test_pagination(headers, first_content):
    head("검증 2 - 페이지네이션 (?next= 커서)")
    page = first_content.get("page") or {}
    cursor = page.get("next")
    if not cursor:
        bad("첫 페이지에 next 커서가 없어 페이지네이션을 검증할 수 없습니다.")
        return

    success, body = call(headers, LIVES_PATH, {"size": 20, "next": cursor})
    if not success:
        return
    content = check_envelope(body)
    if content is None:
        return

    data2 = content.get("data") or []
    ok("2페이지 {}건 수신".format(len(data2)))

    ids1 = {d.get("liveId") for d in first_content.get("data", [])}
    ids2 = {d.get("liveId") for d in data2}
    overlap = ids1 & ids2
    if overlap:
        bad("1페이지와 2페이지가 {}건 겹칩니다 - 커서 사용법 재검토 필요".format(len(overlap)))
    else:
        ok("1페이지와 2페이지가 겹치지 않음 - 커서 정상 동작")

    ccu1 = [d.get("concurrentUserCount") or 0 for d in first_content.get("data", [])]
    ccu2 = [d.get("concurrentUserCount") or 0 for d in data2]
    if ccu1 and ccu2:
        if min(ccu1) >= max(ccu2):
            ok("동접 내림차순 정렬 확인 (1p 최소 {:,} >= 2p 최대 {:,})".format(min(ccu1), max(ccu2)))
        else:
            warn("정렬이 완전한 내림차순이 아닐 수 있음 (1p 최소 {:,}, 2p 최대 {:,}) "
                 "- '동접 N명에서 중단' 전략 재검토 필요".format(min(ccu1), max(ccu2)))


def test_channels(headers, first_content):
    head("검증 3 - 채널 정보 / 팔로워 수 (GET /open/v1/channels)")
    data = first_content.get("data") or []
    channel_ids = [d["channelId"] for d in data[:3] if d.get("channelId")]
    if not channel_ids:
        warn("테스트할 channelId 가 없어 건너뜁니다.")
        return

    # channelIds 는 배열 파라미터 -> ?channelIds=A&channelIds=B
    success, body = call(headers, CHANNELS_PATH, {"channelIds": channel_ids})
    if not success:
        warn("채널 API 호출 실패 - 지금 당장은 필요 없으니 수집은 그대로 진행 가능합니다.")
        return

    content = check_envelope(body)
    if content is None:
        return

    ch_data = content.get("data") if isinstance(content, dict) else content
    if not isinstance(ch_data, list) or not ch_data:
        bad("채널 응답 구조가 예상과 다릅니다: {}".format(
            json.dumps(content, ensure_ascii=False)[:300]))
        return

    ok("채널 {}건 수신 (요청 {}건)".format(len(ch_data), len(channel_ids)))
    sample = ch_data[0]
    missing = EXPECTED_CHANNEL_FIELDS - set(sample.keys())
    if missing:
        bad("채널 응답에 없는 필드: {}".format(sorted(missing)))
    else:
        ok("followerCount 포함 기대 필드 모두 존재")

    for c in ch_data:
        fc = c.get("followerCount")
        if isinstance(fc, int):
            print("    {}: 팔로워 {:,}명".format(c.get("channelName"), fc))
        else:
            print("    {}: followerCount={!r}".format(c.get("channelName"), fc))


def test_depth(headers):
    head("검증 4 - 동접 5명까지 도달하는 데 필요한 페이지 수 실측")
    print("  (호출이 많습니다. 0.3초 간격으로 진행)\n")

    min_ccu = 5
    max_pages = 200
    cursor = None
    total = 0
    pages = 0
    last_ccu = None
    t0 = time.time()

    while pages < max_pages:
        params = {"size": 20}
        if cursor:
            params["next"] = cursor
        try:
            resp = requests.get(BASE_URL + LIVES_PATH, headers=headers, params=params, timeout=10)
        except requests.RequestException as e:
            bad("{}페이지에서 네트워크 오류: {}".format(pages + 1, e))
            break
        if resp.status_code == 429:
            bad("{}페이지에서 429 발생 - 페이지 간 대기시간을 늘려야 합니다.".format(pages + 1))
            break
        if resp.status_code != 200:
            bad("{}페이지에서 HTTP {}".format(pages + 1, resp.status_code))
            break

        content = resp.json().get("content") or {}
        data = content.get("data") or []
        pages += 1
        if not data:
            print("  {}페이지: 데이터 없음 -> 목록 끝".format(pages))
            break

        total += len(data)
        last_ccu = data[-1].get("concurrentUserCount") or 0
        if pages % 10 == 0 or last_ccu < min_ccu:
            print("  {:>3}페이지 누적 {:>5,}건  현재 동접 {:,}명".format(pages, total, last_ccu))

        if last_ccu < min_ccu:
            ok("{}페이지에서 동접 {}명 미만 도달 -> 여기서 중단".format(pages, min_ccu))
            break

        cursor = (content.get("page") or {}).get("next")
        if not cursor:
            print("  {}페이지: next 커서 없음 -> 목록 끝".format(pages))
            break
        time.sleep(0.3)

    elapsed = time.time() - t0
    print("\n  결과: {}페이지 / {:,}건 / {:.1f}초 (마지막 동접 {}명)".format(
        pages, total, elapsed, last_ccu))
    print("  -> 15분마다 실행 시 하루 약 {:,}회 호출 예상".format(pages * 96))
    if pages >= max_pages:
        warn("{}페이지 상한에 걸렸습니다. MAX_PAGES 조정이 필요할 수 있습니다.".format(max_pages))


def main():
    head("치지직 Open API 검증 시작")
    headers = build_headers()

    first = test_lives_first_page(headers)
    if first:
        test_pagination(headers, first)
        test_channels(headers, first)

    if "--depth" in sys.argv:
        test_depth(headers)
    else:
        print("\n  (--depth 옵션을 붙이면 동접 5명까지 몇 페이지인지 실측합니다)")

    head("검증 결과 요약")
    if failures:
        print("  실패 {}건:".format(len(failures)))
        for f in failures:
            print("    - " + f)
        sys.exit(1)
    print("  통과. 문서와 실제 응답이 일치합니다. 2단계(DB 스키마)로 넘어가도 됩니다.")


if __name__ == "__main__":
    main()
