"""Measure process memory across repeated scheduled index runs.

Runs the MCP server's scheduled incremental index (`IndexScheduler.run_once`)
several times in one process against a throwaway maildir, database and Chroma
directory, a few new emails per cycle. After each run it lets the idle reaper
do what it does in the server (`release_if_idle`), then prints the process's
physical footprint (what Activity Monitor and `vmmap --summary` report, GPU
memory included) and the Python heap (tracemalloc).

Never touches your real index: every path points into a temporary directory.
Only the model cache is shared (read-only), so no download is needed if the
model is already cached:

    MODEL_CACHE_DIR=/path/to/data/models \
        .venv/bin/python scripts/measure_schedule_memory.py --cycles 20

Memory that keeps climbing cycle after cycle is a leak; a server running every
five minutes does ~290 cycles a day.
"""

import argparse
import ctypes
import gc
import os
import random
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path


def physical_footprint() -> int:
    """Bytes of physical footprint (macOS); falls back to max RSS elsewhere."""
    if sys.platform == "darwin":

        class RusageInfoV2(ctypes.Structure):
            _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [
                (name, ctypes.c_uint64)
                for name in (
                    "ri_user_time ri_system_time ri_pkg_idle_wkups ri_interrupt_wkups "
                    "ri_pageins ri_wired_size ri_resident_size ri_phys_footprint "
                    "ri_proc_start_abstime ri_proc_exit_abstime ri_child_user_time "
                    "ri_child_system_time ri_child_pkg_idle_wkups ri_child_interrupt_wkups "
                    "ri_child_pageins ri_child_elapsed_abstime ri_diskio_bytesread "
                    "ri_diskio_byteswritten"
                ).split()
            ]

        libc = ctypes.CDLL("libc.dylib")
        info = RusageInfoV2()
        if libc.proc_pid_rusage(os.getpid(), 2, ctypes.byref(info)) == 0:
            return info.ri_phys_footprint
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def mps_driver_bytes() -> int:
    """Metal memory PyTorch's MPS backend holds (0 when torch or MPS is absent)."""
    torch = sys.modules.get("torch")
    if torch is None or not torch.backends.mps.is_available():
        return 0
    return torch.mps.driver_allocated_memory()


def write_emails(mail_dir: Path, start: int, count: int) -> None:
    box = mail_dir / "INBOX.mailbox" / "Messages"
    box.mkdir(parents=True, exist_ok=True)
    for n in range(start, start + count):
        # Vary the length (and so the token count) like real mail does:
        # every distinct input shape costs the GPU backend its own buffers.
        rng = random.Random(n)
        body = " ".join(f"word{rng.randrange(5000)}" for _ in range(rng.randrange(1, 200)))
        (box / f"{n}.eml").write_text(
            f"From: sender{n}@example.com\n"
            f"To: me@example.com\n"
            f"Subject: Test message {n}\n"
            f"Date: Mon, 1 Jan 2024 00:00:00 +0000\n"
            f"Message-ID: <msg{n}@example.com>\n"
            "Content-Type: text/plain; charset=utf-8\n\n"
            f"{body}\n"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument(
        "--emails-per-cycle", type=int, default=8, help="Up to this many new emails per cycle."
    )
    parser.add_argument("--seed-emails", type=int, default=500)
    parser.add_argument(
        "--idle-seconds",
        type=float,
        default=0.0,
        help="Idle timeout given to release_if_idle after each run (0 = due at once, "
        "like the server's 600s timeout after a 5-minute interval).",
    )
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="mss-memory-"))
    mail_dir = work / "Messages"
    os.environ.update(
        EMAIL_DIR=str(mail_dir),
        DATABASE_PATH=str(work / "database.db"),
        CHROMADB_PATH=str(work / "chromadb"),
        INCREMENTAL_OVERLAP_SECONDS="0",
    )
    os.environ.setdefault("MODEL_CACHE_DIR", str(work / "models"))
    write_emails(mail_dir, 0, args.seed_emails)

    from mail_semantic_search import resources
    from mail_semantic_search.index_lock import IndexLock
    from mail_semantic_search.index_schedule import IndexScheduler

    scheduler = IndexScheduler(
        interval=300, first_delay=0, notifier=lambda *_: None, lock=IndexLock()
    )
    tracemalloc.start()
    print(f"workdir: {work}")
    print("cycle  indexed  footprint_MB  delta_MB  mps_driver_MB  py_heap_MB  seconds")
    baseline = None
    next_id = args.seed_emails
    for cycle in range(args.cycles + 1):
        if cycle:
            # mtime/ctime granularity: make the new files strictly newer
            # than the watermark the previous run recorded.
            time.sleep(1.1)
            count = random.Random(cycle).randint(1, args.emails_per_cycle)
            write_emails(mail_dir, next_id, count)
            next_id += count
        started = time.monotonic()
        summary = scheduler.run_once() or {}
        resources.release_if_idle(args.idle_seconds)
        gc.collect()
        footprint = physical_footprint() / 2**20
        mps = mps_driver_bytes() / 2**20
        heap = tracemalloc.get_traced_memory()[0] / 2**20
        if cycle == 1:
            # Cycle 0 indexes the seed mailbox; measure growth from cycle 1.
            baseline = footprint
        delta = "" if baseline is None else f"{footprint - baseline:+8.0f}"
        print(
            f"{cycle:5d}  {summary.get('indexed', 0):7d}  {footprint:12.0f}  "
            f"{delta:>8}  {mps:13.0f}  {heap:10.1f}  {time.monotonic() - started:7.1f}",
            flush=True,
        )


if __name__ == "__main__":
    main()
