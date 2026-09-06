"""Unit tests: DB dedup, covers, API auth, cleanup. Run: pytest -q.

Uses in-memory sqlite (stdlib) via a tiny shim exposing the same
execute()/query_dicts() protocol the repository expects from TursoClient.
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import repository as repo  # noqa: E402
from app.instagram import covers as cover_mod  # noqa: E402
from app.instagram.adapter import ReelCandidate, normalize_reel  # noqa: E402
from app.instagram.discovery import filter_candidates  # noqa: E402


class SqliteDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def execute(self, sql, args=()):
        cur = self.conn.execute(sql, tuple(args or ()))
        self.conn.commit()
        return {"rows": []}

    def query_dicts(self, sql, args=()):
        cur = self.conn.execute(sql, tuple(args or ()))
        return [dict(r) for r in cur.fetchall()]


@pytest.fixture
def db():
    d = SqliteDB()
    repo.init_schema(d)
    return d


# ---------- database ----------

def test_insert_and_duplicate(db):
    ok, _ = repo.try_claim_reel(db, source_media_id="111", destination_account="dest")
    assert ok
    ok2, reason = repo.try_claim_reel(db, source_media_id="111", destination_account="dest")
    assert not ok2 and reason == "in_progress"
    repo.mark_status(db, "111", "dest", "COMPLETED", destination_media_id="999")
    ok3, reason3 = repo.try_claim_reel(db, source_media_id="111", destination_account="dest")
    assert not ok3 and reason3 == "duplicate"


def test_different_destination_account(db):
    repo.try_claim_reel(db, source_media_id="111", destination_account="a")
    repo.mark_status(db, "111", "a", "COMPLETED", destination_media_id="1")
    ok, _ = repo.try_claim_reel(db, source_media_id="111", destination_account="b")
    assert ok  # UNIQUE(source_media_id, destination_account)


def test_failed_allows_retry(db):
    repo.try_claim_reel(db, source_media_id="222", destination_account="d")
    repo.mark_status(db, "222", "d", "FAILED", error="boom")
    ok, _ = repo.try_claim_reel(db, source_media_id="222", destination_account="d")
    assert ok


def test_lock_acquire_release(db):
    assert repo.acquire_lock(db, ttl_sec=600)
    assert not repo.acquire_lock(db, ttl_sec=600)
    repo.release_lock(db)
    assert repo.acquire_lock(db, ttl_sec=600)


# ---------- covers ----------

def _make_covers(tmp, names=("a.png", "b.jpg", "c.webp", "note.txt")):
    for n in names:
        (Path(tmp) / n).write_bytes(b"x")
    return tmp


def test_covers_random_and_fixed(tmp_path):
    _make_covers(tmp_path)
    pick = cover_mod.select_cover(mode="random", cover_dir=tmp_path)
    assert pick.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    fixed = tmp_path / "a.png"
    pick2 = cover_mod.select_cover(mode="fixed", cover_dir=tmp_path,
                                   fixed_file=str(fixed))
    assert pick2 == fixed


def test_covers_sequential_persists_in_db(db, tmp_path):
    _make_covers(tmp_path)
    a = cover_mod.select_cover(mode="sequential", cover_dir=tmp_path, db=db)
    b = cover_mod.select_cover(mode="sequential", cover_dir=tmp_path, db=db)
    assert a != b  # counter survived (Turso-backed, not a local file)


def test_covers_missing_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        cover_mod.select_cover(mode="random", cover_dir=tmp_path / "nope")


# ---------- discovery ----------

def _cand(mid, code="ABC", user="u", mtype=2, url="http://v/x.mp4"):
    return ReelCandidate(source_media_id=mid, shortcode=code, username=user,
                         media_type=mtype, video_url=url)


def test_filter_skips_duplicates_but_continues():
    cands = [_cand("A"), _cand("B"), _cand("C")]
    pick, stats = filter_candidates(
        cands, already_done=lambda m: m in {"A", "B"})
    assert pick is not None and pick.source_media_id == "C"
    assert stats["skipped"] == 2


def test_filter_all_duplicates_returns_none():
    cands = [_cand("A"), _cand("B"), _cand("A"), _cand("B")]
    pick, _ = filter_candidates(cands, already_done=lambda m: True)
    assert pick is None


def test_normalize_raw_rest_payload():
    raw = {"pk": "123", "code": "XYZ", "media_type": 2,
           "user": {"username": "someone"},
           "video_versions": [{"url": "http://v/1.mp4"}]}
    cand = normalize_reel(raw)
    assert cand and cand.source_media_id == "123"
    assert cand.video_url == "http://v/1.mp4"


def test_normalize_missing_id_returns_none():
    assert normalize_reel({"code": "XYZ"}) is None


# ---------- API ----------

def test_health_and_auth():
    from fastapi.testclient import TestClient

    import app.main as main
    main.settings.UPLOAD_SECRET = "test-secret"
    client = TestClient(main.app)
    assert client.get("/health").json() == {"status": "ok"}
    r = client.post("/upload")
    assert r.status_code in (401, 403)
    r2 = client.post("/upload", headers={"Authorization": "Bearer wrong"})
    assert r2.status_code in (401, 403)


def test_concurrent_upload_returns_busy(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main
    main.settings.UPLOAD_SECRET = "test-secret"
    assert main._thread_lock.acquire(blocking=False)
    monkeypatch.setattr(repo, "acquire_lock", lambda db, ttl_sec=600: True)
    monkeypatch.setattr(repo, "release_lock", lambda db, key="upload_lock": None)
    monkeypatch.setattr(main, "get_db", lambda: SqliteDB())
    client = TestClient(main.app)
    try:
        r = client.post("/upload", headers={"Authorization": "Bearer test-secret"})
        assert r.json()["status"] == "busy"
    finally:
        main._thread_lock.release()


# ---------- query-token auth + live activity ----------

def test_upload_accepts_token_param(monkeypatch):
    from fastapi.testclient import TestClient

    import app.main as main
    main.settings.UPLOAD_SECRET = "test-secret"
    assert main._thread_lock.acquire(blocking=False)
    monkeypatch.setattr(repo, "acquire_lock", lambda db, ttl_sec=600: True)
    monkeypatch.setattr(repo, "release_lock", lambda db, key="upload_lock": None)
    monkeypatch.setattr(main, "get_db", lambda: SqliteDB())
    client = TestClient(main.app)
    try:
        r = client.post("/upload?token=test-secret")
        assert r.json()["status"] == "busy"
        assert client.post("/upload?token=wrong").status_code == 401
        g = client.get("/upload?token=test-secret")
        assert g.json()["status"] == "busy"
    finally:
        main._thread_lock.release()


def test_live_and_activity_auth():
    from fastapi.testclient import TestClient

    import app.main as main
    main.settings.UPLOAD_SECRET = "test-secret"
    client = TestClient(main.app)
    assert client.get("/live").status_code == 200  # page shell is public
    assert client.get("/api/activity").status_code == 401
    assert client.get("/api/activity?token=wrong").status_code == 401


def test_authorized_helper():
    import app.main as main
    main.settings.UPLOAD_SECRET = "test-secret"
    assert main._authorized("Bearer test-secret", None)
    assert main._authorized(None, "test-secret")
    assert not main._authorized(None, None)
    assert not main._authorized("Bearer wrong", "also-wrong")


# ---------- cleanup ----------

def test_cleanup_removes_tmp_files():
    from app.instagram import downloader
    fh = tempfile.NamedTemporaryFile(prefix="reel_", suffix=".mp4",
                                     dir="/tmp", delete=False)
    fh.write(b"data")
    fh.close()
    assert Path(fh.name).exists()
    downloader.cleanup(fh.name)
    assert not Path(fh.name).exists()
    downloader.cleanup("/tmp/does-not-exist-xyz.mp4")  # must not raise
