"""One-reel pipeline: discover -> filter -> claim -> download -> cover -> upload -> complete.

Crash-safety order: claim (PROCESSING) -> download -> upload -> obtain
destination pk -> mark COMPLETED. Never COMPLETED before Instagram confirms.
Uncertain upload outcomes are NOT blindly retried.

Resilience: walks ALL eligible candidates in feed order. A pick that proves
undownloadable is marked FAILED (retryable later) and the next candidate is
tried, instead of failing the whole run on the first pick.

Publishing: the source Reel's original caption is copied verbatim (template
fallback when empty); like/view counts are hidden unless disabled.
"""

from __future__ import annotations

import logging
import time

from app import activity
from app.database import repository as repo
from app.instagram import covers as cover_mod
from app.instagram import downloader, uploader
from app.instagram.discovery import filter_candidates

log = logging.getLogger(__name__)

# Max per-run video-URL resolutions (each costs Instagram API calls).
MAX_RESOLVE_ATTEMPTS = 5


def resolve_caption(pick, template: str) -> tuple[str, bool]:
    """Use the source Reel's original caption; fall back to the template.

    Returns (caption, copied_from_source).
    """
    original = (getattr(pick, "caption_text", "") or "").strip()
    if original:
        return original[:2200], True
    rendered = (template or "").replace(
        "{username}", getattr(pick, "username", "") or "instagram").replace(
        "{shortcode}", getattr(pick, "shortcode", "") or "")
    return rendered, False


def run_once(*, settings, db, adapter, hide_counts: bool | None = None,
             cover_url_override: str | None = None) -> dict:
    t0 = time.time()
    run_id = repo.start_run(db)
    counters = dict(reels_found=0, reels_skipped=0, reels_attempted=0,
                    reels_uploaded=0, reels_failed=0)
    hide = settings.HIDE_LIKE_VIEW_COUNTS if hide_counts is None else hide_counts
    try:
        dest = settings.DESTINATION_USERNAME or settings.INSTAGRAM_USERNAME
        if not dest:
            raise RuntimeError("DESTINATION_USERNAME (or INSTAGRAM_USERNAME) is not configured")

        candidates = adapter.get_reels(count=settings.REEL_FETCH_COUNT)
        counters["reels_found"] = len(candidates)
        activity.emit(f"DISCOVERY found={len(candidates)}")

        eligible, stats = filter_candidates(
            candidates,
            already_done=lambda mid: repo.is_completed(db, mid, dest),
        )
        counters["reels_skipped"] = stats["skipped"]
        if not eligible:
            activity.emit(f"NO_NEW_REEL skipped={stats['skipped']}")
            repo.finish_run(db, run_id, "no_new_reel", "", **counters)
            return {"status": "no_new_reel", **counters}

        resolved_tried = 0
        last_error = ""
        for pick in eligible:
            claimed, reason = repo.try_claim_reel(
                db, source_media_id=pick.source_media_id, shortcode=pick.shortcode,
                username=pick.username, destination_account=dest,
                stale_sec=settings.STALE_CLAIM_TIMEOUT_SEC,
            )
            log.info("CLAIM media_id=%s result=%s", pick.source_media_id, reason)
            activity.emit(f"CLAIM media_id={pick.source_media_id} result={reason}")
            if not claimed:
                continue

            counters["reels_attempted"] += 1
            resolved_tried += 1
            pick = adapter.ensure_video_url(pick)
            if not pick.video_url:
                last_error = "source reel is not downloadable (no video_url)"
                log.warning("RESOLVE failed media_id=%s", pick.source_media_id)
                activity.emit(f"RESOLVE failed media_id={pick.source_media_id}")
                repo.mark_status(db, pick.source_media_id, dest, "FAILED",
                                 error=last_error)
                counters["reels_failed"] += 1
                if resolved_tried >= MAX_RESOLVE_ATTEMPTS:
                    break
                continue  # try next eligible candidate
            result = _upload_picked(db=db, adapter=adapter, settings=settings,
                                    dest=dest, pick=pick, counters=counters,
                                    run_id=run_id, t0=t0, hide_counts=hide,
                                    cover_url_override=cover_url_override)
            return result

        repo.finish_run(db, run_id, "no_new_reel" if not last_error else "failed",
                        last_error, **counters)
        if last_error:
            return {"status": "failed", "error": last_error}
        return {"status": "no_new_reel", **counters}
    except Exception as exc:  # noqa: BLE001 - feed/auth-level failure
        log.error("PIPELINE run failed: %r", exc)
        try:
            repo.finish_run(db, run_id, "failed", str(exc), **counters)
        except Exception:
            pass
        return {"status": "failed", "error": str(exc)[:500]}


def _upload_picked(*, db, adapter, settings, dest, pick, counters,
                   run_id: int, t0: float, hide_counts: bool,
                   cover_url_override: str | None = None) -> dict:
    """Download -> cover -> upload -> COMPLETED for a resolved candidate."""
    video_path = None
    cover_label = ""
    try:
        repo.mark_status(db, pick.source_media_id, dest, "DOWNLOADED")
        video_path = downloader.download_to_tmp(
            pick.video_url, timeout=settings.HTTP_TIMEOUT_SEC,
            max_bytes=settings.MAX_VIDEO_BYTES)
        activity.emit(f"DOWNLOADED media_id={pick.source_media_id}")

        # COVER_URL env (or per-upload ?cover_url= override) wins over
        # COVER_MODE when set; downloaded once and cached until URL changes.
        effective_cover_url = (cover_url_override
                               if cover_url_override is not None
                               else getattr(settings, "COVER_URL", ""))
        cover = cover_mod.select_cover(
            mode=settings.COVER_MODE, cover_dir=settings.COVER_DIR,
            fixed_file=settings.COVER_FILE, db=db,
            cover_url=effective_cover_url or "")
        cover_mod.validate_cover(cover)
        cover_label = str(cover)
        log.info("COVER selected=%s", cover_label)
        activity.emit(f"COVER selected={cover_label}")

        caption, copied = resolve_caption(pick, settings.REEL_CAPTION)
        activity.emit(f"CAPTION {'copied original' if copied else 'template fallback'}")
        # SHARE_TO_FEED (default True): reel preview also appears in the
        # profile grid / post count. False = Reels tab only.
        share = getattr(settings, "SHARE_TO_FEED", True)
        if share is None:
            share = True
        dest_pk = uploader.upload_reel(
            adapter, video_path=video_path, cover_path=cover_label,
            caption=caption, duration=pick.duration,
            hide_counts=hide_counts, share_to_feed=bool(share))
        repo.mark_status(db, pick.source_media_id, dest, "COMPLETED",
                         destination_media_id=dest_pk)
        counters["reels_uploaded"] = 1
        log.info("DATABASE completed media_id=%s dest=%s", pick.source_media_id, dest_pk)
        activity.emit(f"UPLOAD success destination_media_id={dest_pk}")
        activity.emit(f"DATABASE completed media_id={pick.source_media_id}")
        repo.finish_run(db, run_id, "success", "", **counters)
        return {
            "status": "success",
            "source_media_id": pick.source_media_id,
            "source_shortcode": pick.shortcode,
            "destination_media_id": dest_pk,
            "cover": cover_label,
            "caption_copied": copied,
            "like_hidden": hide_counts,
            "shared_to_feed": bool(share),
            "elapsed_sec": round(time.time() - t0, 1),
        }
    except Exception as exc:  # noqa: BLE001 - must record + cleanup
        kind = adapter.classify_error(exc) if hasattr(adapter, "classify_error") else "unknown"
        log.error("PIPELINE failed media_id=%s kind=%s err=%r",
                  pick.source_media_id, kind, exc)
        activity.emit(f"FAILED media_id={pick.source_media_id} kind={kind}")
        repo.mark_status(db, pick.source_media_id, dest, "FAILED", error=f"{kind}: {exc}")
        counters["reels_failed"] = 1
        repo.finish_run(db, run_id, "failed", f"{kind}: {exc}", **counters)
        return {"status": "failed", "error": f"{kind}: {exc}",
                "source_media_id": pick.source_media_id}
    finally:
        downloader.cleanup(video_path)
