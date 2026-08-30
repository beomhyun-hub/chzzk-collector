"""환경변수 로딩.

로컬에서는 .env 파일을, GitHub Actions 에서는 Secrets 로 주입된 환경변수를
읽습니다. .env 가 없으면 조용히 넘어가므로 코드를 나눠 쓸 필요가 없습니다.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(Exception):
    """필수 환경변수가 없거나 값이 잘못된 경우."""


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} 은 정수여야 합니다. 현재 값: {raw!r}")


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} 은 숫자여야 합니다. 현재 값: {raw!r}")


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} 이 설정되지 않았습니다.\n"
            f"  - 로컬이면 .env 파일에, GitHub Actions 면 Secrets 에 추가하세요."
        )
    if value.startswith("PASTE_"):
        raise ConfigError(f"{name} 이 자리표시자({value}) 그대로입니다. 실제 값을 넣어주세요.")
    if value[0] in "'\"":
        raise ConfigError(f"{name} 에 따옴표가 붙어 있습니다. 따옴표를 지워주세요.")
    return value


@dataclass(frozen=True)
class Config:
    chzzk_client_id: str
    chzzk_client_secret: str
    supabase_url: str
    supabase_key: str

    # 수집 범위
    min_concurrent_users: int = 10   # 이 값 미만이 나오면 페이지 순회 중단
    max_pages: int = 200             # 안전장치
    page_size: int = 20              # API 가 허용하는 최대값

    # 네트워크
    request_timeout: int = 10
    max_retries: int = 3
    page_delay_seconds: float = 0.3

    # 보존정책
    retention_days: int = 30

    # 카테고리 포스터 이미지를 한 회차에 몇 개까지 조회할지 (0 이면 끔)
    category_backfill_per_run: int = 20

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()  # .env 가 없으면 아무 일도 하지 않음 (Actions 환경)

        url = _required("SUPABASE_URL").rstrip("/")
        if url.endswith("/rest/v1"):
            url = url[: -len("/rest/v1")]

        key = _required("SUPABASE_SERVICE_KEY")
        if "publishable" in key or key.startswith("sb_publishable_"):
            raise ConfigError(
                "SUPABASE_SERVICE_KEY 에 publishable(공개) 키가 들어있습니다.\n"
                "  쓰기가 불가능합니다. Secret keys 의 sb_secret_... 또는 service_role 키를 쓰세요."
            )

        return cls(
            chzzk_client_id=_required("CHZZK_CLIENT_ID"),
            chzzk_client_secret=_required("CHZZK_CLIENT_SECRET"),
            supabase_url=url,
            supabase_key=key,
            min_concurrent_users=_int("MIN_CONCURRENT_USERS", 10),
            max_pages=_int("MAX_PAGES", 200),
            page_size=_int("PAGE_SIZE", 20),
            request_timeout=_int("REQUEST_TIMEOUT", 10),
            max_retries=_int("MAX_RETRIES", 3),
            page_delay_seconds=_float("PAGE_DELAY_SECONDS", 0.3),
            retention_days=_int("RETENTION_DAYS", 30),
            category_backfill_per_run=_int("CATEGORY_BACKFILL_PER_RUN", 20),
        )

    def summary(self) -> str:
        """로그용 요약. 비밀값은 절대 출력하지 않습니다."""
        return (
            f"컷오프 동접 {self.min_concurrent_users}명 미만 / "
            f"최대 {self.max_pages}페이지 / 페이지당 {self.page_size}건 / "
            f"타임아웃 {self.request_timeout}초 / 재시도 {self.max_retries}회 / "
            f"원본보관 {self.retention_days}일"
        )
