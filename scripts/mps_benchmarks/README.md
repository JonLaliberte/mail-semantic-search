# Reproduce the Apple Silicon benchmarks

Run from the repository root on macOS 15 or newer with an arm64 Python 3.14 interpreter. PR #21 used an M5 Max on macOS 27.2.1. The commands run one benchmark process at a time. They use synthetic mail and temporary indexes and logs.

## Set up two environments

The 2.10 environment installs the original baseline metadata, which permits that torch version. Both environments import the application code from the current checkout via `PYTHONPATH`, so the same benchmark switches work with both versions.

```sh
bench="$(mktemp -d)"
mkdir "$bench/baseline"
git archive 1c58c4e | tar -x -C "$bench/baseline"
python3.14 -m venv "$bench/torch210"
python3.14 -m venv "$bench/torch215"

"$bench/torch210/bin/pip" install -e "$bench/baseline" \
  torch==2.10.0 sentence-transformers==5.3.0 transformers==5.3.0 \
  tokenizers==0.22.2 chromadb==1.5.9 numpy==2.4.3 fastmcp==3.3.1 huggingface-hub==1.7.1
"$bench/torch215/bin/pip" install -e . \
  torch==2.15.0 sentence-transformers==5.3.0 transformers==5.3.0 \
  tokenizers==0.22.2 chromadb==1.5.9 numpy==2.4.3 fastmcp==3.3.1 huggingface-hub==1.7.1

export PYTHONPATH="$PWD"
export MODEL_CACHE_DIR="$bench/models"
export EMBEDDING_MODEL=BGE-base-en-v1.5
export RERANKER_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2
export BATCH_SIZE=32 RERANK_MAX_CANDIDATES=50 RERANK_MAX_TEXT_CHARS=1200
```

Point `MODEL_CACHE_DIR` at a cache that already holds both models to run offline. With the new cache above, the first run downloads both models. Set `HF_HUB_OFFLINE=1` after that to use the same cached weights throughout.

## Scheduled memory and synchronous cache clear

```sh
measure() {
  version="$1"
  shift
  "$bench/torch$version/bin/python" scripts/measure_schedule_memory.py \
    --cycles 84 --seed-emails 500 --emails-per-cycle 8 --idle-seconds 3600 "$@"
}

measure 210 --no-cache-clear
measure 210
measure 215 --no-cache-clear
measure 215
measure 215 --no-padding --no-cache-clear
measure 215 --no-padding
```

Each case seeds a mailbox, then runs 84 incremental passes and one query embedding and rerank per pass. Compare the printed first/final/peak/median footprint and median MPS driver memory. The explicit clear timer measures `torch.mps.empty_cache()` inside the scheduled pass, including the seed pass. The idle timeout keeps the idle reaper from clearing during the matrix. The driver fails if a pass skips work, uses the CPU, or returns unscored rerank results. The original PR matrix used an equivalent single-threaded clear after the baseline scheduler returned.

## Search latency and equivalence

```sh
search_trial() {
  version="$1"
  label="$2"
  shift 2
  "$bench/torch$version/bin/python" scripts/mps_benchmarks/search_latency.py \
    "$bench/search-fixture" "$bench/$label.json" "$@"
}

search_trial 210 build --build
search_trial 215 a215
search_trial 210 a210
search_trial 210 b210
search_trial 215 b215
"$bench/torch215/bin/python" scripts/mps_benchmarks/compare_search.py \
  --baseline "$bench/a210.json" "$bench/b210.json" \
  --candidate "$bench/a215.json" "$bench/b215.json"
```

The first invocation makes the 480-message baseline index and runs a trial whose timings are excluded. The next four trials reuse that index and pool 144 warm and 48 first-after-clear samples per version. The `empty-cache` row measures the synchronous PyTorch call just before a first-after-clear search. The search timer starts once that call returns. Comparison checks embedding and score tolerances, followed by exact ordered top-10 matches for every query.

## Sustained MPS encoding

Install the Xcode command-line tools for `clang`, then compile the process-local Metal command-buffer timer and run each version.

```sh
clang -dynamiclib -O2 -fobjc-arc -framework Metal -framework Foundation \
  scripts/mps_benchmarks/gpu_time.m -o "$bench/gpu_time.dylib"

for version in 215 210; do
  "$bench/torch$version/bin/python" scripts/mps_benchmarks/soak.py \
    --timer-library "$bench/gpu_time.dylib" --gpu-seconds 750
done
```

The driver encodes batches of 32 using fixed padding and leaves the cache intact. It sums the GPU start-to-end durations of completed Metal command buffers, verifies encoding outputs and prints a heartbeat. Each run should end with `PASS` after reaching at least 750 seconds of measured GPU time. A stalled run emits Python stack traces after 45 seconds without a completed encode call.
