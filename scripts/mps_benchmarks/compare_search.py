"""Verify search output equivalence and summarize pooled latency samples."""

import argparse
import json
from pathlib import Path

import numpy as np


def verify(baseline, candidate):
    for field, atol in (("query_vectors", 1e-6), ("document_vectors", 1e-6), ("scores", 5e-5)):
        old = np.asarray(baseline[field])
        new = np.asarray(candidate[field])
        np.testing.assert_allclose(old, new, rtol=1e-4, atol=atol)
        print(f"{field} maximum absolute difference {np.max(np.abs(old - new)):.3g}")
    for mode, expected in baseline["rankings"].items():
        if expected != candidate["rankings"][mode]:
            raise ValueError(f"{mode} returned different ordered top-10 results")
        print(f"{mode}: all {len(expected)} ordered top-10 results matched")


def summarize(trials):
    print(f"PyTorch {trials[0]['torch']}")
    for mode in trials[0]["latency_samples_ms"]:
        samples = [sample for trial in trials for sample in trial["latency_samples_ms"][mode]]
        p50, p95 = np.quantile(samples, [.5, .95])
        print(f"{mode:24} n={len(samples):3} p50 {p50:6.2f} ms p95 {p95:6.2f} ms")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", nargs="+", type=Path, required=True)
    parser.add_argument("--candidate", nargs="+", type=Path, required=True)
    args = parser.parse_args()
    baseline = [json.loads(path.read_text()) for path in args.baseline]
    candidate = [json.loads(path.read_text()) for path in args.candidate]
    for trial in baseline[1:] + candidate:
        verify(baseline[0], trial)
    summarize(baseline)
    summarize(candidate)


if __name__ == "__main__":
    main()
