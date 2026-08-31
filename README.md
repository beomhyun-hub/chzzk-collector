# 치지직 라이브 수집 파이프라인

15분마다 치지직(CHZZK) 라이브 목록을 수집해 Supabase(PostgreSQL)에 쌓는 파이프라인입니다.

## 진행 상황

- [x] 1단계 — API 응답 구조 검증 (`scripts/test_api.py`) — 통과
- [x] 2단계 — Supabase 스키마 (`sql/01_schema.sql`) — 적용·검증 완료 (`scripts/test_db.py`)
- [x] 3단계 — 수집 스크립트 (`chzzk_collector/`) — 첫 수집 성공 (89페이지 / 1,780건 / 32초)
- [x] 4단계 — GitHub Actions 워크플로우 — 수동 실행 성공 (1,748건 / 49초)
- [x] 5단계 — 자동 예약 실행 확인 — cron 은 발동하나 간격이 안 지켜짐을 확인, 반복문 방식으로 해결

## 폴더 구조

```
chzzk_collector/        수집 파이프라인 본체
  config.py             환경변수 로딩 (.env / GitHub Secrets 공용)
  chzzk.py              치지직 API 클라이언트 (페이지네이션·재시도)
  db.py                 Supabase 저장 계층 (배치 업서트)  ← 대시보드도 여기 재사용
  collect.py            수집 실행 진입점
scripts/
  test_api.py           API 응답 구조 검증
  test_db.py            DB 연결/권한/쓰기 검증
  check_data.py         수집된 데이터 점검
sql/
  01_schema.sql         테이블·인덱스·집계함수 (여러 번 실행해도 안전)
.github/workflows/
  collect.yml           예약 실행 진입점 - 340분짜리 구간 3개를 이어붙임
  _collect-loop.yml     구간 하나의 반복 수집 로직 (collect.yml 이 호출)
  keepalive.yml         60일 무활동으로 cron 이 꺼지는 것 방지 (월 1회)
```

## GitHub Actions

- 저장소: https://github.com/beomhyun-hub/chzzk-collector (public — Actions 무료 시간 무제한)
- Secrets 4개: `CHZZK_CLIENT_ID`, `CHZZK_CLIENT_SECRET`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`
- 수동 실행: Actions 탭 > 치지직 라이브 수집 > Run workflow
  - `duration_minutes` 입력값: `0` 이면 1회만 수집. 숫자를 넣으면 그 시간만큼 15분 간격 반복

### 15분 간격을 지키는 방법 (중요)

GitHub 무료 예약 실행은 **지정한 cron 간격을 지켜주지 않습니다.**
15분 간격으로 걸어도 실측(2026-08-30~31)에서는 이렇게 발동했습니다.

| 구간 | 간격 |
|---|---|
| 17:24 → 20:00 | 2시간 36분 |
| 20:00 → 22:41 | 2시간 41분 |
| 22:41 → 00:56 | 2시간 15분 |
| 00:56 → 06:59 | 6시간 3분 |

그래서 cron 은 '작업을 깨우는 방아쇠'로만 쓰고, 실제 간격은 job 안의 반복문이 지킵니다.

- 수집은 **정각 기준 경계(:00 :15 :30 :45)** 에 맞춰 돕니다.
  epoch 초가 900 의 배수인 시각이 곧 그 경계입니다(900 이 3600 을 나누므로 UTC·KST 둘 다 정렬됨).
- 깨어난 직후에는 경계를 기다리지 않고 바로 한 번 수집합니다. 단 경계가 2분 안이면 건너뜁니다.
- 작업 하나의 수명 한계가 6시간이라, **340분짜리 구간(leg)을 3개 이어붙여 한 번에 17시간**을 덮습니다.
  관측된 최대 cron 간격 6시간 3분보다 넉넉합니다. 구간 로직은 `_collect-loop.yml` 한 곳에만 있습니다.
- `concurrency.cancel-in-progress: true` 라서 cron 이 다시 발동하면 새 회차가 앞 회차를 이어받습니다.
- 앞 구간이 실패해도 다음 구간은 이어집니다(`!cancelled()`). 취소된 경우에만 멈춥니다.

간격을 바꾸려면 `_collect-loop.yml` 의 `interval_seconds` 기본값을 고치면 됩니다.
저장소가 public 이라 Actions 시간이 무제한이므로 오래 도는 것 자체는 비용이 들지 않습니다.

## 명령어

```
# 수집 1회 실행
python -m chzzk_collector.collect

# 점검
python scripts/test_api.py      # 치지직 API 가 정상인가
python scripts/test_db.py       # DB 연결/권한이 정상인가
python scripts/check_data.py    # 데이터가 잘 쌓이고 있는가
```

## API 요약 (공식 문서 검증 완료)

- Base URL: `https://openapi.chzzk.naver.com`
- 인증 헤더: `Client-Id`, `Client-Secret`, `Content-Type: application/json`
- 라이브 목록: `GET /open/v1/lives?size=20&next={커서}` (size 최대 20, 동접 내림차순)
- 채널 정보: `GET /open/v1/channels?channelIds=...` (최대 20개, `followerCount` 포함)
  - 아직 호출하지 않음. `chzzk.py`의 `fetch_channels()` 에 자리만 만들어 둠
- 응답 껍데기: `{ "code": 200, "message": null, "content": { "data": [...], "page": { "next": "..." } } }`

## 수집 정책

- 목록이 동접 내림차순이므로 **동접 10명 미만이 나오면 중단** (`MIN_CONCURRENT_USERS`)
  - 실측: 89페이지 / 1,780건 / 32초 (동접 5명까지 가면 138페이지 / 2,760건 / 49초)
- 같은 방송이 15분마다 계속 들어오는 것이 정상 — 덮어쓰지 않고 계속 적재
- 원본 스냅샷은 30일 보관, 그 전에 시간별·일별 집계로 굴려서 영구 보관 (`run_rollup`)
- 429/5xx/타임아웃은 지수 백오프로 3회 재시도, 그래도 실패하면 거기까지 저장하고 `partial`

## 환경변수

`.env.example` 참고. GitHub Actions 에서는 같은 이름으로 Secrets 에 등록합니다.
