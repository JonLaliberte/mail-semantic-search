"""Sustain sentence-transformer encoding for a target duration of Metal GPU time."""

import argparse
import ctypes
import faulthandler
import os
import random
import tempfile
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu-seconds", type=float, default=750)
    parser.add_argument("--timer-library", type=Path, required=True)
    args = parser.parse_args()
    if args.gpu_seconds <= 0:
        parser.error("--gpu-seconds must be positive")
    work = Path(tempfile.mkdtemp(prefix="mss-soak-"))
    os.environ.update(
        EMAIL_DIR=str(work / "mail"),
        DATABASE_PATH=str(work / "mail.db"),
        CHROMADB_PATH=str(work / "chroma"),
        LOG_PATH=str(work / "application.log"),
        PYTHON_DOTENV_DISABLED="1",
    )
    faulthandler.dump_traceback_later(45, repeat=True)

    timer = ctypes.CDLL(str(args.timer_library.resolve()))
    timer.install_gpu_timer.restype = ctypes.c_char_p
    timer.gpu_seconds.restype = ctypes.c_double
    timer.gpu_buffers.restype = ctypes.c_uint64
    driver = timer.install_gpu_timer()
    if not driver:
        raise RuntimeError("could not attach the Metal command-buffer timer")

    import numpy as np
    import torch

    from mail_semantic_search.embedding_service import EmbeddingService

    model = EmbeddingService().model
    assert model.device.type == "mps"
    print(f"torch {torch.__version__}, {model.device}, {driver.decode()}, padding on, no cache clears", flush=True)

    def corpus():
        rng = random.Random(19)
        tokens = ("project contract invoice budget planning presentation travel hotel "
                  "repair customer automation deadline payment forecast revised").split()
        return [" ".join(rng.choices(tokens, k=rng.randint(160, 510))) for _ in range(512)]

    texts = corpus()
    model.encode(texts[:32], batch_size=32, show_progress_bar=False)
    torch.mps.synchronize()
    gpu_start = timer.gpu_seconds()
    count_start = timer.gpu_buffers()
    wall_start = time.monotonic()
    total = 0
    next_print = wall_start
    while timer.gpu_seconds() - gpu_start < args.gpu_seconds:
        embs = model.encode(texts, batch_size=32, show_progress_bar=False)
        assert embs.shape == (len(texts), 768) and np.isfinite(embs).all()
        total += len(texts)
        wall = time.monotonic()
        faulthandler.dump_traceback_later(45, repeat=True)
        if wall >= next_print:
            elapsed = wall - wall_start
            gpu = timer.gpu_seconds() - gpu_start
            print(f"encoded {total:7d} | wall {elapsed:7.1f}s | Metal GPU {gpu:7.1f}s | "
                  f"buffers {timer.gpu_buffers() - count_start} | "
                  f"driver {torch.mps.driver_allocated_memory()/2**20:.0f} MiB", flush=True)
            if elapsed > 20:
                if not 0 < gpu <= elapsed * 1.02:
                    raise RuntimeError("Metal command-buffer timing invalid")
                if gpu / elapsed < 0.1:
                    raise RuntimeError("Metal timer did not capture enough GPU work")
            next_print = wall + 10

    torch.mps.synchronize()
    faulthandler.cancel_dump_traceback_later()
    print(f"PASS | encoded {total} | wall {time.monotonic()-wall_start:.1f}s | "
          f"Metal GPU {timer.gpu_seconds()-gpu_start:.1f}s | "
          f"buffers {timer.gpu_buffers()-count_start}", flush=True)


if __name__ == "__main__":
    main()
