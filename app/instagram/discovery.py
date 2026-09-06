"""Candidate filtering — modular, testable, no I/O."""

from __future__ import annotations

import logging

from app.instagram.adapter import ReelCandidate, is_reel_video

log = logging.getLogger(__name__)


def filter_candidates(candidates: list[ReelCandidate], *,
                      already_done,
                      max_duration_sec: float = 0,
                      min_duration_sec: float = 0,
                      blacklist: set[str] | None = None) -> tuple[ReelCandidate | None, dict]:
    """Walk the whole batch (never stop after N skips). Returns (pick, stats)."""
    stats = {"total": len(candidates), "skipped": 0, "reasons": {}}

    def skip(cand_id: str, reason: str):
        stats["skipped"] += 1
        stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
        log.info("SKIP media_id=%s reason=%s", cand_id or "?", reason)

    for cand in candidates:
        cid = cand.source_media_id
        if not cid:
            skip("?", "no_media_id")
            continue
        if not is_reel_video(cand):
            skip(cid, "not_reel_video")
            continue
        if blacklist and (cid in blacklist or cand.shortcode in blacklist):
            skip(cid, "blacklisted")
            continue
        if max_duration_sec and cand.duration and cand.duration > max_duration_sec:
            skip(cid, "too_long")
            continue
        if min_duration_sec and cand.duration and cand.duration < min_duration_sec:
            skip(cid, "too_short")
            continue
        if already_done(cid):
            skip(cid, "duplicate")
            continue
        if not cand.video_url and not cand.shortcode:
            skip(cid, "not_downloadable")
            continue
        return cand, stats
    return None, stats
