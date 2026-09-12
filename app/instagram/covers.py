"""Cover selection from /cover/ repo assets or a cached URL cover. No local state files."""

from __future__ import annotations

import hashlib
import logging
import random
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

CACHE_DIR = Path("/tmp/covers")
URL_MAP: dict[str, Path] = {}

MAX_COVER_BYTES = 10 * 1024 * 1024  # ~10MB cap for URL covers

log = logging.getLogger(__name__)


def _ext_from_url(url: str) -> str:
    """Extension from URL path; default .jpg, only allow known image exts."""
    try:
        suffix = Path(urlparse(url).path).suffix.lower()
    except Exception:
        suffix = ""
    if suffix in ALLOWED_EXTS:
        return suffix
    return ".jpg"


def cover_from_url(url: str) -> Path:
    """Download a cover image once, cache in /tmp/covers keyed by URL hash.

    Same URL requested again returns the cached file without re-downloading
    (download once, keep until env changes = different URL = different hash).
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("Empty cover URL")
    ext = _ext_from_url(url)
    name = hashlib.sha256(url.encode("utf-8")).hexdigest() + ext
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = CACHE_DIR / name

    # In-memory fast path + on-disk cache: reuse if present and non-empty.
    cached = URL_MAP.get(url)
    if cached is not None and cached.is_file() and cached.stat().st_size > 0:
        return cached
    if dest.is_file() and dest.stat().st_size > 0:
        URL_MAP[url] = dest
        log.info("COVER cache-hit path=%s url=%.80s", dest, url)
        return dest

    size = 0
    try:
        import httpx

        with httpx.stream("GET", url, timeout=60,
                          headers={"User-Agent": "Mozilla/5.0"},
                          follow_redirects=True) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes(65536):
                    size += len(chunk)
                    if size > MAX_COVER_BYTES:
                        raise ValueError(
                            f"Cover exceeds {MAX_COVER_BYTES} bytes cap")
                    fh.write(chunk)
    except ImportError:
        # Fallback when httpx is unavailable: stdlib urllib.
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            with open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_COVER_BYTES:
                        raise ValueError(
                            f"Cover exceeds {MAX_COVER_BYTES} bytes cap")
                    fh.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise

    if dest.suffix.lower() not in ALLOWED_EXTS:
        dest.unlink(missing_ok=True)
        raise ValueError(f"Unsupported cover extension: {dest.suffix}")
    if dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise ValueError(f"Cover is empty: {dest}")
    URL_MAP[url] = dest
    log.info("COVER downloaded path=%s bytes=%d url=%.80s", dest, size, url)
    return dest


def discover_covers(cover_dir: str | Path) -> list[Path]:
    root = Path(cover_dir)
    if not root.is_dir():
        return []
    files = sorted(
        p for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in ALLOWED_EXTS
    )
    return files


def select_cover(*, mode: str, cover_dir: str | Path, fixed_file: str = "",
                 db=None, total_hint: int = 0, cover_url: str = "") -> Path:
    """Return a cover path. sequential mode uses Turso counter (crash-safe).

    If cover_url is non-empty it takes precedence (URL mode): the image is
    downloaded once and cached in /tmp/covers until the URL changes.
    """
    if cover_url and cover_url.strip():
        path = cover_from_url(cover_url.strip())
        log.info("COVER selected=%s mode=url", path)
        return path
    covers = discover_covers(cover_dir)
    if not covers:
        raise FileNotFoundError(f"No cover images (*{sorted(ALLOWED_EXTS)}) in {cover_dir}/")
    mode = (mode or "random").lower()
    if mode == "fixed":
        if not fixed_file:
            raise ValueError("COVER_FILE must be set when COVER_MODE=fixed")
        candidate = Path(fixed_file)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        if not (candidate.is_file() and candidate.suffix.lower() in ALLOWED_EXTS):
            raise ValueError(f"Invalid COVER_FILE: {fixed_file}")
        log.info("COVER selected=%s mode=fixed", candidate)
        return candidate
    if mode == "sequential":
        from app.database import repository as repo
        idx = repo.next_cover_index(db, len(covers)) if db is not None else 0
        chosen = covers[idx % len(covers)]
        log.info("COVER selected=%s mode=sequential idx=%d", chosen, idx)
        return chosen
    chosen = random.choice(covers)  # random (default)
    log.info("COVER selected=%s mode=random", chosen)
    return chosen


def validate_cover(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"Cover not found: {path}")
    if path.suffix.lower() not in ALLOWED_EXTS:
        raise ValueError(f"Unsupported cover extension: {path.suffix}")
    if path.stat().st_size == 0:
        raise ValueError(f"Cover is empty: {path}")
