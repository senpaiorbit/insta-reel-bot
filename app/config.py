"""Central configuration. All secrets come from environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Instagram
    INSTAGRAM_USERNAME: str = ""
    INSTAGRAM_PASSWORD: str = ""  # only for first-time login, then use session
    INSTAGRAM_SESSION: str = ""  # raw or base64 session.json content, or cookie string
    SESSION_ID: str = ""
    CSRF_TOKEN: str = ""
    DS_USER_ID: str = ""
    DESTINATION_USERNAME: str = ""

    # Turso (ONLY persistent store)
    TURSO_DATABASE_URL: str = ""
    TURSO_AUTH_TOKEN: str = ""

    # API protection (UptimeRobot -> POST /upload)
    UPLOAD_SECRET: str = "change-me"

    # Pipeline tuning
    REEL_FETCH_COUNT: int = 30
    MAX_UPLOADS_PER_RUN: int = 1
    STALE_CLAIM_TIMEOUT_SEC: int = 1800  # reclaim PROCESSING rows older than this
    UPLOAD_LOCK_TIMEOUT_SEC: int = 600  # distributed /upload lock TTL
    HTTP_TIMEOUT_SEC: int = 60
    MAX_VIDEO_BYTES: int = 100 * 1024 * 1024  # 100 MB safety cap

    # Covers (/cover/ repo assets)
    COVER_MODE: str = "random"  # random | sequential | fixed
    COVER_FILE: str = ""  # e.g. cover/1.png when COVER_MODE=fixed
    COVER_DIR: str = "cover"
    COVER_URL: str = ""  # e.g. https://i.ibb.co/sp6WvzJK/1.jpg - downloaded once, cached until URL changes

    # Publishing
    HIDE_LIKE_VIEW_COUNTS: bool = True  # honored only if library supports it (it doesn't)
    SHARE_TO_FEED: bool = True  # also share reel preview to profile grid/feed (False = Reels tab only)
    REEL_CAPTION: str = "🎬 via @{username} #reels"

    # Auto-archive (/archive hit every 24h): reels older than this, with
    # fewer views than this, get archived (owner-only, reversible in-app).
    ARCHIVE_MIN_AGE_HR: int = 24
    ARCHIVE_MAX_VIEWS: int = 900

    # Proxy (optional): empty means direct connection.
    PROXY_URL: str = ""  # e.g. http://user:pass@host:port or socks5h://host:port — empty means direct

    # Telegram logs (optional, off by default).
    TELEGRAM_ENABLED: bool = False
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    LOG_LEVEL: str = "INFO"
    BASE_DIR: str = str(Path(__file__).resolve().parent.parent)


@lru_cache
def get_settings() -> Settings:
    return Settings()
