"""Structured-output stability test for AI-assisted task creation (app/drafting/).

"100 automated runs" per the feature spec — implemented as **10 real M3 issue descriptions x 10
repeats each**, not 100 different one-off prompts, because the actual question worth measuring is
*stability*: given the same rough description, how consistent is the structured output across
repeated calls? A flat "100/100 succeeded" number would hide whether the model is confidently
consistent or just randomly guessing something valid each time. Real descriptions (fetched live from
jira-backend's Postgres, same pattern as `vectorization-service/scripts/backfill_jira_backend.py`),
not synthetic test strings — this project's established policy of testing against real data, applied
to eval too.

Usage:
    JIRA_BACKEND_PG_DSN=postgresql://poc:poc123@localhost:5432/pocdb \
    AI_LLM_PROVIDER=openai_compatible AI_AGENT_MODEL=qwen/qwen3.6-35b-a3b \
    AI_OPENAI_BASE_URL=http://localhost:1234/v1 \
    python -m eval.task_draft_stability
"""
from __future__ import annotations

import asyncio
import os
import re
import statistics
from collections import Counter, defaultdict
from typing import List, Tuple

import asyncpg

from app.config import Settings
from app.drafting.instructor_client import build_instructor_client
from app.drafting.service import DraftResult, draft_task

N_DESCRIPTIONS = 10
REPEATS_PER_DESCRIPTION = 10  # 10 x 10 = 100 total calls

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text or "")).strip()


async def _fetch_real_descriptions(dsn: str, n: int) -> List[Tuple[str, str]]:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        rows = await pool.fetch(
            "SELECT issue_key, description FROM issues "
            "WHERE space_id = 5000018 AND description IS NOT NULL AND length(description) > 200 "
            "ORDER BY random() LIMIT $1",
            n,
        )
        return [(r["issue_key"], _strip_html(r["description"])) for r in rows]
    finally:
        await pool.close()


async def main() -> None:
    jira_dsn = os.environ.get("JIRA_BACKEND_PG_DSN", "postgresql://poc:poc123@localhost:5432/pocdb")
    descriptions = await _fetch_real_descriptions(jira_dsn, N_DESCRIPTIONS)
    print(f"fetched {len(descriptions)} real M3 issue descriptions: {[k for k, _ in descriptions]}\n")

    settings = Settings()
    client, model = build_instructor_client(settings)
    print(f"provider={settings.llm_provider} model={model}")
    print(f"plan: {len(descriptions)} descriptions x {REPEATS_PER_DESCRIPTION} repeats = "
          f"{len(descriptions) * REPEATS_PER_DESCRIPTION} calls\n")

    results: List[Tuple[str, DraftResult]] = []
    try:
        for issue_key, desc in descriptions:
            for rep in range(REPEATS_PER_DESCRIPTION):
                result = await draft_task(client, model, desc)
                results.append((issue_key, result))
                status = "DEGRADED" if result.degraded else "ok"
                print(
                    f"[{len(results):3d}/{len(descriptions) * REPEATS_PER_DESCRIPTION}] "
                    f"{issue_key} rep{rep + 1:<2} {status:8s} {result.latency_seconds:5.1f}s "
                    f"title={result.draft.title[:55]!r}"
                )
                if result.degraded:
                    # The whole point of printing this per-run, not just in the summary: without it,
                    # every failure looks identical from the console output — found the hard way
                    # (a first eval run degraded ~everything and the cause wasn't visible until this
                    # was added; see loadtest/README.md's "found via testing, not assumed" pattern).
                    print(f"      -> {result.error}")
    finally:
        await client.client.close()

    _report(results)


def _report(results: List[Tuple[str, DraftResult]]) -> None:
    n = len(results)
    degraded = [r for _, r in results if r.degraded]
    latencies = [r.latency_seconds for _, r in results]
    sorted_lat = sorted(latencies)

    print(f"\n=== SUMMARY (n={n}) ===")
    print(f"schema-valid (non-degraded): {n - len(degraded)}/{n} "
          f"({100 * (n - len(degraded)) / n:.1f}%)")
    if degraded:
        print(f"degradation errors seen: {Counter(r.error for r in degraded)}")
    print(f"latency: mean={statistics.mean(latencies):.2f}s  p50={statistics.median(latencies):.2f}s  "
          f"p95={sorted_lat[int(n * 0.95)]:.2f}s  max={max(latencies):.2f}s")

    print("\n=== per-description stability (same input, 10 repeats) ===")
    by_key: dict = defaultdict(list)
    for issue_key, r in results:
        by_key[issue_key].append(r)
    for issue_key, reps in by_key.items():
        degraded_n = sum(1 for r in reps if r.degraded)
        issue_types = Counter(r.draft.issue_type for r in reps)
        label_sets = Counter(tuple(sorted(r.draft.labels)) for r in reps)
        has_estimate = sum(1 for r in reps if r.draft.estimate_story_points is not None)
        most_common_labels, most_common_count = label_sets.most_common(1)[0]
        print(
            f"  {issue_key}: degraded={degraded_n}/{len(reps)}  "
            f"issue_type={dict(issue_types)}  "
            f"distinct_label_sets={len(label_sets)} (most common {most_common_count}/{len(reps)}: {most_common_labels})  "
            f"has_estimate={has_estimate}/{len(reps)}"
        )


if __name__ == "__main__":
    asyncio.run(main())
