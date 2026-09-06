"""Cover selection from /cover/ repo assets. No local state files."""

from __future__ import annotations

import logging
import random
from pathlib import Path

ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

log = logging.getLogger(__name__)


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
                 db=None, total_hint: int = 0) -> Path:
    """Return a cover path. sequential mode uses Turso counter (crash-safe)."""
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
