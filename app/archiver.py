"""Auto-archive pass: COMPLETED uploads older than min_age whose live
view count is below max_views get archived (owner-only, reversible).

One /archive hit = one pass over all eligible rows. Thresholds come from
query params (defaults from Settings) so UptimeRobot needs no config.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from app import activity
from app.database import repository as repo

log = logging.getLogger(__name__)


def run_archive(*, settings, db, adapter,
                min_age_hr: int = 24, max_views: int = 900) -> dict:
    started = time.time()
    try:
        min_age_hr = max(int(min_age_hr), 1)
    except (TypeError, ValueError):
        min_age_hr = 24
    try:
        max_views = max(int(max_views), 0)
    except (TypeError, ValueError):
        max_views = 900
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=min_age_hr)).isoformat()
    dest = settings.DESTINATION_USERNAME or settings.INSTAGRAM_USERNAME or ""

    candidates = repo.archive_candidates(
        db, destination_account=dest, older_than_iso=cutoff)
    activity.emit(f"ARCHIVE pass start: {len(candidates)} candidate(s)"
                  f" older than {min_age_hr}h, threshold {max_views} views")
    log.info("ARCHIVE pass start candidates=%d min_age_hr=%d max_views=%d",
             len(candidates), min_age_hr, max_views)

    archived: list[dict] = []
    skipped: list[dict] = []
    for cand in candidates:
        pk = str(cand.get("destination_media_id") or "")
        src = str(cand.get("source_media_id") or "")
        if not pk:
            continue
        views = adapter.get_media_views(pk)
        if views is None:
            activity.emit(f"ARCHIVE skip media={pk}: views unreadable")
            skipped.append({"destination_media_id": pk, "reason": "views_unknown"})
            continue
        if views >= max_views:
            activity.emit(f"ARCHIVE skip media={pk}: {views} views >= {max_views}")
            skipped.append({"destination_media_id": pk, "views": views,
                            "reason": "popular_enough"})
            continue
        try:
            adapter.archive_media(pk)
        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:200]}"
            activity.emit(f"ARCHIVE failed media={pk}: {err}")
            log.warning("ARCHIVE failed media=%s: %r", pk, exc)
            skipped.append({"destination_media_id": pk, "views": views,
                            "reason": "archive_failed", "error": err})
            continue
        repo.mark_archived(db, src, dest)
        activity.emit(f"ARCHIVE done media={pk} ({views} views)")
        archived.append({"destination_media_id": pk, "views": views,
                         "source_media_id": src})
        time.sleep(2)  # gentle pace between archive calls

    elapsed = round(time.time() - started, 1)
    result = {"status": "ok", "checked": len(candidates),
              "archived_count": len(archived), "archived": archived,
              "skipped": skipped, "min_age_hr": min_age_hr,
              "max_views": max_views, "elapsed_sec": elapsed}
    activity.emit(f"ARCHIVE pass done: {len(archived)} archived,"
                  f" {len(skipped)} skipped ({elapsed}s)")
    log.info("ARCHIVE pass done archived=%d skipped=%d elapsed=%ss",
             len(archived), len(skipped), elapsed)
    return result
