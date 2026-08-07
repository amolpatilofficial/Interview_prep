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


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    model: str
    effort: str
    server_fallbacks: bool

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
    s = Settings(
        api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        model=os.getenv("JAA_MODEL", "claude-opus-5").strip() or "claude-opus-5",
        effort=(os.getenv("JAA_EFFORT", "high") or "high").strip(),
        server_fallbacks=_bool("JAA_SERVER_FALLBACKS", True),
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
