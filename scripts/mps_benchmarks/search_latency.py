"""Time MPS search and reranking against a reusable synthetic index."""

import argparse
import itertools
import json
import os
import random
import time
from pathlib import Path

import numpy as np
import torch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("work", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--repeats", type=int, default=3, help="Warm passes over the 24 queries.")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if not torch.backends.mps.is_available():
        parser.error("this benchmark requires MPS")
    work = args.work.absolute()
    mail = work / "mail"
    fixture_ready = work / "synthetic-fixture.ready"
    if args.build:
        work.mkdir()
    elif not fixture_ready.is_file():
        parser.error("pass --build with a new fixture directory first")
    os.environ.update(
        EMAIL_DIR=str(mail),
        DATABASE_PATH=str(work / "mail.db"),
        CHROMADB_PATH=str(work / "chroma"),
        LOG_PATH=str(work / "application.log"),
        PYTHON_DOTENV_DISABLED="1",
    )

    from mail_semantic_search import resources
    from mail_semantic_search.index_schedule import IndexScheduler
    from mail_semantic_search.search import search_email_records
    from mail_semantic_search.service_models import SearchRequest

    topics = [
        ("flight confirmation and itinerary", "airport seat baggage flight ticket terminal check in airline boarding pass"),
        ("quarterly board budget and forecast", "financial revenue expense forecast budget planning quarterly board accounting"),
        ("landlord lease renewal and security deposit", "lease rent renewal landlord apartment deposit tenancy agreement building move"),
        ("electrician quote for home repairs", "electrician contractor outlet repair circuit rewiring electrical garage estimate quote"),
        ("legal review of the software contract", "agreement license terms renewal contract liability confidential software legal counsel"),
        ("invoice for the contractor's kitchen project", "invoice bathroom kitchen contractor countertop payment receipt balance labor supplies"),
        ("hotel reservation and cancellation", "hotel room booking arrival departure nights refund cancellation reservation guest"),
        ("training presentation for new hires", "training onboarding handbook orientation induction presentation documentation new hires team"),
        ("order replacement for a broken monitor", "order shipment delivery monitor return merchandise warehouse replacement serial support"),
        ("marketing launch presentation", "campaign campaign launch advertisement slides marketing audience social brand release"),
        ("security audit report and infrastructure", "security audit datacenter infrastructure access credential mitigation identity report"),
        ("neighborhood community meeting", "neighbors committee planning community zoning volunteer meeting committee minutes"),
    ]
    rng = random.Random(42)
    if args.build:
        mail.mkdir()
        for i in range(480):
            topic, terms = topics[i % len(topics)]
            text = " ".join(rng.choices(terms.split(), k=rng.randint(10, 380)))
            (mail / f"{i}.eml").write_text(
                "From: jan@example.com\nTo: me@example.com\n"
                f"Subject: {topic} reference {i}\n"
                "Date: Thu, 21 May 2026 14:00:00 +0000\n"
                f"Message-ID: <fixture-{i}@example.com>\n\n{text}\n"
            )
        summary = IndexScheduler(interval=300).run_once()
        if not summary or summary["indexed"] != 480 or summary["batch_failures"]:
            raise RuntimeError(f"could not build the 480-message fixture ({summary!r})")
        fixture_ready.touch()

    embed = resources.get_embedding_service()
    rerank = resources.get_reranker()
    assert embed.model.device.type == rerank.model.device.type == "mps"
    queries = [topic for topic, _ in topics] + [terms for _, terms in topics]
    vectors = np.array(embed.embed_texts(queries))
    rng = random.Random(91)
    texts = [
        " ".join(rng.choices(terms.split(), k=length))
        for (_, terms), length in itertools.product(topics, (12, 70, 170, 270))
    ]
    doc_vectors = np.array(embed.embed_texts(texts))
    pairs = [(queries[i % len(queries)], text) for i, text in enumerate(texts)]
    scores = rerank.model.predict(pairs).tolist()
    rank = rerank.rerank
    rerank_times = []

    def timed_rerank(*args, **kwargs):
        start = time.perf_counter()
        result = rank(*args, **kwargs)
        rerank_times.append((time.perf_counter() - start) * 1000)
        return result

    rerank.rerank = timed_rerank
    all_times = {}
    samples = {}
    rankings = {}
    for mode in ("vector", "rerank", "rerank-after-clear"):
        elapsed = []
        clear_times = []
        rankings[mode] = []
        count = len(queries) if mode == "rerank-after-clear" else len(queries) * args.repeats
        with resources.in_use():
            for i in range(-4, count):
                query = queries[i % len(queries)]
                req = SearchRequest(query=query, rerank=(mode != "vector"), auto_filters=False, limit=10)
                if mode == "rerank-after-clear":
                    clear_start = time.perf_counter()
                    torch.mps.empty_cache()
                    if i >= 0:
                        clear_times.append((time.perf_counter() - clear_start) * 1000)
                start = time.perf_counter()
                result = search_email_records(req)
                spent = (time.perf_counter() - start) * 1000
                assert len(result.results) == 10 and result.rerank_applied == (mode != "vector")
                if req.rerank:
                    assert all(isinstance(row.get("rerank_score"), float) for row in result.results)
                if i == -1:
                    rerank_times.clear()
                if i >= 0:
                    elapsed.append(spent)
                    rankings[mode].append([row["message_id"] for row in result.results])
        all_times[mode] = [round(float(x), 3) for x in np.quantile(elapsed, [.5, .95])]
        samples[mode] = elapsed
        if clear_times:
            samples["empty-cache"] = clear_times
            all_times["empty-cache"] = [round(float(x), 3) for x in np.quantile(clear_times, [.5, .95])]
        if rerank_times:
            all_times[f"{mode}-only"] = [round(float(x), 3) for x in np.quantile(rerank_times, [.5, .95])]
            samples[f"{mode}-only"] = rerank_times.copy()
            rerank_times.clear()

    data = {
        "torch": torch.__version__,
        "query_vectors": vectors.tolist(),
        "document_vectors": doc_vectors.tolist(),
        "scores": scores,
        "rankings": rankings,
        "latency_samples_ms": samples,
    }
    args.output.write_text(json.dumps(data))
    print(torch.__version__, all_times, flush=True)


if __name__ == "__main__":
    main()
