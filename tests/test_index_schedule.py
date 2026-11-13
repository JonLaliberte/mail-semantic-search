"""The MCP server can own the index and run the incremental index itself.

Two processes opening the same Chroma index (the server plus a scheduled
`index --incremental`) corrupted chroma.sqlite3. With MCP_INDEX_INTERVAL_SECONDS
the server indexes on a timer and holds an OS file lock on the index while it
runs; CLI commands that open the index take the same lock, so the two can
never have the index open at once.
"""

import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime

import pytest
from click.testing import CliRunner
from fastmcp.exceptions import ToolError

import mail_semantic_search.config as cfg_mod
from mail_semantic_search import index_lock, index_schedule, mcp_server, resources
from mail_semantic_search.cli import main
from mail_semantic_search.database import Database
from mail_semantic_search.index_lock import IndexLock


@pytest.fixture(autouse=True)
def db_path(tmp_path, monkeypatch):
    """Every test gets its own database, index and lock file, never the real ones."""
    path = tmp_path / "test.db"
    monkeypatch.setattr(cfg_mod.config, "database_path", path)
    monkeypatch.setattr(cfg_mod.config, "chromadb_path", tmp_path / "chroma")
    monkeypatch.setattr(index_schedule, "_scheduler", None)
    # Never reuse a keepalive store opened against another test's index.
    _drop_keepalive()
    Database(path).close()  # create the schema
    yield path
    _drop_keepalive()


def _drop_keepalive():
    store, resources._keepalive_store = resources._keepalive_store, None
    if store is not None:
        store.close()


@contextmanager
def held_by_another_process(role):
    """Hold the index lock from a child process, as a server or CLI command would."""
    script = (
        "import fcntl, json, os, sys\n"
        f"fd = os.open({str(index_lock.lock_path())!r}, os.O_RDWR | os.O_CREAT)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "os.ftruncate(fd, 0)\n"
        f"os.pwrite(fd, json.dumps({{'pid': os.getpid(), 'role': {role!r}}}).encode(), 0)\n"
        "print('ready', flush=True)\n"
        "sys.stdin.read()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "ready"
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


# --- the lock -----------------------------------------------------------------


def test_one_holder_at_a_time_and_release_frees_it():
    first, second = IndexLock(), IndexLock()
    assert first.try_acquire("prune")
    assert first.try_acquire("prune")  # re-taking our own is fine
    assert not second.try_acquire("search")
    first.release()
    assert second.try_acquire("search")
    second.release()


def test_holder_reports_who_has_it():
    assert index_lock.holder() is None
    with held_by_another_process(index_lock.SERVER_ROLE) as proc:
        info = index_lock.holder()
        assert info["pid"] == proc.pid
        assert index_lock.describe(info) == f"the MCP server (pid={proc.pid})"
    assert index_lock.holder() is None


def test_a_killed_holder_never_leaves_a_stale_lock():
    """The kernel drops the lock however the holder dies, so PID reuse can't fake one."""
    with held_by_another_process(index_lock.SERVER_ROLE) as proc:
        proc.kill()  # SIGKILL: no cleanup code runs
        proc.wait()
        assert index_lock.holder() is None
        assert IndexLock().try_acquire("status")


def test_taking_the_lock_clears_a_stale_index_job_lock(db_path):
    """Whoever holds the file lock is alone, so a job lock from another PID is stale,
    even when that PID now belongs to some unrelated live process."""
    with Database(db_path) as db:
        db.conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?)",
            (
                Database._BACKFILL_LOCK_KEY,
                json.dumps({"pid": os.getppid(), "started_at": "x", "kind": "incremental"}),
            ),
        )
        db.conn.commit()
        assert db.get_backfill_lock() is not None
    lock = IndexLock()
    assert lock.try_acquire("index")
    with Database(db_path) as db:
        assert db.get_backfill_lock() is None
    lock.release()


# --- CLI ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["status"],
        ["search", "anything"],
        ["prune", "--dry-run"],
        ["compact-vectors", "--dry-run"],
        ["index"],
        ["index", "--incremental", "--limit", "5"],
        ["index", "--incremental", "--no-skip"],
        ["index-file", "--force", "/tmp/x.eml"],
    ],
)
def test_index_commands_refuse_while_the_server_holds_the_index(args):
    with held_by_another_process(index_lock.SERVER_ROLE) as proc:
        result = CliRunner().invoke(main, args)
    assert result.exit_code == 1
    assert f"the MCP server (pid={proc.pid})" in result.output
    assert "MCP_INDEX_INTERVAL_SECONDS" in result.output


def test_a_command_waits_its_turn_behind_another_command():
    with held_by_another_process("prune") as proc:
        result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 1
    assert f"`mail-semantic-search prune` (pid={proc.pid})" in result.output
    assert "Wait for it to finish" in result.output


@pytest.mark.parametrize(
    "args", [["index", "--incremental"], ["index-file", "/tmp/x.eml"]]
)
def test_scheduled_style_runs_skip_quietly(args):
    """Leftover cron/KM/MailMate hooks must not alert: exit 0, no alert words."""
    with held_by_another_process(index_lock.SERVER_ROLE):
        result = CliRunner().invoke(main, args)
    assert result.exit_code == 0
    assert "skipping this run" in result.output
    assert "error" not in result.output.lower()
    assert "warning" not in result.output.lower()


def test_a_command_holds_the_lock_while_it_runs_and_frees_it_after(monkeypatch):
    seen = []
    import mail_semantic_search.cli as cli_mod

    def fake_status_data():
        seen.append(index_lock.holder())
        raise RuntimeError("stop here")

    monkeypatch.setattr(cli_mod, "get_status_data", fake_status_data)
    CliRunner().invoke(main, ["status"])
    assert seen and seen[0] is not None and seen[0]["role"] == "status"
    assert index_lock.holder() is None


def test_commands_run_once_the_holder_is_gone():
    with held_by_another_process(index_lock.SERVER_ROLE):
        pass
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert "Semantic Search Status" in result.output


# --- scheduler ----------------------------------------------------------------


class _Runner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.indexing_seen = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.indexing_seen.append(resources._indexing)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _ok(indexed=0):
    return {"ran": True, "indexed": indexed, "skipped": 0, "batch_failures": 0}


SKIPPED = {"ran": False, "indexed": 0, "skipped": 0, "batch_failures": 0}
FAILED_BATCHES = {"ran": True, "indexed": 0, "skipped": 0, "batch_failures": 2}


def _no_alerts(title, msg):
    raise AssertionError(f"unexpected alert: {msg}")


@pytest.fixture
def marks(monkeypatch):
    calls = []
    monkeypatch.setattr(resources, "mark_index_current", lambda: calls.append(1))
    return calls


def _scheduler(runner, notifier=_no_alerts, interval=300, **kw):
    return index_schedule.IndexScheduler(interval, run_index=runner, notifier=notifier, **kw)


def test_run_once_runs_an_incremental_pass_with_the_shared_model(marks):
    runner = _Runner(_ok(indexed=3))
    scheduler = _scheduler(runner)

    assert scheduler.run_once()["indexed"] == 3
    (kwargs,) = runner.calls
    assert kwargs["incremental"] is True
    assert kwargs["show_progress"] is False
    assert kwargs["embedding_service_factory"] is resources.get_embedding_service
    assert kwargs["should_stop"]() is False
    # The run holds the models against idle release, and is over afterwards.
    assert runner.indexing_seen == [1]
    assert resources._indexing == 0
    assert marks == [1]
    assert scheduler.lock.held
    scheduler.lock.release()


def test_failures_notify_once_until_a_run_succeeds(marks):
    alerts = []
    runner = _Runner(RuntimeError("boom"), FAILED_BATCHES, _ok(), RuntimeError("again"))
    scheduler = _scheduler(runner, notifier=lambda title, msg: alerts.append(msg))

    assert scheduler.run_once() is None  # exception never escapes
    scheduler.run_once()  # still failing: no second alert
    assert alerts == ["RuntimeError: boom"]
    scheduler.run_once()  # recovers
    scheduler.run_once()  # fails again: alert again
    assert alerts == ["RuntimeError: boom", "RuntimeError: again"]
    # Runs that wrote mark the loaded index current, failed ones too (batches
    # before the failure were committed).
    assert len(marks) == 4
    scheduler.lock.release()


def test_a_skipped_run_neither_marks_nor_clears_the_failure(marks):
    """ran=False means another index job holds the job lock and may be writing."""
    alerts = []
    runner = _Runner(RuntimeError("boom"), SKIPPED, RuntimeError("again"))
    scheduler = _scheduler(runner, notifier=lambda title, msg: alerts.append(msg))
    for _ in range(3):
        scheduler.run_once()

    assert len(marks) == 2  # the two failures; the skip never marks
    # The failure alerted once; the skip alerted separately (in the server a
    # held job lock means a process outside the index lock); no re-alert.
    assert alerts[0] == "RuntimeError: boom"
    assert "outside the index lock" in alerts[1]
    assert len(alerts) == 2
    scheduler.lock.release()


def test_long_runs_of_skips_alert_once(marks):
    alerts = []
    n = index_schedule.SKIP_ALERT_AFTER
    runner = _Runner(*([SKIPPED] * (n + 2)), _ok(), *([SKIPPED] * (n - 1)))
    scheduler = _scheduler(runner, notifier=lambda title, msg: alerts.append(title))
    for _ in range(n + 2):
        scheduler.run_once()
    assert alerts == ["Mail search index in use elsewhere", "Mail search indexing is stuck"]
    scheduler.run_once()  # a real run resets the count
    for _ in range(n - 1):
        scheduler.run_once()
    assert alerts.count("Mail search indexing is stuck") == 1
    scheduler.lock.release()


def test_a_server_waits_for_a_command_then_takes_the_index_over(marks):
    """Restarted mid-`prune`, the server must not index until prune is done."""
    alerts = []
    runner = _Runner(_ok(indexed=1))
    scheduler = _scheduler(runner, notifier=lambda title, msg: alerts.append(msg))
    with held_by_another_process("prune") as proc:
        for _ in range(index_schedule.SKIP_ALERT_AFTER):
            assert scheduler.run_once() is None
        assert runner.calls == []
        assert alerts and f"prune` (pid={proc.pid})" in alerts[0]
    assert scheduler.run_once()["indexed"] == 1
    assert scheduler.lock.held
    scheduler.lock.release()


def test_tools_report_the_index_busy_while_a_command_holds_it(marks):
    index_schedule.ensure_index_available()  # no schedule: never blocks
    scheduler = _scheduler(_Runner())
    index_schedule._scheduler = scheduler
    with held_by_another_process("compact-vectors"):
        with pytest.raises(ToolError, match="compact-vectors"):
            index_schedule.ensure_index_available()
    index_schedule.ensure_index_available()  # free again: the server takes it
    assert scheduler.lock.held
    scheduler.lock.release()


def test_shutdown_stops_the_loop_and_keeps_the_index_until_exit(marks):
    started = threading.Event()
    release_run = threading.Event()
    stop_seen = []

    def runner(**kwargs):
        started.set()
        release_run.wait(5)
        stop_seen.append(kwargs["should_stop"]())
        return _ok()

    scheduler = _scheduler(runner, interval=30, first_delay=0)
    index_schedule._scheduler = scheduler
    scheduler.start()
    assert started.wait(5)
    threading.Timer(0.1, release_run.set).start()
    index_schedule.shutdown()
    # The run in progress finished (and saw the stop request) before exit.
    assert stop_seen == [True]
    assert not scheduler._thread.is_alive()
    # Still held (the process may have Chroma open until it exits), and late
    # tool calls are still checked against it.
    assert scheduler.lock.held
    assert index_schedule._scheduler is scheduler
    scheduler.lock.release()


def test_mark_happens_before_the_run_leaves_indexing(monkeypatch):
    seen = []
    monkeypatch.setattr(resources, "mark_index_current", lambda: seen.append(resources._indexing))
    scheduler = _scheduler(_Runner(_ok()))
    scheduler.run_once()
    assert seen == [1]
    scheduler.lock.release()


def test_server_retries_once_when_a_probe_had_the_lock(monkeypatch):
    scheduler = _scheduler(_Runner())
    attempts = iter([False, True])
    monkeypatch.setattr(scheduler.lock, "try_acquire", lambda role: next(attempts))
    monkeypatch.setattr(index_schedule, "holder", lambda: None)
    assert scheduler.claim()


def test_lock_works_when_the_file_is_read_only(tmp_path):
    path = tmp_path / "ro.lock"
    path.write_text("")
    path.chmod(0o444)
    lock = IndexLock(path)
    assert lock.try_acquire("prune")
    assert index_lock.holder(path) is not None
    lock.release()
    assert index_lock.holder(path) is None


def test_cli_refuses_to_run_without_the_lock(monkeypatch):
    def broken(self, role):
        raise OSError("no such device")

    monkeypatch.setattr(IndexLock, "try_acquire", broken)
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 1
    assert "Could not take the index lock" in result.output


def test_shutdown_keeps_the_lock_while_a_run_is_still_writing(marks):
    release_run = threading.Event()
    started = threading.Event()

    def runner(**kwargs):
        started.set()
        release_run.wait(5)
        return _ok()

    scheduler = _scheduler(runner, interval=30, first_delay=0)
    scheduler.start()
    assert started.wait(5)
    scheduler.shutdown(timeout=0.1)  # gives up waiting while the run is busy
    assert scheduler.lock.held
    release_run.set()
    scheduler._thread.join(5)
    scheduler.lock.release()


def test_startup_status_check_waits_for_the_index(monkeypatch, marks):
    class SyncThread:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            self.target()

    import threading as threading_mod

    monkeypatch.setattr(threading_mod, "Thread", SyncThread)
    monkeypatch.setattr(
        mcp_server,
        "get_status_data_payload",
        lambda: (_ for _ in ()).throw(AssertionError("opened the index while busy")),
    )
    index_schedule._scheduler = _scheduler(_Runner())
    with held_by_another_process("prune"):
        mcp_server._log_startup_status_async()  # skips instead of opening Chroma


def test_loop_never_overlaps_runs_and_stops(marks):
    state = {"active": 0, "max_active": 0, "runs": 0}
    done = threading.Event()

    def runner(**kwargs):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        state["runs"] += 1
        if state["runs"] >= 3:
            done.set()
        state["active"] -= 1
        return _ok()

    scheduler = index_schedule.IndexScheduler(
        0.01, first_delay=0, run_index=runner, notifier=_no_alerts
    )
    scheduler.start()
    assert done.wait(5)
    scheduler.shutdown(timeout=5)
    assert not scheduler._thread.is_alive()
    assert state["max_active"] == 1


def test_loop_survives_a_crashing_pass(monkeypatch):
    done = threading.Event()
    calls = []

    def run_once(self):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("unexpected")
        done.set()

    monkeypatch.setattr(index_schedule.IndexScheduler, "run_once", run_once)
    scheduler = index_schedule.IndexScheduler(0.01, first_delay=0, notifier=_no_alerts)
    scheduler.start()
    assert done.wait(5)
    scheduler.shutdown(timeout=5)
    assert len(calls) >= 2


def test_start_runs_even_when_a_command_holds_the_index(monkeypatch):
    monkeypatch.setattr(index_schedule.IndexScheduler, "start", lambda self: None)
    with held_by_another_process("prune"):
        scheduler = index_schedule.start_index_schedule(300)
        assert not scheduler.lock.held
    assert scheduler.claim()
    index_schedule.shutdown()
    scheduler.lock.release()


def test_notify_strips_control_characters_and_never_raises(monkeypatch):
    sent = []
    monkeypatch.setattr(index_schedule.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        index_schedule.subprocess, "run", lambda args, **kw: sent.append(args)
    )
    index_schedule.notify("t", 'bad\x00 "quoted" \\ path')
    (args,) = sent
    assert "\x00" not in args[2]
    assert '\\"quoted\\"' in args[2]

    def boom(*a, **kw):
        raise ValueError("embedded null byte")

    monkeypatch.setattr(index_schedule.subprocess, "run", boom)
    index_schedule.notify("t", "x")  # must not raise


# --- server startup -------------------------------------------------------------


def test_server_never_schedules_over_stdio(monkeypatch):
    started = []
    monkeypatch.setattr(index_schedule, "start_index_schedule", started.append)
    monkeypatch.setattr(mcp_server.signal, "signal", lambda *a: None)
    monkeypatch.setattr(mcp_server.atexit, "register", lambda f: None)

    monkeypatch.setenv("MCP_INDEX_INTERVAL_SECONDS", "300")
    mcp_server._start_index_schedule("stdio")
    monkeypatch.setenv("MCP_INDEX_INTERVAL_SECONDS", "0")
    mcp_server._start_index_schedule("http")
    assert started == []
    monkeypatch.setenv("MCP_INDEX_INTERVAL_SECONDS", "300")
    mcp_server._start_index_schedule("http")
    assert started == [300]


def test_scheduled_server_exits_cleanly_on_sigterm(monkeypatch):
    handlers = {}
    registered = []
    monkeypatch.setattr(index_schedule, "start_index_schedule", lambda i: None)
    monkeypatch.setattr(mcp_server.signal, "signal", lambda sig, h: handlers.setdefault(sig, h))
    monkeypatch.setattr(mcp_server.atexit, "register", registered.append)
    monkeypatch.setenv("MCP_INDEX_INTERVAL_SECONDS", "300")

    mcp_server._start_index_schedule("http")
    assert registered == [index_schedule.shutdown]
    with pytest.raises(SystemExit):
        handlers[mcp_server.signal.SIGTERM](mcp_server.signal.SIGTERM, None)


@pytest.mark.parametrize(
    "raw,expected", [(None, 0.0), ("0", 0.0), ("300", 300.0), ("30", 30.0)]
)
def test_index_interval_seconds(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("MCP_INDEX_INTERVAL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("MCP_INDEX_INTERVAL_SECONDS", raw)
    assert index_schedule.index_interval_seconds() == expected


@pytest.mark.parametrize("raw", ["soon", "inf", "nan", "1e10", "5", "-5"])
def test_index_interval_seconds_rejects_unusable_values(monkeypatch, raw):
    monkeypatch.setenv("MCP_INDEX_INTERVAL_SECONDS", raw)
    with pytest.raises(SystemExit):
        index_schedule.index_interval_seconds()


def test_a_bad_interval_never_stops_a_stdio_server(monkeypatch):
    monkeypatch.setenv("MCP_INDEX_INTERVAL_SECONDS", "soon")
    mcp_server._start_index_schedule("stdio")  # warns, doesn't exit


# --- index_emails summary -------------------------------------------------------


def test_index_emails_reports_a_skip_when_the_job_lock_is_held(tmp_path, monkeypatch):
    from mail_semantic_search import index as index_mod

    monkeypatch.setattr(cfg_mod.config, "email_dir", tmp_path)
    monkeypatch.setattr(index_mod, "_acquire_index_lock_or_skip", lambda kind: False)
    summary = index_mod.index_emails(incremental=True, show_progress=False)
    assert summary["ran"] is False


def test_index_emails_reports_an_empty_run(tmp_path, monkeypatch):
    from mail_semantic_search import index as index_mod

    mail = tmp_path / "mail"
    mail.mkdir()
    monkeypatch.setattr(cfg_mod.config, "email_dir", mail)

    def no_model():
        raise AssertionError("no mail, so no model load")

    summary = index_mod.index_emails(
        incremental=True, show_progress=False, embedding_service_factory=no_model
    )
    assert summary == {
        "ran": True,
        "indexed": 0,
        "skipped": 0,
        "batch_failures": 0,
        "interrupted": False,
    }


def test_index_emails_stops_between_batches_without_moving_the_watermark(
    db_path, tmp_path, monkeypatch
):
    from mail_semantic_search import index as index_mod

    mail = tmp_path / "mail"
    mail.mkdir()
    (mail / "1.eml").write_text(
        "From: a@example.com\nTo: b@example.com\nSubject: hi\n"
        "Message-ID: <1@example.com>\nDate: Mon, 1 Jan 2024 00:00:00 +0000\n\nbody\n"
    )
    monkeypatch.setattr(cfg_mod.config, "email_dir", mail)

    def no_model():
        raise AssertionError("stopped before embedding")

    summary = index_mod.index_emails(
        incremental=True,
        show_progress=False,
        embedding_service_factory=no_model,
        should_stop=lambda: True,
    )
    assert summary["interrupted"] is True
    assert summary["indexed"] == 0
    with Database(db_path) as db:
        assert db.get_incremental_scan_watermark() is None
