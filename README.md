# Instagram Reel Automation Bot

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/senpaiorbit/insta-reel-bot)

Production-ready Python service. Three UptimeRobot monitors drive it: a
5-minute `/ping` keep-alive (beats Render Free sleep), a scheduled
`/upload` that posts one new Reel per hit, and a daily `/archive` that
archives flops (older than 24h, under 900 views).

## One-click deploy

1. Click **Deploy to Render** above.
2. Connect your GitHub account and select this repo.
3. Build `pip install -r requirements.txt` + start `uvicorn app.main:app --host 0.0.0.0 --port $PORT` are auto-filled from `render.yaml`.
4. Fill `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `INSTAGRAM_SESSION`, `DESTINATION_USERNAME`, `UPLOAD_SECRET`, `COVER_URL`.
5. Click **Deploy**.

## Cover via URL (no fork needed)

- Set the `COVER_URL` env (e.g. `https://i.ibb.co/sp6WvzJK/1.jpg`) — it
  overrides `COVER_MODE` when set — or pass a per-upload
  `?cover_url=` query param: `/upload?token=<SECRET>&cover_url=https://i.ibb.co/sp6WvzJK/1.jpg`.
- The image is downloaded once and cached in `/tmp/covers` (filename =
  `sha256(url)` + ext) until the URL changes (a new URL = new hash = new file).
- Allowed extensions: `.png/.jpg/.jpeg/.webp` (URL path ext, defaults to `.jpg`).

## Verified InstaHarvest v2 API surface (don't guess — this is checked)

| Concern | Real API (`pip install instaharvest-v2`) |
|---|---|
| Import / client | `from instaharvest_v2 import Instagram`; `ig = Instagram.from_env()` / `Instagram()` |
| Login / session | `ig.login(u, p)`, `ig.save_session()` / `ig.auth.save_session/load_session(path)`, `ig.auth.validate_session()`, `Instagram.from_session_file(path)` |
| Reels feed | `ig.feed.get_reels_feed(count=20, cursor=None)` → `{posts, has_next, end_cursor, count}` — verified against installed source `api/feed.py` (the docs' `max_id` kwarg does NOT exist) |
| NOT used | `get_timeline()` (home feed, not the Reels feed) |
| Media fields | `Media` has NO `video_url` — video lives in `video_versions` / `best_video_url` (verified against installed `models/media.py`); `play_count`/`view_count` carry views |
| Upload | `ig.upload.post_reel(...)` sleeps a fixed 3s before configure — too short; we replicate its exact steps with configure-only retries |
| Hide counts | ✅ `like_and_view_counts_disabled=1` on `configure_to_clips` (same wire param as instagrapi/goinsta) |
| Cover | ✅ supported via `thumbnail_path` on upload |
| Archive | ⚠️ `MediaAPI` has NO archive method (checked 1.1.x) — we call its `HttpClient` directly: `POST media/{id}/only_me/` (same private endpoint instagrapi / `instagram_private_api.media_only_me()` use) |
| Exceptions | `instaharvest_v2.exceptions`: `InstagramError`, `LoginRequired`, `ChallengeRequired`, `CheckpointRequired`, `ConsentRequired`, `RateLimitError`, `NotFoundError`/`MediaNotFound`, `NetworkError`, `ProxyError` |
| Upstream bug | installed GraphQL/users/client modules call bare `build_request_headers()` without importing it — patched at runtime from `http_utils` |

## Setup

1. **GitHub**: this repo (`insta-reel-bot`).
2. **Turso**: create a DB in the dashboard (creates the default group) → get `libsql://…` URL + token. Tables auto-create on startup (idempotent `schema.sql`).
3. **Instagram session**: log in once in a browser, open DevTools → Application → Cookies, and copy `SESSION_ID`, `CSRF_TOKEN`, `DS_USER_ID` (plus `MID`, `IG_DID`, `DATR`, `USER_AGENT` for stability) into Render env.
4. **Env vars on Render** (see `.env.example`): `INSTAGRAM_*`, `DESTINATION_USERNAME`, `TURSO_*`, `UPLOAD_SECRET`, `REEL_FETCH_COUNT=30`, `COVER_MODE`, `HIDE_LIKE_VIEW_COUNTS=true`, `LOG_LEVEL`.
5. **Covers**: drop `.png/.jpg/.jpeg/.webp` files into `/cover/` (GitHub web UI → Add file → Upload files).
6. **Deploy**: Render → New → Web Service → select repo. Build `pip install -r requirements.txt`, start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`, health check `/health`. Or push `render.yaml` (Blueprint).
7. **UptimeRobot — THREE monitors:**
   - **Keep-alive** (beats Render Free's ~15 min sleep): HTTP(s), URL
     `https://<service>.onrender.com/ping`, interval **5 minutes**.
     Ultra-light: no DB, no Instagram, instant response.
   - **Auto-upload** (your schedule): HTTP(s), URL
     `https://<service>.onrender.com/upload?token=<UPLOAD_SECRET>&hide_like=1`,
     method GET or POST, interval per desired cadence (e.g. 120–300 min).
     Each hit uploads **at most one** reel (`MAX_UPLOADS_PER_RUN=1`).
   - **Auto-archive** (daily cleanup): HTTP(s), URL
     `https://<service>.onrender.com/archive?token=<UPLOAD_SECRET>`,
     method GET or POST, interval **24 hours**.
     Archives reels older than 24h with under 900 views (owner-only,
     reversible in the Instagram app). Popular, unreadable, and
     already-archived reels are skipped.
   Keep them separate so wake-up pings never trigger uploads or archives.
8. **Test**: `GET /health` → `{"status":"ok"}`; `GET /upload?token=<UPLOAD_SECRET>` → `success` / `no_new_reel` / `busy` / `failed`; `GET /archive?token=<UPLOAD_SECRET>` → `{"status":"ok","checked":0,...}` (nothing old enough yet).

## Simple auth + live log

All protected endpoints accept the secret two ways (use whichever is easier):

- Header: `Authorization: Bearer <UPLOAD_SECRET>`
- Query: `/upload?token=<UPLOAD_SECRET>`

Hide like/view counts: automatic on every upload. Override per run with
`&hide_like=0` (leave visible) or `&hide_like=1` (force hide).
Full example: `/upload?token=<SECRET>&hide_like=1`.

Captions: the source Reel's original caption is copied verbatim (up to 2200
chars). If the source has no caption, `REEL_CAPTION` is used as a template
(`{username}`, `{shortcode}`). The success response reports
`"caption_copied": true/false` and `"like_hidden": true/false`.

Watch runs live in your browser: `https://<service>.onrender.com/live?token=<UPLOAD_SECRET>`
shows the latest run counters plus a streaming activity tail.
Raw JSON for dashboards: `GET /api/activity?token=<UPLOAD_SECRET>`.

Note: a URL token can appear in access logs — fine for a personal bot,
but don't share the bookmarked link publicly.

## Environment variables

| Var | Required | Default | Meaning |
|---|---|---|---|
| `INSTAGRAM_USERNAME` | for login | — | Source/destination login |
| `INSTAGRAM_PASSWORD` | first login only | — | Avoid storing; prefer session |
| `INSTAGRAM_SESSION` | alt session | — | session.json content, raw or base64 |
| `SESSION_ID`/`CSRF_TOKEN`/`DS_USER_ID` | ✅ (preferred) | — | Browser cookies (`from_env`) |
| `DESTINATION_USERNAME` | ✅ | — | Account uniqueness scope |
| `TURSO_DATABASE_URL` | ✅ | — | `libsql://…` |
| `TURSO_AUTH_TOKEN` | ✅ | — | Turso token |
| `UPLOAD_SECRET` | ✅ | — | Secret for `/upload` + `/archive` (header or `?token=`) and `/live` |
| `REEL_FETCH_COUNT` | — | 30 | Candidates per run |
| `HIDE_LIKE_VIEW_COUNTS` | — | true | Default for hiding counts; `hide_like` overrides per run |
| `SHARE_TO_FEED` | — | true | Share reel preview to profile grid/feed (`false` = Reels tab only) |
| `COVER_MODE` | — | random | `random`/`sequential`/`fixed` |
| `COVER_FILE` | for fixed | — | e.g. `cover/1.png` |
| `REEL_CAPTION` | — | 🎬 via @{username} #reels | Fallback template when source has no caption |
| `ARCHIVE_MIN_AGE_HR` | — | 24 | `/archive`: only reels older than this (hours) |
| `ARCHIVE_MAX_VIEWS` | — | 900 | `/archive`: archive reels below this view count |
| `PROXY_URL` | — | — (direct) | Optional proxy (`http://user:pass@host:port` or `socks5h://host:port`); empty = direct |
| `TELEGRAM_ENABLED` | — | false | Telegram run logs on/off |
| `TELEGRAM_BOT_TOKEN` | when enabled | — | Bot token from @BotFather (keep secret, env only) |
| `TELEGRAM_CHAT_ID` | when enabled | — | Numeric chat id that receives the logs |
| `LOG_LEVEL` | — | INFO | — |

## Proxy (optional)

- Leave `PROXY_URL` empty for a direct connection (default, 100% optional).
- Set `PROXY_URL=http://user:pass@host:port` (or `https://…`, `socks5://…`,
  `socks5h://…`) to route Instagram traffic through a proxy.
- Invalid scheme/host → a warning is logged and the run continues direct.
- Only the proxy host is ever logged — credentials are never logged.

## Telegram logs (optional, off by default)

- `TELEGRAM_ENABLED=false` by default — set to `true` to log every run.
- Setup: talk to **@BotFather** → `/newbot` → copy the bot token into
  `TELEGRAM_BOT_TOKEN`; message your bot once, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your numeric
  `chat id` → `TELEGRAM_CHAT_ID`. (No real values belong in the repo —
  env names only.)
- Per-request override (works even when globally off): `?logbot=1/0` —
  `/upload?token=SECRET&logbot=1` forces Telegram for that run;
  `/upload?token=SECRET&logbot=0` silences it. Same `?logbot=` param on
  `/archive`. Accepts `1/true/yes/on` and `0/false/no/off`.
- Logged events: run started (`run_id`), success (shortcode + destination
  id + elapsed), failed (shortcode + error kind, truncated). Never includes
  secrets, session material, or caption text. A Telegram failure never
  breaks the pipeline.

## Database & deduplication

`processed_reels` keyed by `UNIQUE(source_media_id, destination_account)` — the same source reel can serve multiple destinations exactly once each. Lifecycle `PROCESSING → DOWNLOADED → UPLOADED → COMPLETED`; failures → `FAILED` (retryable once). `try_claim_reel()` is the atomic gate; stale `PROCESSING` rows are reclaimed only after `STALE_CLAIM_TIMEOUT_SEC`. `bot_runs` logs every run; `bot_settings` holds the distributed `upload_lock`/`archive_lock` and the `cover_index` counter (sequential covers survive restarts — no local files). `archived=1`/`archived_at` mark auto-archived rows so they are never re-processed.

## Cover system

- `random`: uniform pick. `sequential`: Turso-backed round-robin. `fixed`: `COVER_FILE`.
- Covers are repo assets; never modified. Any conversion would happen in `/tmp` only.
- Missing dir / invalid file → run fails safely with `failed`, nothing uploaded.

## Hidden counts — supported via configure param

`instaharvest-v2`'s `post_reel()` exposes no like-count-hiding option, so the
uploader performs the publish itself and sets
`like_and_view_counts_disabled=1` on `configure_to_clips` (verified wire
parameter, same as instagrapi/goinsta use). Counts are hidden by default;
pass `hide_like=0` to skip. The success response reports `"like_hidden"`.

## Share to feed — reels also appear in the profile grid

`instaharvest-v2`'s `post_reel()` exposes no feed-preview option, so the
uploader performs the publish itself and sets
`clips_share_preview_to_feed=1` on `configure_to_clips` (verified wire
parameter — the same one instagrapi sends for
`clip_upload(show_preview_in_feed=True)`). Without it Instagram defaults to
Reels-tab-only: the reel never shows in the profile grid and the post count
doesn't go up. Sharing is on by default (`SHARE_TO_FEED=true`); set
`SHARE_TO_FEED=false` to opt out back to Reels-tab-only. The success
response reports `"shared_to_feed": true/false`. Note: this applies to reels
uploaded after the change — already-posted Reels-tab-only reels can only be
fixed in the Instagram app (open the reel → ⋯ → "Add to profile grid", or
re-upload).

## Auto-archive — daily flop cleanup

`GET /archive?token=<UPLOAD_SECRET>` runs one pass (same token auth and
`busy` locking as `/upload`, with its own `archive_lock`):

1. Selects `COMPLETED` rows older than `ARCHIVE_MIN_AGE_HR` (default 24)
   that were never archived.
2. Reads each reel's live view count (`play_count`/`view_count`).
3. Archives (`POST media/{id}/only_me/`, owner-only) those below
   `ARCHIVE_MAX_VIEWS` (default 900) and marks them `archived=1` in Turso.
4. Skips popular reels, unreadable view counts (never archive on unknown
   data), and already-archived rows.

Per-run overrides: `/archive?token=<SECRET>&min_age_hr=48&max_views=500`.
Target one post only: `&only_pk=<destination_media_id>` (bypasses the age
cutoff, keeps every other gate — other reels are never touched). Preview
without archiving: `&dry_run=1` (reports `would_archive`, archives nothing).
UptimeRobot: one HTTP(s) monitor, 24h interval. Archiving is reversible —
unarchive anytime in the Instagram app (profile → menu → Archive).

## Crash-safety edge case

If Instagram accepts the upload but the process dies before Turso marks `COMPLETED`, the next run may re-upload (classic distributed-systems dual-write). Mitigation: `COMPLETED` is written only after a destination pk is returned; uploads are never blindly retried after timeouts; check the destination account manually after crashes.

## Troubleshooting

- `401 unauthorized` on `/upload` or `/archive` → wrong/missing secret (header or `?token=`).
- `{"status":"busy"}` → overlapping tick; increase UptimeRobot interval.
- `{"status":"no_new_reel"}` → whole batch already processed; normal.
- `instagram_auth` → session expired; refresh cookies in Render env (DevTools → Cookies).
- `instagram_challenge/checkpoint` → cookie sessions avoid this; password logins from server IPs usually get challenged — approve in the Instagram app and refresh cookies.
- `instagram_rate_limited` → widen UptimeRobot interval.
- Turso errors → check URL/token; tables self-heal on next boot (archive columns migrate automatically).
- Render sleeping (Free) → the 5-min `/ping` monitor keeps it awake.
- Invalid cover → check `/cover/` extensions and `COVER_FILE` path.

## Run tests

```bash
pip install -r requirements.txt
pytest -q
```
