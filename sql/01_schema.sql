-- ============================================================================
--  치지직 라이브 수집 파이프라인 - 테이블 스키마
--  Supabase SQL Editor 에 이 파일 전체를 붙여넣고 RUN 하세요. (여러 번 실행해도 안전)
-- ============================================================================
--
--  설계 요약
--  ----------------------------------------------------------------
--  channel        채널 정보 (이름, 이미지, 팔로워)          - 채널당 1행
--  category       카테고리 사전 (마인크래프트, talk 등)      - 카테고리당 1행
--  live_session   방송 1회                                  - 방송당 1행
--  live_snapshot  15분마다의 동접 기록  <- 실제로 쌓이는 곳   - 하루 약 15만 행
--  collection_run 수집 회차별 성공/실패 로그                 - 하루 96행
--
--  agg_category_hourly  시간대x카테고리 집계 (영구 보관)
--  agg_channel_daily    일자x채널 집계     (영구 보관)
--
--  왜 이렇게 나눴나:
--    채널명/방송제목을 15분마다 반복 저장하면 하루 159MB 라 무료티어(500MB)가
--    3일이면 찹니다. 변하지 않는 값은 별도 테이블로 빼고, live_snapshot 에는
--    숫자만 남겨 행당 약 52바이트로 줄였습니다. -> 동접 10명 컷오프 기준 하루 약 12MB.
--    여기에 원본 21일 보관 + 집계는 영구 보관으로 용량이 일정하게 유지됩니다.
--
--  시각 처리:
--    collected_at 은 UTC 로 저장합니다.
--    API 의 openDate 는 "2026-08-30 16:57:31" 처럼 타임존이 없는데 한국시간(KST)
--    입니다. 수집 스크립트에서 +09:00 을 붙여 timestamptz 로 넣습니다.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. channel : 채널 정보
-- ----------------------------------------------------------------------------
create table if not exists public.channel (
    channel_id          text        primary key,
    channel_name        text        not null,
    channel_image_url   text,

    -- 팔로워는 지금 수집하지 않습니다. GET /open/v1/channels 를 나중에 붙일 자리.
    follower_count      integer,
    follower_updated_at timestamptz,

    first_seen_at       timestamptz not null default now(),
    last_seen_at        timestamptz not null default now()
);

comment on table  public.channel is '치지직 채널. 방송이 한 번이라도 잡히면 등록됨';
comment on column public.channel.follower_count is 'GET /open/v1/channels 로 나중에 채울 컬럼. 지금은 NULL';

-- 채널명으로 검색 (대시보드 검색창용)
create index if not exists idx_channel_name on public.channel (channel_name);
-- 팔로워 갱신이 오래된 채널부터 훑기 위한 인덱스
create index if not exists idx_channel_follower_stale
    on public.channel (follower_updated_at nulls first);


-- ----------------------------------------------------------------------------
-- 2. category : 카테고리 사전
-- ----------------------------------------------------------------------------
--   categoryType        GAME / SPORTS / ETC
--   liveCategory        내부 코드   예: 'talk', 'Minecraft'
--   liveCategoryValue   화면 표시명 예: 'talk', '마인크래프트'   <- 집계는 이걸로
--
--   카테고리가 비어있는 방송이 실제로 존재합니다(검증 1단계에서 확인).
--   NULL 대신 빈 문자열('')로 저장합니다. 중복 방지 제약을 단순하게 걸기 위해서입니다.
--   조회할 때는 아래 v_category 뷰를 쓰면 ''가 '미분류'로 보입니다.
create table if not exists public.category (
    category_id         integer generated always as identity primary key,
    category_type       text not null default '',
    live_category       text not null default '',
    live_category_value text not null default '',

    constraint uq_category unique (category_type, live_category, live_category_value)
);

comment on table public.category is '카테고리 사전. live_snapshot 이 4바이트 정수로 참조하여 용량을 아낌';

create index if not exists idx_category_value on public.category (live_category_value);

-- 카테고리가 없는 방송이 담길 '미분류' 행을 미리 만들어 둡니다.
insert into public.category (category_type, live_category, live_category_value)
values ('', '', '')
on conflict (category_type, live_category, live_category_value) do nothing;

-- 카테고리 포스터 이미지 (GET /open/v1/categories/search 로 채움)
--   lives 응답의 liveCategory 가 그 API 의 categoryId 와 정확히 일치하는 것을
--   확인했습니다. 포스터가 없는 카테고리를 매 회차 조금씩 채워 넣고, 조회한
--   시각을 남겨 찾지 못한 카테고리를 무한히 다시 조회하지 않도록 합니다.
alter table public.category add column if not exists poster_image_url  text;
alter table public.category add column if not exists poster_checked_at timestamptz;

create index if not exists idx_category_poster_todo
    on public.category (poster_checked_at nulls first)
    where poster_image_url is null;

-- '' 을 '미분류' 로 보여주는 뷰
create or replace view public.v_category as
select
    category_id,
    nullif(category_type, '')                          as category_type,
    nullif(live_category, '')                          as live_category,
    coalesce(nullif(live_category_value, ''), '미분류') as category_name,
    poster_image_url
from public.category;


-- ----------------------------------------------------------------------------
-- 3. live_session : 방송 1회 (liveId 하나 = 방송 하나)
-- ----------------------------------------------------------------------------
--  방송 제목/태그는 방송 중에 바뀔 수 있지만 자주는 아니라서, 15분마다 쌓지 않고
--  여기에 "가장 최근 값"만 갱신합니다.
--  카테고리는 방송 도중 바뀌는 일이 잦고 트렌드 분석에 중요하므로 live_snapshot 쪽에 둡니다.
create table if not exists public.live_session (
    live_id            bigint      primary key,
    channel_id         text        not null references public.channel(channel_id),

    live_title         text,
    open_date          timestamptz,          -- 방송 시작 시각 (KST -> timestamptz 변환됨)
    adult              boolean     not null default false,
    tags               text[]      not null default '{}',
    thumbnail_url      text,

    first_seen_at      timestamptz not null default now(),
    last_seen_at       timestamptz not null default now(),
    peak_concurrent_user_count integer not null default 0,
    snapshot_count     integer     not null default 0
);

comment on table  public.live_session is '방송 1회. 방송 빈도/방송 길이 분석의 기준 단위';
comment on column public.live_session.snapshot_count is '이 방송이 잡힌 스냅샷 수. x15분 = 대략적인 방송 길이';

create index if not exists idx_live_session_channel   on public.live_session (channel_id, open_date desc);
create index if not exists idx_live_session_open_date on public.live_session (open_date desc);
create index if not exists idx_live_session_last_seen on public.live_session (last_seen_at desc);


-- ----------------------------------------------------------------------------
-- 4. live_snapshot : 15분마다의 동접 기록  ★ 실제로 쌓이는 테이블
-- ----------------------------------------------------------------------------
--  같은 방송이 15분마다 계속 들어오는 것이 정상이며, 덮어쓰지 않고 계속 쌓입니다.
--
--  기본키를 (live_id, collected_at) 로 잡았습니다.
--  요청하신 (channel_id, collected_at) 과 사실상 동일합니다 -- 한 채널은 동시에
--  방송을 하나만 할 수 있어서 1:1로 대응하는데, live_id 가 8바이트 정수라
--  32글자 문자열인 channel_id 보다 훨씬 가볍기 때문입니다.
--  채널 기준 조회는 live_session 을 거치거나 agg_channel_daily 를 쓰면 됩니다.
create table if not exists public.live_snapshot (
    live_id                 bigint      not null references public.live_session(live_id) on delete cascade,
    collected_at            timestamptz not null,
    category_id             integer     references public.category(category_id),
    concurrent_user_count   integer     not null,

    constraint pk_live_snapshot primary key (live_id, collected_at)
);

comment on table public.live_snapshot is '15분 간격 동접 스냅샷. 원본은 21일 보관 후 집계로 대체됨(run_rollup 참고)';

-- 시간 범위 조회 + 보존정책 삭제용.
-- BRIN 을 쓰는 이유: 데이터가 항상 시간순으로 들어와서 BRIN 이 잘 맞고,
-- 일반 인덱스보다 용량이 수천 배 작습니다. (하루 5MB 절약)
create index if not exists idx_snapshot_collected_at_brin
    on public.live_snapshot using brin (collected_at) with (pages_per_range = 32);

-- 카테고리 인덱스는 만들었다가 제거했습니다.
--   카테고리 분석은 전부 agg_category_hourly 에서 하기 때문에 이 인덱스를
--   실제로 타는 조회가 없는데, 실측 결과 전체 용량의 15%(행당 16바이트)를
--   차지하고 있었습니다. 원본 보관 기간을 벌기 위해 지웁니다.
--   원본을 카테고리로 직접 뒤질 일이 생기면 BRIN 시간 인덱스로 기간을 좁힌 뒤
--   훑으면 되고, 그 정도 조회는 몇 초 안에 끝납니다.
drop index if exists public.idx_snapshot_category;


-- ----------------------------------------------------------------------------
-- 5. collection_run : 수집 회차 로그
-- ----------------------------------------------------------------------------
create table if not exists public.collection_run (
    run_id           bigint generated always as identity primary key,
    started_at       timestamptz not null default now(),
    finished_at      timestamptz,
    status           text        not null default 'running'
                     check (status in ('running', 'success', 'partial', 'failed')),

    pages_fetched    integer not null default 0,
    lives_collected  integer not null default 0,
    rows_inserted    integer not null default 0,
    channels_upserted integer not null default 0,
    last_page_min_ccu integer,          -- 마지막 페이지의 최저 동접 (컷오프 확인용)
    duration_ms      integer,
    error_message    text,
    retry_count      integer not null default 0
);

comment on table public.collection_run is '수집 회차별 성공/실패 로그. status=partial 은 일부 페이지만 성공한 경우';

create index if not exists idx_collection_run_started on public.collection_run (started_at desc);
create index if not exists idx_collection_run_status  on public.collection_run (status, started_at desc);


-- ----------------------------------------------------------------------------
-- 6. agg_category_hourly : 시간대 x 카테고리 집계 (영구 보관)
-- ----------------------------------------------------------------------------
--   분석 A) 카테고리별 시청 트렌드가 여기서 나옵니다.
create table if not exists public.agg_category_hourly (
    bucket_hour     timestamptz not null,
    category_id     integer     not null references public.category(category_id),

    snapshot_count  integer not null,   -- 집계에 들어간 행 수
    tick_count      integer not null,   -- 서로 다른 수집 시각의 수 (보통 4)
    sum_ccu         bigint  not null,   -- 동접 합계
    avg_ccu         numeric(12,2) not null, -- sum_ccu / tick_count = 그 시간대 평균 동시 시청자
    peak_ccu        integer not null,
    live_count      integer not null,   -- 그 시간대에 이 카테고리로 방송한 방송 수
    channel_count   integer not null,

    updated_at      timestamptz not null default now(),
    constraint pk_agg_category_hourly primary key (bucket_hour, category_id)
);

create index if not exists idx_agg_cat_hourly_cat
    on public.agg_category_hourly (category_id, bucket_hour desc);


-- ----------------------------------------------------------------------------
-- 7. agg_channel_daily : 일자 x 채널 집계 (영구 보관)
-- ----------------------------------------------------------------------------
--   분석 B) 채널별 리포트가 여기서 나옵니다.
create table if not exists public.agg_channel_daily (
    bucket_date     date not null,
    channel_id      text not null references public.channel(channel_id),

    snapshot_count  integer not null,               -- x15분 = 그날 방송한 대략적 시간
    sum_ccu         bigint  not null,
    avg_ccu         numeric(12,2) not null,         -- 평균 동접
    peak_ccu        integer not null,
    live_count      integer not null,               -- 그날 방송 횟수
    top_category_id integer references public.category(category_id),  -- 주력 카테고리
    follower_count  integer,                        -- 그날의 팔로워 수 (나중에 채움)

    updated_at      timestamptz not null default now(),
    constraint pk_agg_channel_daily primary key (bucket_date, channel_id)
);

comment on column public.agg_channel_daily.follower_count
    is '팔로워 대비 동접 비율 분석용. 채널 API 를 붙이면 채워짐';

create index if not exists idx_agg_ch_daily_channel
    on public.agg_channel_daily (channel_id, bucket_date desc);
create index if not exists idx_agg_ch_daily_date
    on public.agg_channel_daily (bucket_date desc, avg_ccu desc);


-- ============================================================================
--  RLS (Row Level Security)
--  Supabase 테이블은 기본적으로 인터넷에 노출될 수 있으므로 반드시 켜야 합니다.
--  - 수집 스크립트는 service_role 키를 쓰는데, 이 키는 RLS 를 통과합니다.
--  - 나중에 붙일 Vercel 대시보드는 anon 키로 "읽기만" 가능하게 열어둡니다.
--    (치지직 공개 방송 정보라 읽기 공개는 문제 없습니다.
--     원치 않으면 아래 anon 관련 policy 블록만 지우세요.)
-- ============================================================================
alter table public.channel             enable row level security;
alter table public.category            enable row level security;
alter table public.live_session        enable row level security;
alter table public.live_snapshot       enable row level security;
alter table public.collection_run      enable row level security;
alter table public.agg_category_hourly enable row level security;
alter table public.agg_channel_daily   enable row level security;

do $$
declare
    t text;
begin
    foreach t in array array[
        'channel', 'category', 'live_session', 'live_snapshot',
        'collection_run', 'agg_category_hourly', 'agg_channel_daily'
    ] loop
        execute format('drop policy if exists "public read %1$s" on public.%1$I', t);
        execute format(
            'create policy "public read %1$s" on public.%1$I for select to anon, authenticated using (true)',
            t
        );
    end loop;
end $$;


-- 프로젝트 생성 시 "Automatically expose new tables" 를 껐더라도 동작하도록
-- 권한을 명시적으로 부여합니다. (이미 부여돼 있으면 아무 일도 일어나지 않습니다)
grant usage on schema public to anon, authenticated, service_role;
grant select on all tables    in schema public to anon, authenticated;
grant all    on all tables    in schema public to service_role;
grant usage, select on all sequences in schema public to service_role;
grant all    on all functions in schema public to service_role;


-- ============================================================================
--  집계 + 보존정책 함수
--  수집 스크립트가 매 회차 끝에 select public.run_rollup(); 을 호출합니다.
--  - 최근 몇 시간/며칠만 다시 계산하므로 가볍습니다.
--  - 원본을 지우기 전에 반드시 집계가 끝나므로 데이터 손실이 없습니다.
-- ============================================================================
create or replace function public.run_rollup(
    p_hour_lookback     integer default 6,   -- 최근 6시간 재집계
    p_day_lookback      integer default 3,   -- 최근 3일 재집계
    p_retention_days    integer default 21   -- 원본 보관 기간
)
returns table (
    hours_updated  integer,
    days_updated   integer,
    rows_deleted   integer
)
language plpgsql
security invoker
set search_path = public
as $$
declare
    v_hours int := 0;
    v_days  int := 0;
    v_del   int := 0;
    v_hour_from timestamptz := date_trunc('hour', now()) - make_interval(hours => p_hour_lookback);
    v_day_from  date        := (now() at time zone 'Asia/Seoul')::date - p_day_lookback;
begin
    ------------------------------------------------------------------
    -- 1) 시간대 x 카테고리
    --    진행 중인 현재 시간대도 포함해서 넣되, 다음 실행 때 완성된 값으로 덮어씁니다.
    ------------------------------------------------------------------
    insert into public.agg_category_hourly as a (
        bucket_hour, category_id, snapshot_count, tick_count,
        sum_ccu, avg_ccu, peak_ccu, live_count, channel_count, updated_at
    )
    select
        date_trunc('hour', s.collected_at)                       as bucket_hour,
        s.category_id                                            as category_id,
        count(*)                                                 as snapshot_count,
        count(distinct s.collected_at)                           as tick_count,
        sum(s.concurrent_user_count)                             as sum_ccu,
        round(sum(s.concurrent_user_count)::numeric
              / greatest(count(distinct s.collected_at), 1), 2)  as avg_ccu,
        max(s.concurrent_user_count)                             as peak_ccu,
        count(distinct s.live_id)                                as live_count,
        count(distinct ls.channel_id)                            as channel_count,
        now()
    from public.live_snapshot s
    join public.live_session ls on ls.live_id = s.live_id
    where s.collected_at >= v_hour_from
      and s.category_id is not null
    group by 1, 2
    on conflict (bucket_hour, category_id) do update set
        snapshot_count = excluded.snapshot_count,
        tick_count     = excluded.tick_count,
        sum_ccu        = excluded.sum_ccu,
        avg_ccu        = excluded.avg_ccu,
        peak_ccu       = excluded.peak_ccu,
        live_count     = excluded.live_count,
        channel_count  = excluded.channel_count,
        updated_at     = now();
    get diagnostics v_hours = row_count;

    ------------------------------------------------------------------
    -- 2) 일자 x 채널  (날짜는 한국시간 기준)
    ------------------------------------------------------------------
    insert into public.agg_channel_daily as a (
        bucket_date, channel_id, snapshot_count, sum_ccu, avg_ccu,
        peak_ccu, live_count, top_category_id, updated_at
    )
    select
        ((s.collected_at at time zone 'Asia/Seoul')::date)      as bucket_date,
        ls.channel_id,
        count(*)                                                as snapshot_count,
        sum(s.concurrent_user_count)                            as sum_ccu,
        round(avg(s.concurrent_user_count)::numeric, 2)         as avg_ccu,
        max(s.concurrent_user_count)                            as peak_ccu,
        count(distinct s.live_id)                               as live_count,
        -- 주력 카테고리 = 그날 동접이 가장 높았던 시점의 카테고리
        (array_agg(s.category_id order by s.concurrent_user_count desc)
            filter (where s.category_id is not null))[1]        as top_category_id,
        now()
    from public.live_snapshot s
    join public.live_session ls on ls.live_id = s.live_id
    -- 아래 두 조건은 같은 범위를 뜻합니다. 위쪽은 BRIN 인덱스로 빠르게 걸러내기 위한 것,
    -- 아래쪽은 한국시간 날짜 경계를 정확히 맞추기 위한 것입니다.
    where s.collected_at >= (v_day_from::timestamp at time zone 'Asia/Seoul')
      and (s.collected_at at time zone 'Asia/Seoul')::date >= v_day_from
    group by 1, 2
    on conflict (bucket_date, channel_id) do update set
        snapshot_count  = excluded.snapshot_count,
        sum_ccu         = excluded.sum_ccu,
        avg_ccu         = excluded.avg_ccu,
        peak_ccu        = excluded.peak_ccu,
        live_count      = excluded.live_count,
        top_category_id = excluded.top_category_id,
        updated_at      = now();
    get diagnostics v_days = row_count;

    ------------------------------------------------------------------
    -- 3) live_session 의 최고 동접 / 스냅샷 수 갱신
    --    최근 2시간 안에 잡힌 방송만 다시 계산합니다. 끝난 방송은 값이 고정되므로
    --    나중에 원본이 삭제돼도 방송 길이/최고 동접 기록은 영구히 남습니다.
    ------------------------------------------------------------------
    update public.live_session ls
    set peak_concurrent_user_count = agg.peak,
        snapshot_count             = agg.cnt
    from (
        select s.live_id,
               max(s.concurrent_user_count) as peak,
               count(*)                     as cnt
        from public.live_snapshot s
        where s.live_id in (
            select live_id from public.live_session
            where last_seen_at >= now() - interval '2 hours'
        )
        group by s.live_id
    ) agg
    where ls.live_id = agg.live_id
      and (ls.peak_concurrent_user_count is distinct from agg.peak
           or ls.snapshot_count is distinct from agg.cnt);

    ------------------------------------------------------------------
    -- 4) 보존 기간이 지난 원본 삭제
    ------------------------------------------------------------------
    delete from public.live_snapshot
    where collected_at < now() - make_interval(days => p_retention_days);
    get diagnostics v_del = row_count;

    return query select v_hours, v_days, v_del;
end $$;

comment on function public.run_rollup is
    '원본 스냅샷을 시간별/일별 집계로 굴리고, 보존기간 지난 원본을 삭제. 수집 스크립트가 매 회차 호출';


-- ============================================================================
--  확인용 뷰 : 지금 DB 가 어떤 상태인지 한눈에 보기
--  Supabase SQL Editor 에서  select * from v_pipeline_status;
-- ============================================================================
create or replace view public.v_pipeline_status as
select
    (select count(*) from public.live_snapshot)                       as 스냅샷_행수,
    (select count(*) from public.live_session)                        as 방송_수,
    (select count(*) from public.channel)                             as 채널_수,
    (select count(*) from public.category)                            as 카테고리_수,
    (select max(collected_at) from public.live_snapshot)              as 최근_수집시각,
    (select count(*) from public.collection_run
      where status = 'success' and started_at > now() - interval '24 hours') as 최근24h_성공,
    (select count(*) from public.collection_run
      where status in ('failed','partial') and started_at > now() - interval '24 hours') as 최근24h_실패,
    pg_size_pretty(pg_total_relation_size('public.live_snapshot'))    as 스냅샷_용량,
    pg_size_pretty(pg_database_size(current_database()))              as 전체_DB용량;


-- 파일 뒷부분에서 만든 함수/뷰에도 권한을 부여합니다.
grant execute on function public.run_rollup(integer, integer, integer) to service_role;
grant select  on public.v_pipeline_status to anon, authenticated, service_role;
grant select  on public.v_category        to anon, authenticated, service_role;
