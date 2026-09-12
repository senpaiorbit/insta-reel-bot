"""Upload wrapper: transcode-aware Reel publishing + like-count hiding
+ share-to-feed.

1. HIDE COUNTS (supported): Instagram's ``configure_to_clips`` accepts
   ``like_and_view_counts_disabled=1`` (same wire parameter used by
   instagrapi/goinsta/instagram4j for all media configure calls). We set it
   on our own configure POST when requested.

2. Transcode retries: ``post_reel()`` sleeps a fixed 3s between video upload
   and ``configure_to_clips``, which fails with "Transcode not finished yet"
   for larger videos (and leaves a phantom media pk). We replicate the
   library's exact upload steps (same endpoints/params) but retry ONLY the
   cheap configure call with backoff, reusing the same upload_id. No
   re-upload, no duplicates.

3. SHARE TO FEED (supported): ``configure_to_clips`` accepts
   ``clips_share_preview_to_feed=1`` (same wire parameter instagrapi sends
   for ``clip_upload(show_preview_in_feed=True)``). Without it Instagram
   defaults to Reels-tab-only, so the reel never appears in the profile
   grid / post count. We set "1" by default (SHARE_TO_FEED=True); "0"
   opts out back to Reels-tab-only.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

HIDE_COUNTS_SUPPORTED = True  # via like_and_view_counts_disabled on configure
SHARE_TO_FEED_SUPPORTED = True  # via clips_share_preview_to_feed on configure

# Configure retry schedule after "Transcode not finished yet" (seconds).
CONFIGURE_RETRIES = (45, 90, 120, 180)


def _configure(handle, upload_id: str, caption: str, hide_counts: bool,
               share_to_feed: bool = True) -> dict:
    data = {
        "upload_id": upload_id,
        "caption": caption,
        "source_type": "4",
        "clips_uses_original_audio": "1",
        # "1" = also share preview to feed/profile grid (post count goes
        # up); "0" = Reels tab only. Same wire param instagrapi sends for
        # clip_upload(show_preview_in_feed=...).
        "clips_share_preview_to_feed": "1" if share_to_feed else "0",
    }
    if hide_counts:
        data["like_and_view_counts_disabled"] = "1"
    return handle._client.post(
        "/media/configure_to_clips/",
        data=data,
        rate_category="post_default",
    )


def upload_reel(adapter, *, video_path: str, cover_path: str | None,
                caption: str, duration: float, hide_counts: bool,
                share_to_feed: bool = True) -> str:
    if hide_counts:
        log.info("HIDE_COUNTS requested — setting like_and_view_counts_disabled=1")
    else:
        log.info("HIDE_COUNTS skipped (hide_like=0)")
    if share_to_feed:
        log.info("SHARE_TO_FEED requested — setting clips_share_preview_to_feed=1")
    else:
        log.info("SHARE_TO_FEED skipped — clips_share_preview_to_feed=0 (Reels tab only)")
    upload_api = adapter._ig.upload
    with open(video_path, "rb") as fh:
        video_data = fh.read()
    thumb_data = None
    if cover_path:
        with open(cover_path, "rb") as fh:
            thumb_data = fh.read()

    upload_id = upload_api._generate_upload_id()
    log.info("UPLOAD binary video=%s bytes=%d cover=%s",
             video_path, len(video_data), cover_path)
    upload_api._upload_video(video_data, upload_id, float(duration or 0),
                             1080, 1920, True)
    if thumb_data:
        try:
            upload_api._upload_photo(thumb_data, upload_id)
        except Exception as exc:
            log.warning("UPLOAD thumbnail ignored: %r", exc)

    last_err = "no configure attempt made"
    waits = (3,) + CONFIGURE_RETRIES
    for attempt, wait in enumerate(waits):
        time.sleep(wait)
        log.info("UPLOAD configure attempt=%d upload_id=%s", attempt + 1, upload_id)
        try:
            result = _configure(upload_api, upload_id, caption or "", hide_counts,
                                share_to_feed=share_to_feed)
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            log.warning("UPLOAD configure raised (attempt %d): %s", attempt + 1, last_err)
            continue
        if isinstance(result, dict) and result.get("media"):
            pk = adapter._extract_media_pk(result)
            if pk:
                log.info("UPLOAD success destination_media_id=%s", pk)
                return pk
        msg = str(result)[:300] if isinstance(result, dict) else str(result)[:300]
        last_err = msg
        if "transcode" in msg.lower():
            log.warning("UPLOAD transcode pending (attempt %d), waiting ...", attempt + 1)
            continue
        raise RuntimeError(f"Reel configure rejected: {msg}")
    raise RuntimeError(f"Reel configure never finished transcoding: {last_err[:300]}")
