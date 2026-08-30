# 치지직 라이브 수집 파이프라인

15분마다 치지직(CHZZK) 라이브 목록을 수집해 Supabase(PostgreSQL)에 쌓는 파이프라인입니다.

## 진행 상황

- [x] 1단계 — API 응답 구조 검증 (`scripts/test_api.py`) — 통과
- [x] 2단계 — Supabase 스키마 (`sql/01_schema.sql`) — 적용·검증 완료 (`scripts/test_db.py`)
- [x] 3단계 — 수집 스크립트 (`chzzk_collector/`) — 첫 수집 성공 (89페이지 / 1,780건 / 32초)
- [ ] 4단계 — GitHub Actions 워크플로우 (15분 cron)
- [ ] 5단계 — 첫 자동 수집 확인 및 데이터 점검

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
```

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
