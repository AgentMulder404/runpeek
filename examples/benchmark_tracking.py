"""Measure local tracking overhead without making any model calls."""
from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path

from runpeek.tracking import Tracker, task

with tempfile.TemporaryDirectory() as directory:
    timings = []
    tracker = Tracker(Path(directory) / "bench.db", capacity=12000)
    started = time.perf_counter()
    with task("benchmark"):
        for i in range(10000):
            before = time.perf_counter()
            tracker.record(provider="openai", model="gpt-5", request_id=f"request-{i}",
                           input_tokens=100, output_tokens=10)
            timings.append((time.perf_counter() - before) * 1000)
    result = tracker.close(timeout=30)
    print(json.dumps({"events": len(timings), "enqueue_median_ms": statistics.median(timings),
                      "enqueue_p95_ms": sorted(timings)[9499],
                      "total_seconds": time.perf_counter() - started, "counters": result,
                      "tracking_model_tokens": 0}, indent=2))
