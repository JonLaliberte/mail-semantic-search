"""Tests for the backfill lock + index_emails / index_email_file skip path."""

import os

import pytest

from mail_semantic_search.database import Database


def _make_db(tmp_path) -> Database:
    return Database(tmp_path / "test.db")


def test_no_lock_returns_none(tmp_path):
    db = _make_db(tmp_path)
    assert db.get_backfill_lock() is None
    db.close()


def test_acquire_and_release(tmp_path):
    db = _make_db(tmp_path)
    db.acquire_backfill_lock()
    held = db.get_backfill_lock()
    assert held is not None
    assert held["pid"] == os.getpid()
    assert "started_at" in held
    db.release_backfill_lock()
    assert db.get_backfill_lock() is None
    db.close()


def test_acquire_twice_same_process_is_idempotent(tmp_path):
    db = _make_db(tmp_path)
    db.acquire_backfill_lock()
    # Should not raise — same PID re-claiming its own lock.
    db.acquire_backfill_lock()
    held = db.get_backfill_lock()
    assert held["pid"] == os.getpid()
    db.release_backfill_lock()
    db.close()


def test_stale_pid_lock_is_treated_as_absent(tmp_path):
    """A lock held by a dead PID should not block a new acquire."""
    db = _make_db(tmp_path)
    # Inject a lock for a PID that almost certainly doesn't exist.
    import json
    from datetime import datetime
    fake = json.dumps({"pid": 999999, "started_at": datetime.now().isoformat()})
    cursor = db.conn.cursor()
    cursor.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?)",
        (Database._BACKFILL_LOCK_KEY, fake),
    )
    db.conn.commit()

    # get_backfill_lock filters dead PIDs.
    assert db.get_backfill_lock() is None

    # acquire should succeed and overwrite.
    db.acquire_backfill_lock()
    held = db.get_backfill_lock()
    assert held["pid"] == os.getpid()

    db.release_backfill_lock()
    db.close()


def test_skip_message_contains_no_error_or_warning_words(tmp_path, capsys, monkeypatch):
    """The KM rule pops a window on 'error' or 'warning' substrings — verify
    the skip string we print contains neither."""
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.index import _check_backfill_lock_or_skip

    monkeypatch.setattr(cfg_mod.config, "database_path", tmp_path / "skip.db")

    db = Database()
    db.acquire_backfill_lock()
    db.close()

    skipped = _check_backfill_lock_or_skip()
    assert skipped is True

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "error" not in output.lower()
    assert "warning" not in output.lower()
    assert "Backfill in progress" in captured.out

    # Cleanup so other tests don't see a residual lock.
    db2 = Database()
    db2.release_backfill_lock()
    db2.close()


def test_no_lock_returns_false_from_check(tmp_path, monkeypatch):
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.index import _check_backfill_lock_or_skip

    monkeypatch.setattr(cfg_mod.config, "database_path", tmp_path / "nolock.db")

    # Init the DB so the table exists.
    db = Database()
    db.close()

    assert _check_backfill_lock_or_skip() is False


# --- kind-tagged lock + incremental run mutual exclusion ---------------------


def _inject_foreign_lock(db: Database, kind: str, pid: int = 424242) -> None:
    """Write a lock payload as if another process (pid) held it."""
    import json
    from datetime import datetime

    payload = json.dumps(
        {"pid": pid, "started_at": datetime.now().isoformat(), "kind": kind}
    )
    cursor = db.conn.cursor()
    cursor.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?)",
        (Database._BACKFILL_LOCK_KEY, payload),
    )
    db.conn.commit()


def test_acquire_lock_defaults_to_backfill_kind(tmp_path):
    db = _make_db(tmp_path)
    db.acquire_backfill_lock()
    held = db.get_backfill_lock()
    assert held["kind"] == "backfill"
    db.release_backfill_lock()
    db.close()


def test_acquire_lock_records_kind(tmp_path):
    db = _make_db(tmp_path)
    db.acquire_backfill_lock(kind="incremental")
    held = db.get_backfill_lock()
    assert held["kind"] == "incremental"
    db.release_backfill_lock()
    db.close()


def test_acquire_raises_when_live_other_pid_holds_lock(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    _inject_foreign_lock(db, kind="incremental")
    # Treat the foreign PID as alive so the lock is considered held.
    monkeypatch.setattr(Database, "_pid_is_alive", staticmethod(lambda pid: True))
    with pytest.raises(RuntimeError):
        db.acquire_backfill_lock(kind="incremental")
    db.close()


def test_acquire_index_lock_or_skip_acquires_when_free(tmp_path, monkeypatch):
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.index import _acquire_index_lock_or_skip

    monkeypatch.setattr(cfg_mod.config, "database_path", tmp_path / "free.db")

    assert _acquire_index_lock_or_skip("incremental") is True

    db = Database()
    held = db.get_backfill_lock()
    assert held is not None
    assert held["pid"] == os.getpid()
    assert held["kind"] == "incremental"
    db.release_backfill_lock()
    db.close()


def test_acquire_index_lock_or_skip_skips_when_index_run_holds_lock(
    tmp_path, monkeypatch, capsys
):
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.index import _acquire_index_lock_or_skip

    monkeypatch.setattr(cfg_mod.config, "database_path", tmp_path / "busy.db")

    db = Database()
    _inject_foreign_lock(db, kind="incremental")
    db.close()
    monkeypatch.setattr(Database, "_pid_is_alive", staticmethod(lambda pid: True))

    assert _acquire_index_lock_or_skip("incremental") is False

    out = capsys.readouterr().out
    assert "Indexing already in progress" in out
    # KM rule pops a window on these substrings — the skip line must avoid them.
    assert "error" not in out.lower()
    assert "warning" not in out.lower()


def test_acquire_index_lock_or_skip_skips_when_backfill_holds_lock(
    tmp_path, monkeypatch, capsys
):
    import mail_semantic_search.config as cfg_mod
    from mail_semantic_search.index import _acquire_index_lock_or_skip

    monkeypatch.setattr(cfg_mod.config, "database_path", tmp_path / "busy2.db")

    db = Database()
    _inject_foreign_lock(db, kind="backfill")
    db.close()
    monkeypatch.setattr(Database, "_pid_is_alive", staticmethod(lambda pid: True))

    assert _acquire_index_lock_or_skip("incremental") is False

    out = capsys.readouterr().out
    assert "Backfill in progress" in out
    assert "error" not in out.lower()
    assert "warning" not in out.lower()
