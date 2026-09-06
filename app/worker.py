"""One-reel pipeline: discover -> filter -> claim -> download -> cover -> upload -> complete.

Crash-safety order: claim (PROCESSING) -> download -> upload -> obtain
destination pk -> mark COMPLETED. Never COMPLETED before Instagram confirms.
Uncertain upload outcomes are NOT blindly retried.
"""

from __future__ import annotations

import logging
import time

from app.database import repository as repo
from app.instagram import covers as cover_mod
from app.instagram import downloader, uploader
from app.instagram.discovery import filter_candidates

log = logging.getLogger(__name__)


def run_once(*, settings, db, adapter) -> dict:
    t0 = time.time()
    run_id = repo.start_run(db)
    counters = dict(reels_found=0, reels_skipped=0, reels_attempted=0,
                    reels_uploaded=0, reels_failed=0)
    try:
        dest = settings.DESTINATION_USERNAME or settings.INSTAGRAM_USERNAME
        if not dest:
            raise RuntimeError("DESTINATION_USERNAME (or INSTAGRAM_USERNAME) is not configured")

        candidates = adapter.get_reels(count=settings.REEL_FETCH_COUNT)
        counters["reels_found"] = len(candidates)

        pick, stats = filter_candidates(
            candidates,
            already_done=lambda mid: repo.is_completed(db, mid, dest),
        )
        counters["reels_skipped"] = stats["skipped"]
        if pick is None:
            repo.finish_run(db, run_id, "no_new_reel", "", **counters)
            return {"status": "no_new_reel", **counters}

        claimed, reason = repo.try_claim_reel(
            db, source_media_id=pick.source_media_id, shortcode=pick.shortcode,
            username=pick.username, destination_account=dest,
            stale_sec=settings.STALE_CLAIM_TIMEOUT_SEC,
        )
        log.info("CLAIM media_id=%s result=%s", pick.source_media_id, reason)
        if not claimed:
            repo.finish_run(db, run_id, "no_new_reel",
                            f"claim refused: {reason}", **counters)
            return {"status": "no_new_reel", "reason": reason, **counters}

        counters["reels_attempted"] = 1
        video_path = cover_path = None
        cover_label = ""
        try:
            pick = adapter.ensure_video_url(pick)
            if not pick.video_url:
                raise RuntimeError("source reel is not downloadable (no video_url)")
            repo.mark_status(db, pick.source_media_id, dest, "DOWNLOADED")
            video_path = downloader.download_to_tmp(
                pick.video_url, timeout=settings.HTTP_TIMEOUT_SEC,
                max_bytes=settings.MAX_VIDEO_BYTES)

            cover = cover_mod.select_cover(
                mode=settings.COVER_MODE, cover_dir=settings.COVER_DIR,
                fixed_file=settings.COVER_FILE, db=db)
            cover_mod.validate_cover(cover)
            cover_path, cover_label = str(cover), str(cover)
            log.info("COVER selected=%s", cover_label)

            caption = (settings.REEL_CAPTION or "").replace(
                "{username}", pick.username or "instagram").replace(
                "{shortcode}", pick.shortcode or "")
            dest_pk = uploader.upload_reel(
                adapter, video_path=video_path, cover_path=cover_path,
                caption=caption, duration=pick.duration,
                hide_counts=settings.HIDE_LIKE_VIEW_COUNTS)
            repo.mark_status(db, pick.source_media_id, dest, "COMPLETED",
                             destination_media_id=dest_pk)
            counters["reels_uploaded"] = 1
            log.info("DATABASE completed media_id=%s dest=%s", pick.source_media_id, dest_pk)
            repo.finish_run(db, run_id, "success", "", **counters)
            return {
                "status": "success",
                "source_media_id": pick.source_media_id,
                "source_shortcode": pick.shortcode,
                "destination_media_id": dest_pk,
                "cover": cover_label,
                "elapsed_sec": round(time.time() - t0, 1),
            }
        except Exception as exc:  # noqa: BLE001 - must record + cleanup
            kind = adapter.classify_error(exc) if hasattr(adapter, "classify_error") else "unknown"
            log.error("PIPELINE failed media_id=%s kind=%s err=%r",
                      pick.source_media_id, kind, exc)
            repo.mark_status(db, pick.source_media_id, dest, "FAILED", error=f"{kind}: {exc}")
            counters["reels_failed"] = 1
            repo.finish_run(db, run_id, "failed", f"{kind}: {exc}", **counters)
            return {"status": "failed", "error": f"{kind}: {exc}",
                    "source_media_id": pick.source_media_id}
        finally:
            downloader.cleanup(video_path)
    except Exception as exc:  # noqa: BLE001 - feed/auth-level failure
        log.error("PIPELINE run failed: %r", exc)
        try:
            repo.finish_run(db, run_id, "failed", str(exc), **counters)
        except Exception:
            pass
        return {"status": "failed", "error": str(exc)[:500]}
