"""Central configuration. Everything is overridable through .env / environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _path(name: str, default: str) -> Path:
    raw = os.getenv(name, "").strip() or default
    p = Path(raw)
    return p if p.is_absolute() else (BASE_DIR / p)


VALID_MODES = ("dry_run", "review", "auto")


VALID_PROVIDERS = ("anthropic", "openrouter")

# Free OpenRouter models that advertise json_schema structured output. Anything
# else on the free tier tends to return prose around the JSON, which the
# planner's salvage path can usually recover but should not have to.
OPENROUTER_DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"


@dataclass(frozen=True)
class Settings:
    provider: str
    api_key: str | None
    model: str
    effort: str
    server_fallbacks: bool

    openrouter_api_key: str | None
    openrouter_model: str
    openrouter_base_url: str
    openrouter_app_title: str

    field_batch: int
    max_output_tokens: int
    request_timeout_s: float

    headless: bool
    storage_state: Path
    nav_timeout_ms: int
    slow_mo_ms: int
    browser_executable: str | None
    no_sandbox: bool

    auth_user: str
    auth_password: str | None

    default_mode: str
    concurrency: int
    min_confidence: float

    db_path: Path
    profile_path: Path
    screenshot_dir: Path
    base_dir: Path


def load_settings() -> Settings:
    mode = (os.getenv("JAA_DEFAULT_MODE", "review") or "review").strip()
    if mode not in VALID_MODES:
        mode = "review"
    anthropic_key = os.getenv("ANTHROPIC_API_KEY") or None
    openrouter_key = os.getenv("OPENROUTER_API_KEY") or None

    provider = (os.getenv("JAA_PROVIDER", "") or "").strip().lower()
    if provider not in VALID_PROVIDERS:
        # Infer from whichever key is present; Anthropic wins if both are.
        provider = "anthropic" if anthropic_key else ("openrouter" if openrouter_key else "anthropic")

    # Free models are smaller and less reliable, so hand them fewer fields at a
    # time; a focused prompt beats one long one on a weak model.
    default_batch = 18 if provider == "openrouter" else 0

    s = Settings(
        provider=provider,
        api_key=anthropic_key,
        model=os.getenv("JAA_MODEL", "claude-opus-5").strip() or "claude-opus-5",
        effort=(os.getenv("JAA_EFFORT", "high") or "high").strip(),
        server_fallbacks=_bool("JAA_SERVER_FALLBACKS", True),
        openrouter_api_key=openrouter_key,
        openrouter_model=(
            os.getenv("JAA_OPENROUTER_MODEL", "").strip() or OPENROUTER_DEFAULT_MODEL
        ),
        openrouter_base_url=(
            os.getenv("JAA_OPENROUTER_BASE_URL", "").strip() or "https://openrouter.ai/api/v1"
        ),
        openrouter_app_title=(
            os.getenv("JAA_OPENROUTER_APP_TITLE", "").strip() or "Job Application Agent"
        ),
        field_batch=max(0, _int("JAA_FIELD_BATCH", default_batch)),
        max_output_tokens=_int("JAA_MAX_OUTPUT_TOKENS", 16000 if provider == "anthropic" else 8000),
        request_timeout_s=_float("JAA_REQUEST_TIMEOUT_S", 300.0),
        headless=_bool("JAA_HEADLESS", False),
        storage_state=_path("JAA_STORAGE_STATE", "data/storage_state.json"),
        nav_timeout_ms=_int("JAA_NAV_TIMEOUT_MS", 45000),
        slow_mo_ms=_int("JAA_SLOW_MO_MS", 0),
        browser_executable=(os.getenv("JAA_BROWSER_EXECUTABLE", "").strip() or None),
        no_sandbox=_bool("JAA_NO_SANDBOX", False),
        auth_user=(os.getenv("JAA_AUTH_USER", "").strip() or "admin"),
        auth_password=(os.getenv("JAA_AUTH_PASSWORD", "").strip() or None),
        default_mode=mode,
        concurrency=max(1, _int("JAA_CONCURRENCY", 2)),
        min_confidence=_float("JAA_MIN_CONFIDENCE", 0.35),
        db_path=_path("JAA_DB_PATH", "data/job_agent.duckdb"),
        profile_path=_path("JAA_PROFILE_PATH", "config/profile.yaml"),
        screenshot_dir=_path("JAA_SCREENSHOT_DIR", "data/screenshots"),
        base_dir=BASE_DIR,
    )
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    s.screenshot_dir.mkdir(parents=True, exist_ok=True)
    return s


settings = load_settings()
