-- Turso / libSQL schema. Applied idempotently on startup.
-- Deduplication key: UNIQUE(source_media_id, destination_account).

CREATE TABLE IF NOT EXISTS processed_reels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_media_id TEXT NOT NULL,
    source_shortcode TEXT,
    source_username TEXT,
    destination_account TEXT NOT NULL DEFAULT '',
    destination_media_id TEXT,
    status TEXT NOT NULL DEFAULT 'PROCESSING',
    attempts INTEGER NOT NULL DEFAULT 1,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at TEXT,
    UNIQUE(source_media_id, destination_account)
);
CREATE INDEX IF NOT EXISTS idx_processed_status ON processed_reels(status, updated_at);

CREATE TABLE IF NOT EXISTS bot_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    reels_found INTEGER NOT NULL DEFAULT 0,
    reels_skipped INTEGER NOT NULL DEFAULT 0,
    reels_attempted INTEGER NOT NULL DEFAULT 0,
    reels_uploaded INTEGER NOT NULL DEFAULT 0,
    reels_failed INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
