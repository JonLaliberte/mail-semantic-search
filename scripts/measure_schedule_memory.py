"""Measure process memory across repeated scheduled index runs.

Runs the MCP server's scheduled incremental index (`IndexScheduler.run_once`)
several times in one process against a throwaway maildir, database and Chroma
directory, a few new emails per cycle, then one search (a query embedding and
a rerank of random-length candidates). After each cycle it lets the idle reaper
do what it does in the server (`release_if_idle`), then prints the process's
physical footprint (what Activity Monitor and `vmmap --summary` report, GPU
memory included), its peak so far, PyTorch's MPS memory and the Python heap.

It stops with an error instead of printing a sample when a cycle doesn't
embed what it should: a skipped or failed run, failed batches, a wrong indexed
count, or models that aren't on Apple's GPU (pass --allow-cpu to measure there
anyway). So a flat line always means the GPU did the work.

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


def memory_footprint() -> tuple:
    """(current, lifetime peak) physical footprint in bytes, as vmmap reports it.

    macOS only (proc_pid_rusage, RUSAGE_INFO_V4); elsewhere both are max RSS.
    """
    if sys.platform == "darwin":
        names = (
            "user_time system_time pkg_idle_wkups interrupt_wkups pageins wired_size "
            "resident_size phys_footprint proc_start_abstime proc_exit_abstime "
            "child_user_time child_system_time child_pkg_idle_wkups "
            "child_interrupt_wkups child_pageins child_elapsed_abstime "
            "diskio_bytesread diskio_byteswritten cpu_time_qos_default "
            "cpu_time_qos_maintenance cpu_time_qos_background cpu_time_qos_utility "
            "cpu_time_qos_legacy cpu_time_qos_user_initiated "
            "cpu_time_qos_user_interactive billed_system_time serviced_system_time "
            "logical_writes lifetime_max_phys_footprint instructions cycles "
            "billed_energy serviced_energy interval_max_phys_footprint runnable_time"
        ).split()

        class RusageInfoV4(ctypes.Structure):
            _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [(n, ctypes.c_uint64) for n in names]

        info = RusageInfoV4()
        libc = ctypes.CDLL("libc.dylib")
        if libc.proc_pid_rusage(os.getpid(), 4, ctypes.byref(info)) == 0:
            return info.phys_footprint, info.lifetime_max_phys_footprint
    import resource

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    return rss, rss


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


def fail(message: str) -> None:
    print(f"FAILED: {message}", file=sys.stderr, flush=True)
    sys.exit(1)


def search_once(rng: random.Random, resources) -> None:
    """One interactive search: a query embedding and a rerank, random lengths."""
    query = " ".join(f"word{rng.randrange(5000)}" for _ in range(rng.randrange(1, 40)))
    resources.get_embedding_service().embed_query(query)
    candidates = [
        {"document": " ".join(f"word{rng.randrange(5000)}" for _ in range(rng.randrange(1, 400)))}
        for _ in range(rng.randint(1, 30))
    ]
    resources.get_reranker().rerank(query, candidates, top_k=10)


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
    parser.add_argument(
        "--no-search", action="store_true", help="Skip the search + rerank done each cycle."
    )
    parser.add_argument(
        "--allow-cpu",
        action="store_true",
        help="Measure even when the models are not on Apple's GPU (MPS). Without "
        "it the script stops, since the GPU leak can't show up on CPU.",
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
    rng = random.Random(0)
    tracemalloc.start()
    print(f"workdir: {work}")
    print(
        "cycle  indexed  footprint_MB  delta_MB  peak_MB  mps_driver_MB  py_heap_MB  seconds"
    )
    baseline = None
    samples = []
    next_id = args.seed_emails
    expected = args.seed_emails
    for cycle in range(args.cycles + 1):
        if cycle:
            # mtime/ctime granularity: make the new files strictly newer
            # than the watermark the previous run recorded.
            time.sleep(1.1)
            expected = random.Random(cycle).randint(1, args.emails_per_cycle)
            write_emails(mail_dir, next_id, expected)
            next_id += expected
        started = time.monotonic()
        summary = scheduler.run_once()
        # A skipped or failed pass embeds nothing, so its memory says nothing.
        if not summary or not summary.get("ran"):
            fail(f"cycle {cycle}: the index run was skipped or raised ({summary!r})")
        if summary.get("batch_failures") or summary.get("interrupted"):
            fail(f"cycle {cycle}: the index run had failed batches ({summary!r})")
        if summary.get("indexed") != expected:
            fail(f"cycle {cycle}: indexed {summary.get('indexed')} emails, expected {expected}")
        if not args.no_search:
            search_once(rng, resources)
        device = resources.get_embedding_service().model.device.type
        if device != "mps" and not args.allow_cpu:
            fail(f"the embedding model is on {device}, not MPS (use --allow-cpu to measure anyway)")
        mps = mps_driver_bytes() / 2**20
        if mps == 0 and not args.allow_cpu:
            fail("PyTorch reports no MPS memory in use after an embedding run")
        resources.release_if_idle(args.idle_seconds)
        gc.collect()
        footprint, peak = (b / 2**20 for b in memory_footprint())
        heap = tracemalloc.get_traced_memory()[0] / 2**20
        if cycle == 1:
            # Cycle 0 indexes the seed mailbox; measure growth from cycle 1.
            baseline = footprint
        if cycle:
            samples.append(footprint)
        delta = "" if baseline is None else f"{footprint - baseline:+8.0f}"
        print(
            f"{cycle:5d}  {summary['indexed']:7d}  {footprint:12.0f}  {delta:>8}  "
            f"{peak:7.0f}  {mps:13.0f}  {heap:10.1f}  {time.monotonic() - started:7.1f}",
            flush=True,
        )
    if len(samples) >= 4:
        half = len(samples) // 2
        print(
            f"growth over the second half of the cycles: "
            f"{samples[-1] - samples[half]:+.0f} MB (a leak keeps this well above 0)"
        )


if __name__ == "__main__":
    main()
