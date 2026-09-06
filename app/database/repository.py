"""Repository layer: atomic claims, status transitions, run bookkeeping.

Works against TursoClient (prod) or sqlite3 (tests) through a tiny
protocol: the object must expose ``execute(sql, args)`` and
``query_dicts(sql, args)``. SQL is kept portable (no RETURNING).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

TERMINAL_OK = "COMPLETED"
TERMINAL_FAIL = "FAILED"
ACTIVE_STATES = ("PROCESSING", "DOWNLOADED", "UPLOADED")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_schema(db) -> None:
    schema = SCHEMA_PATH.read_text()
    for stmt in [s.strip() for s in schema.split(";") if s.strip()]:
        db.execute(stmt)


# ---------------- processed_reels ----------------

def is_completed(db, source_media_id: str, destination_account: str) -> bool:
    rows = db.query_dicts(
        "SELECT status FROM processed_reels WHERE source_media_id=? AND destination_account=?",
        (source_media_id, destination_account),
    )
    return bool(rows) and rows[0]["status"] == TERMINAL_OK


def try_claim_reel(db, *, source_media_id: str, shortcode: str = "",
                   username: str = "", destination_account: str = "",
                   stale_sec: int = 1800) -> tuple[bool, str]:
    """Atomically claim a reel. Returns (claimed, reason).

    Reasons: claimed | duplicate | in_progress | reclaimed
    """
    rows = db.query_dicts(
        "SELECT status, updated_at FROM processed_reels WHERE source_media_id=? AND destination_account=?",
        (source_media_id, destination_account),
    )
    now = utcnow()
    if not rows:
        db.execute(
            "INSERT INTO processed_reels (source_media_id, source_shortcode, source_username,"
            " destination_account, status, attempts, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'PROCESSING', 1, ?, ?)",
            (source_media_id, shortcode, username, destination_account, now, now),
        )
        return True, "claimed"
    status = rows[0]["status"]
    if status == TERMINAL_OK:
        return False, "duplicate"
    if status in ACTIVE_STATES:
        updated = str(rows[0].get("updated_at") or "")
        try:
            ts = datetime.fromisoformat(updated).timestamp()
            age = time.time() - ts
        except Exception:
            age = 0
        if age < stale_sec:
            return False, "in_progress"
        db.execute(
            "UPDATE processed_reels SET status='PROCESSING', attempts=attempts+1,"
            " last_error='stale lock reclaimed', updated_at=? WHERE source_media_id=?"
            " AND destination_account=?",
            (now, source_media_id, destination_account),
        )
        return True, "reclaimed"
    # FAILED -> allow one more attempt
    db.execute(
        "UPDATE processed_reels SET status='PROCESSING', attempts=attempts+1,"
        " source_shortcode=?, source_username=?, updated_at=? WHERE source_media_id=?"
        " AND destination_account=?",
        (shortcode, username, now, source_media_id, destination_account),
    )
    return True, "claimed"


def mark_status(db, source_media_id: str, destination_account: str, status: str,
                destination_media_id: str = "", error: str = "") -> None:
    now = utcnow()
    if status == TERMINAL_OK:
        db.execute(
            "UPDATE processed_reels SET status='COMPLETED', destination_media_id=?,"
            " last_error='', updated_at=?, completed_at=? WHERE source_media_id=?"
            " AND destination_account=?",
            (destination_media_id, now, now, source_media_id, destination_account),
        )
    else:
        db.execute(
            "UPDATE processed_reels SET status=?, last_error=?, updated_at=?"
            " WHERE source_media_id=? AND destination_account=?",
            (status, (error or "")[:2000], now, source_media_id, destination_account),
        )


# ---------------- bot_runs ----------------

def start_run(db) -> int:
    db.execute("INSERT INTO bot_runs (status) VALUES ('running')")
    rows = db.query_dicts("SELECT id FROM bot_runs ORDER BY id DESC LIMIT 1", ())
    return int(rows[0]["id"])


def finish_run(db, run_id: int, status: str, error: str = "", **counters) -> None:
    sets = ["status=?", "finished_at=?", "error_message=?"]
    args: list = [status, utcnow(), (error or "")[:2000]]
    for key in ("reels_found", "reels_skipped", "reels_attempted",
                "reels_uploaded", "reels_failed"):
        if key in counters:
            sets.append(f"{key}=?")
            args.append(int(counters[key]))
    args.append(run_id)
    db.execute(f"UPDATE bot_runs SET {', '.join(sets)} WHERE id=?", tuple(args))


def last_successful_upload_at(db) -> str | None:
    rows = db.query_dicts(
        "SELECT MAX(completed_at) AS ts FROM processed_reels WHERE status='COMPLETED'", ()
    )
    return rows[0]["ts"] if rows else None


def counts(db) -> dict:
    total = db.query_dicts("SELECT COUNT(*) AS c FROM processed_reels", ())[0]["c"]
    done = db.query_dicts(
        "SELECT COUNT(*) AS c FROM processed_reels WHERE status='COMPLETED'", ())[0]["c"]
    failed_runs = db.query_dicts(
        "SELECT COUNT(*) AS c FROM bot_runs WHERE status='failed'", ())[0]["c"]
    last = db.query_dicts(
        "SELECT * FROM bot_runs ORDER BY id DESC LIMIT 1", ())
    return {
        "processed_total": int(total),
        "processed_completed": int(done),
        "failed_runs": int(failed_runs),
        "last_run": dict(last[0]) if last else None,
        "last_successful_upload_at": last_successful_upload_at(db),
    }


# ---------------- distributed lock + cover counter ----------------

def acquire_lock(db, key: str = "upload_lock", ttl_sec: int = 600) -> bool:
    rows = db.query_dicts("SELECT value, updated_at FROM bot_settings WHERE key=?", (key,))
    now = utcnow()
    if not rows:
        db.execute("INSERT INTO bot_settings (key, value, updated_at) VALUES (?, ?, ?)",
                   (key, "locked", now))
        return True
    try:
        age = time.time() - datetime.fromisoformat(str(rows[0]["updated_at"])).timestamp()
    except Exception:
        age = ttl_sec + 1
    if age < ttl_sec and rows[0]["value"] == "locked":
        return False
    db.execute("UPDATE bot_settings SET value='locked', updated_at=? WHERE key=?", (now, key))
    return True


def release_lock(db, key: str = "upload_lock") -> None:
    db.execute(
        "UPDATE bot_settings SET value='free', updated_at=? WHERE key=?", (utcnow(), key)
    )


def next_cover_index(db, total: int) -> int:
    rows = db.query_dicts("SELECT value FROM bot_settings WHERE key='cover_index'", ())
    idx = int(rows[0]["value"]) if rows and str(rows[0]["value"]).isdigit() else 0
    chosen = idx % max(total, 1)
    db.execute(
        "INSERT INTO bot_settings (key, value, updated_at) VALUES ('cover_index', ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (str(idx + 1), utcnow()),
    )
    return chosen
