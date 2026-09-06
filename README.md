# Instagram Reel Automation Bot

Production-ready Python service. UptimeRobot calls `POST /upload` → the bot
pulls Instagram's dedicated Reels feed via **InstaHarvest v2**
(`ig.feed.get_reels_feed()`), skips everything already recorded in **Turso**,
uploads one new Reel with a custom cover from `/cover/`, records it, and
cleans `/tmp`.

## Verified InstaHarvest v2 API surface (don't guess — this is checked)

| Concern | Real API (`pip install instaharvest-v2`) |
|---|---|
| Import / client | `from instaharvest_v2 import Instagram`; `ig = Instagram.from_env()` / `Instagram()` |
| Login / session | `ig.login(u, p)`, `ig.save_session()` / `ig.auth.save_session/load_session(path)`, `ig.auth.validate_session()`, `Instagram.from_session_file(path)` |
| Challenge codes | `Instagram(challenge_callback=...)` fires mid-login for `ChallengeRequired`; `CheckpointRequired` carries no callback — resolved via `ChallengeHandler.resolve(session, challenge_url, csrf_token)` with email/SMS auto-selection |
| Reels feed | `ig.feed.get_reels_feed(max_id=None)` → `dict` (paginate via `max_id`; **no** `count` kwarg — the adapter loops pages until `REEL_FETCH_COUNT`) |
| NOT used | `get_timeline()` (home feed, not the Reels feed) |
| Media fields | `Media.pk`, `Media.shortcode`, `media_type` (1 photo / 2 video / 8 carousel), `video_url`, `video_duration`; raw dicts use `pk`/`id`, `code`/`shortcode` |
| Download | `ig.download.download_media(pk)`; fallback `ig.public.get_post_by_shortcode(code)` → `video_url` |
| Upload | `ig.upload.post_reel(video_path\|video_data, thumbnail_path\|thumbnail_data, caption, duration, width, height)` → dict with media `pk` |
| Cover | ✅ supported via `thumbnail_path` / `thumbnail_data` on `post_reel()` |
| Hide like/view counts | ❌ **NOT supported** — `post_reel()` has no such parameter. If `HIDE_LIKE_VIEW_COUNTS=true`, the service logs a warning and uploads with counts visible. It never claims otherwise. |
| Exceptions | `instaharvest_v2.exceptions`: `InstagramError`, `LoginRequired`, `ChallengeRequired`, `CheckpointRequired`, `ConsentRequired`, `RateLimitError`, `NotFoundError`/`MediaNotFound`, `NetworkError`, `ProxyError` |

## Setup

1. **GitHub**: this repo (`insta-reel-bot`).
2. **Turso**: create a DB in the dashboard (creates the default group) → get `libsql://…` URL + token. Tables auto-create on startup (idempotent `schema.sql`).
3. **Instagram session** (no repeated logins): log in once in a browser, open DevTools → Application → Cookies, and copy `SESSION_ID`, `CSRF_TOKEN`, `DS_USER_ID` (plus `MID`, `IG_DID`, `DATR` for stability) into Render env. This cookie path is the reliable one — password logins from server IPs usually hit checkpoints.
4. **Env vars on Render** (see `.env.example`): `INSTAGRAM_*`, `DESTINATION_USERNAME`, `TURSO_*`, `UPLOAD_SECRET`, `REEL_FETCH_COUNT=30`, `COVER_MODE`, `HIDE_LIKE_VIEW_COUNTS`, `LOG_LEVEL`.
5. **Covers**: drop `.png/.jpg/.jpeg/.webp` files into `/cover/` (GitHub web UI → Add file → Upload files).
6. **Deploy**: Render → New → Web Service → select repo. Build `pip install -r requirements.txt`, start `uvicorn app.main:app --host 0.0.0.0 --port $PORT`, health check `/health`. Or push `render.yaml` (Blueprint).
7. **UptimeRobot**: monitor type HTTP(s), URL `https://<service>.onrender.com/upload?token=<UPLOAD_SECRET>`, method POST. Interval ≥ your desired posting cadence (e.g. 60–300 min). Each hit uploads **at most one** reel (`MAX_UPLOADS_PER_RUN=1`).
8. **Test**: `GET /health` → `{"status":"ok"}`; `POST /upload?token=<UPLOAD_SECRET>` → `success` / `no_new_reel` / `busy` / `failed`.

## Simple auth + live log

All protected endpoints accept the secret two ways (use whichever is easier):

- Header: `Authorization: Bearer <UPLOAD_SECRET>`
- Query: `POST /upload?token=<UPLOAD_SECRET>`

UptimeRobot: use the query form as the monitor URL —
`https://<service>.onrender.com/upload?token=<UPLOAD_SECRET>`, method POST.

Watch runs live in your browser: `https://<service>.onrender.com/live?token=<UPLOAD_SECRET>`
shows the latest run counters plus a streaming activity tail
(DISCOVERY → CLAIM → DOWNLOAD → COVER → UPLOAD → COMPLETED/FAILED).
Raw JSON for dashboards: `GET /api/activity?token=<UPLOAD_SECRET>`.

Note: a URL token can appear in access logs — fine for a personal bot,
but don't share the bookmarked link publicly.

## Environment variables

| Var | Required | Default | Meaning |
|---|---|---|---|
| `INSTAGRAM_USERNAME` | for login | — | Source/destination login |
| `INSTAGRAM_PASSWORD` | first login only | — | Avoid storing; prefer session |
| `INSTAGRAM_SESSION` | ✅ (one of the session options) | — | session.json content, raw or base64 |
| `SESSION_ID`/`CSRF_TOKEN`/`DS_USER_ID` | ✅ (preferred) | — | Browser cookies (`from_env`) |
| `DESTINATION_USERNAME` | ✅ | — | Account uniqueness scope |
| `TURSO_DATABASE_URL` | ✅ | — | `libsql://…` |
| `TURSO_AUTH_TOKEN` | ✅ | — | Turso token |
| `UPLOAD_SECRET` | ✅ | — | Secret for `/upload` (header or `?token=`) and `/live` |
| `REEL_FETCH_COUNT` | — | 30 | Candidates per run |
| `MAX_UPLOADS_PER_RUN` | — | 1 | Always 1 in this design |
| `STALE_CLAIM_TIMEOUT_SEC` | — | 1800 | Reclaim crashed PROCESSING rows |
| `UPLOAD_LOCK_TIMEOUT_SEC` | — | 600 | Distributed lock TTL |
| `COVER_MODE` | — | random | `random`/`sequential`/`fixed` |
| `COVER_FILE` | for fixed | — | e.g. `cover/1.png` |
| `HIDE_LIKE_VIEW_COUNTS` | — | true | Requested but **unsupported by library** → warning |
| `REEL_CAPTION` | — | 🎬 via @{username} #reels | `{username}`, `{shortcode}` placeholders |
| `LOG_LEVEL` | — | INFO | — |

## Database & deduplication

`processed_reels` keyed by `UNIQUE(source_media_id, destination_account)` — the same source reel can serve multiple destinations exactly once each. Lifecycle `PROCESSING → DOWNLOADED → UPLOADED → COMPLETED`; failures → `FAILED` (retryable once). `try_claim_reel()` is the atomic gate; stale `PROCESSING` rows are reclaimed only after `STALE_CLAIM_TIMEOUT_SEC`. `bot_runs` logs every run; `bot_settings` holds the distributed `upload_lock` and the `cover_index` counter (sequential covers survive restarts — no local files).

## Cover system

- `random`: uniform pick. `sequential`: Turso-backed round-robin. `fixed`: `COVER_FILE`.
- Covers are repo assets; never modified. Any conversion would happen in `/tmp` only.
- Missing dir / invalid file → run fails safely with `failed`, nothing uploaded.

## Hidden counts — honest limitation

`instaharvest-v2`'s `post_reel()` exposes no like-count-hiding option, and there is no separate supported endpoint in the installed version. The uploader (`app/instagram/uploader.py`) therefore logs `HIDE_LIKE_VIEW_COUNTS requested but UNSUPPORTED…` and proceeds visibly. To support it later, add an adapter in `uploader.py` — never fake the flag.

## Crash-safety edge case

If Instagram accepts the upload but the process dies before Turso marks `COMPLETED`, the next run may re-upload (classic distributed-systems dual-write). Mitigation: `COMPLETED` is written only after a destination pk is returned; uploads are never blindly retried after timeouts; check the destination account manually after crashes.

## Troubleshooting

- `401 unauthorized` on `/upload` → wrong/missing secret (header or `?token=`).
- `{"status":"busy"}` → overlapping tick; increase UptimeRobot interval.
- `{"status":"no_new_reel"}` → whole batch already processed; normal.
- `instagram_auth` → session expired; refresh cookies in Render env (DevTools → Cookies).
- `instagram_challenge/checkpoint` → cookie sessions avoid this; password logins from server IPs usually get challenged — approve in the Instagram app and refresh cookies.
- `instagram_rate_limited` → widen UptimeRobot interval.
- Turso errors → check URL/token; tables self-heal on next boot.
- Render sleeping (Free) → first UptimeRobot hit wakes it; upload still runs.
- Invalid cover → check `/cover/` extensions and `COVER_FILE` path.

## Run tests

```bash
pip install -r requirements.txt
pytest -q
```
