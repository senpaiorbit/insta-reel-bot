"""Upload wrapper: honest feature detection for hidden like/view counts.

Verified from instaharvest-v2 docs: ``post_reel()`` accepts only
(video_path/video_data, thumbnail_path/thumbnail_data, caption, duration,
width, height). There is NO hide-likes / like-and-view-counts parameter, so
HIDE_LIKE_VIEW_COUNTS cannot be applied through this library. We log a clear
warning and never claim otherwise.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def upload_reel(adapter, *, video_path: str, cover_path: str | None,
                caption: str, duration: float, hide_counts: bool) -> str:
    if hide_counts:
        log.warning(
            "HIDE_LIKE_VIEW_COUNTS requested but UNSUPPORTED by instaharvest-v2 "
            "post_reel() — uploading with counts visible. See README 'Hidden counts'."
        )
    dest_pk = adapter.upload_reel(
        video_path=video_path, cover_path=cover_path,
        caption=caption, duration=duration,
    )
    return dest_pk
