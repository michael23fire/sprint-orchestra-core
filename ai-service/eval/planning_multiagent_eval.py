#!/usr/bin/env python3
"""Does the multi-agent planner actually produce better plans than the single call it replaces?

Same discipline as this project's other ablations (chunk_size 300/500/800, judge model 27b/72b,
reranker on/off, naive-RAG baseline): the sophisticated path does not get to ship on by default
because it sounds better. Two arms, one controlled variable.

  Arm A "single":     app/planning/service.py::plan_epic — one structured-output call.
  Arm B "multiagent": app/planning/graph.py — planner -> critic, bounded revision loop.

Both arms run against the SAME client and model, in the SAME invocation, on the SAME proposals — so a
score difference cannot be attributed to a model swap or a different day's server behaviour. The only
thing that differs is the orchestration.

PRE-REGISTERED DECISION RULE (written before the first run, deliberately, so the write-up can't be
shaped to fit whatever came out):
  * Default `epic_planning_multiagent_enabled=True` only if BOTH `completeness` and
    `dependency_correctness` improve by >= 0.5 (on the 1-5 scale) AND neither `non_redundancy` nor
    `estimate_reasonableness` regresses by more than 0.2.
  * Report the cost multiplier (LLM calls, latency) either way. The single-call path is strictly
    cheaper and stays available regardless of outcome.
  * The estimator node is judged separately: if `estimate_reasonableness` does not improve at all,
    that node isn't earning its call and should be cut, leaving a two-node planner<->critic graph.
    "I hypothesised three nodes, measured, and deleted one" is the honest result, not a failure.
    [APPLIED, 2026-08-05: the measured delta was exactly 0.00, so the estimator node was deleted. Arm
    B is now two nodes. The first run's raw numbers are kept at
    eval/results/planning_multiagent_eval_result_3node.json so the deletion is auditable rather than
    just asserted.]

KNOWN LIMITATION OF THIS HARNESS, found by running it twice (read before quoting any number it
prints): re-running the *unchanged* single-call arm on the same 8 proposals moved its own scores by
+0.13/+0.12/+0.26/+0.00 — the same magnitudes as the first run's measured multi-agent "improvement."
The absolute 1-5 judge saturates near 5 and drifts by ~0.25 run to run, so this instrument cannot
resolve effects below roughly that. Both runs agree on the verdict (flag off) and that conclusion is
robust; anything smaller that it reports is not. The fix is pairwise A/B judging (two anonymised plans,
"which is better"), which is immune to ceiling effects and absolute-scale drift — not implemented here.
See eval/results/planning_multiagent_comparison.md.

Judging is per-axis on 1-5 with written reasoning, never collapsed to one pass/fail number — same
principle eval/judge.py already states for grading answers. A stronger judge model than the generator
is preferred where available, on this project's own finding (eval/results/ragas_judge_model_comparison.md)
that judge capability materially changes scores on nuanced criteria.

Usage:
    ai-service/.venv/bin/python eval/planning_multiagent_eval.py \
        --judge-base-url http://localhost:1234/v1 --judge-model qwen2.5-72b-instruct
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))  # so `app.*` resolves like ai-service's own code

from pydantic import BaseModel, Field

from app.config import Settings
from app.drafting.instructor_client import build_instructor_client
from app.planning.graph import build_planning_graph, plan_epic_multiagent
from app.planning.schemas import EpicDraft, IssueDraft
from app.planning.service import _render_current_plan, plan_epic


@dataclass(frozen=True)
class PlanCase:
    name: str
    proposal: str
    challenge: str  # what this case is specifically designed to expose


# Each case seeds one specific failure mode a single-pass planner is plausibly bad at. Deliberately
# NOT ten variations of "build a feature" — a golden set that can't distinguish the two arms tells you
# nothing regardless of how many rows it has (same reasoning as docs/EVAL_GOLDEN_SET_TAXONOMY.md).
CASES: List[PlanCase] = [
    PlanCase(
        name="trivial-one-liner",
        proposal="Change the session cookie's SameSite attribute from Lax to Strict.",
        challenge="Over-decomposition: a reviewer should NOT invent five issues for a one-line change. "
                  "Tests that the critic doesn't manufacture coverage gaps to look useful.",
    ),
    PlanCase(
        name="large-multi-subsystem",
        proposal=(
            "Replace our home-grown notification system with a unified service. It needs to send "
            "email, SMS and in-app notifications, support per-user preferences and quiet hours, "
            "handle retries and dead-lettering, expose delivery status to the existing admin "
            "dashboard, and backfill the notification preferences we currently store in three "
            "different tables."
        ),
        challenge="Under-coverage: a single pass tends to drop the least-salient requirements "
                  "(backfill, dead-lettering, admin dashboard) when the proposal names many.",
    ),
    PlanCase(
        name="vague-ambiguous",
        proposal="Make the reporting section faster and easier to use. Customers keep complaining about it.",
        challenge="A proposal with no concrete requirements — tests whether the critic flags genuine "
                  "gaps or just pads the plan with generic tasks.",
    ),
    PlanCase(
        name="near-duplicate-asks",
        proposal=(
            "Add rate limiting to our public API so a single client can't overwhelm us. Also we need "
            "throttling on the public endpoints so one noisy integration doesn't degrade service for "
            "everyone else. Separately, add per-API-key request quotas with a monthly reset."
        ),
        challenge="Redundancy: the first two sentences describe the same work in different words. A "
                  "good plan merges them; a single pass often emits two near-identical issues.",
    ),
    PlanCase(
        name="ordering-dependency",
        proposal=(
            "Build a customer-facing audit log page. The UI shows a filterable timeline of account "
            "events. We'll need an events API for it to read from, and the events themselves aren't "
            "being recorded anywhere yet."
        ),
        challenge="Missing dependencies: recording -> API -> UI is a hard ordering, and a single pass "
                  "frequently emits all three with no depends_on edges at all.",
    ),
    PlanCase(
        name="mixed-complexity",
        proposal=(
            "Two things for this release: flip the feature flag that enables the new pricing page "
            "for all users (the code is already merged and tested behind the flag), and migrate all "
            "historical invoice PDFs from our old on-prem file server to S3 with checksummed "
            "verification and a fallback read path during the transition."
        ),
        challenge="Estimation specifically: a config flip and a verified bulk migration in one epic. "
                  "This is the case that decides whether a separate estimator node earns its cost.",
    ),
    PlanCase(
        name="hidden-compliance-requirement",
        proposal=(
            "Let users export all their data as a single downloadable archive from their account "
            "settings page. We're doing this because of a GDPR data-portability request we got from "
            "legal, so it has to cover every table that holds personal data."
        ),
        challenge="Coverage: 'every table that holds personal data' implies discovery/audit work that "
                  "a plan focused on the download button will skip entirely.",
    ),
    PlanCase(
        name="brownfield-with-constraint",
        proposal=(
            "Add multi-currency support to checkout. Prices are currently stored as integer cents in "
            "USD across the orders, refunds and subscriptions tables, and the accounting export job "
            "assumes a single currency."
        ),
        challenge="Coverage + ordering: schema change, three consumers, and a downstream job. Tests "
                  "whether the plan notices the accounting export at all.",
    ),
]


class PlanQualityJudgment(BaseModel):
    """Four independent axes, each 1-5 with its own reasoning — never one collapsed score. A plan can
    be complete but badly ordered, or well-ordered but redundant; averaging that into a single number
    destroys exactly the signal this eval exists to produce (same principle as eval/judge.py).
    """

    completeness_reasoning: str = Field(..., description="What the plan covers and what it misses, specifically.")
    completeness: int = Field(..., ge=1, le=5, description="5 = every requirement implied by the proposal has an issue; 1 = major requirements absent.")
    non_redundancy_reasoning: str = Field(..., description="Whether any issues substantially overlap.")
    non_redundancy: int = Field(..., ge=1, le=5, description="5 = every issue is distinct work; 1 = several issues describe the same thing.")
    dependency_correctness_reasoning: str = Field(..., description="Whether the depends_on edges match the real required ordering.")
    dependency_correctness: int = Field(..., ge=1, le=5, description="5 = required orderings present and no invented ones; 1 = clearly wrong or entirely missing.")
    estimate_reasonableness_reasoning: str = Field(..., description="Whether the story points fit the described scope, relative to each other.")
    estimate_reasonableness: int = Field(..., ge=1, le=5, description="5 = sizes are proportionate and internally consistent; 1 = clearly mis-sized.")


_AXES = ["completeness", "non_redundancy", "dependency_correctness", "estimate_reasonableness"]

_JUDGE_PROMPT = """You are an experienced engineering manager reviewing a proposed epic plan against \
the original proposal it was generated from. Score it on four independent axes, each 1-5, with a \
short specific reason for each.

Judge the plan on its own merits against the proposal — you are not comparing it to another plan and \
you have no information about how it was produced. Do not reward length: more issues is not better if \
the extra ones are padding, and a small proposal deserves a small plan. An issue with a null estimate \
is a legitimate choice, not automatically a low estimate_reasonableness score; score that axis on \
whether the estimates that ARE present are proportionate to the work described.

Original proposal:
{proposal}

Proposed plan:
{plan}"""


async def judge_plan(judge_client, judge_model: str, proposal: str, epic: EpicDraft, issues: List[IssueDraft]) -> Optional[PlanQualityJudgment]:
    try:
        return await judge_client.chat.completions.create(
            model=judge_model,
            response_model=PlanQualityJudgment,
            max_retries=2,
            max_tokens=2000,
            messages=[
                {"role": "system", "content": "You grade engineering plans. Respond only with the structured judgment."},
                {"role": "user", "content": _JUDGE_PROMPT.format(proposal=proposal, plan=_render_current_plan(epic, issues))},
            ],
        )
    except Exception as exc:  # noqa: BLE001 - a judge failure must skip one row, not kill the run
        print(f"    JUDGE FAILED: {exc}", file=sys.stderr)
        return None


def _count_calls(client) -> int:
    """instructor's client doesn't expose a call counter, so the arms are instrumented by wrapping
    `create` in the runner below. This reads the counter that wrapper maintains.
    """
    return getattr(client, "_eval_call_count", 0)


def _instrument(client):
    """Counts LLM calls without touching production code — the cost half of this eval's result. A
    wrapper here rather than a metric inside app/ so the eval measures what it measures and nothing
    ships just to make measurement easier.
    """
    completions = client.chat.completions
    original_create = completions.create
    client._eval_call_count = 0

    async def counting_create(**kwargs):
        client._eval_call_count += 1
        return await original_create(**kwargs)

    completions.create = counting_create
    return client


async def run_arms(settings: Settings, judge_base_url: str, judge_model: str, limit: Optional[int] = None) -> dict:
    cases = CASES[:limit] if limit else CASES
    client, model = build_instructor_client(settings)
    _instrument(client)
    graph = build_planning_graph(client, model)

    judge_settings = Settings(
        llm_provider="openai_compatible", agent_model=judge_model,
        openai_base_url=judge_base_url, openai_api_key="local",
    )
    judge_client, _ = build_instructor_client(judge_settings)

    rows = []
    try:
        for i, case in enumerate(cases, 1):
            print(f"\n[{i}/{len(cases)}] {case.name}")
            row = {"case": case.name, "challenge": case.challenge}

            for arm in ("single", "multiagent"):
                client._eval_call_count = 0
                start = time.perf_counter()
                if arm == "single":
                    result = await plan_epic(client, model, case.proposal)
                else:
                    result = await plan_epic_multiagent(graph, case.proposal)
                elapsed = time.perf_counter() - start
                calls = _count_calls(client)
                print(f"    {arm:11s} {len(result.plan.issues)} issues, {calls} LLM calls, {elapsed:.1f}s"
                      + (f"  DEGRADED: {result.error}" if result.degraded else ""))

                judgment = await judge_plan(judge_client, judge_model, case.proposal, result.plan.epic, result.plan.issues)
                row[arm] = {
                    "issue_count": len(result.plan.issues),
                    "llm_calls": calls,
                    # Two calls per pass (planner + critic), so a loop-back shows up as 4 not 2.
                    "revision_rounds": max(0, (calls - 2) // 2) if arm == "multiagent" else 0,
                    "latency_seconds": round(elapsed, 2),
                    "degraded": result.degraded,
                    "error": result.error,
                    "epic_title": result.plan.epic.title,
                    "issues": [i.model_dump() for i in result.plan.issues],
                    "judgment": judgment.model_dump() if judgment else None,
                }
                if judgment:
                    print(f"                scores: " + ", ".join(f"{a}={getattr(judgment, a)}" for a in _AXES))
            rows.append(row)
    finally:
        await client.client.close()
        await judge_client.client.close()

    return {"rows": rows, "means": _means(rows)}


def _means(rows: List[dict]) -> dict:
    out = {}
    for arm in ("single", "multiagent"):
        judged = [r[arm]["judgment"] for r in rows if r.get(arm, {}).get("judgment")]
        arm_means = {a: round(statistics.mean([j[a] for j in judged]), 2) for a in _AXES} if judged else {}
        arm_means["mean_llm_calls"] = round(statistics.mean([r[arm]["llm_calls"] for r in rows]), 2)
        arm_means["mean_latency_seconds"] = round(statistics.mean([r[arm]["latency_seconds"] for r in rows]), 2)
        arm_means["mean_issue_count"] = round(statistics.mean([r[arm]["issue_count"] for r in rows]), 2)
        arm_means["degraded_count"] = sum(1 for r in rows if r[arm]["degraded"])
        arm_means["judged_count"] = len(judged)
        if arm == "multiagent":
            arm_means["mean_revision_rounds"] = round(statistics.mean([r[arm]["revision_rounds"] for r in rows]), 2)
        out[arm] = arm_means
    out["deltas"] = {
        a: round(out["multiagent"].get(a, 0) - out["single"].get(a, 0), 2)
        for a in _AXES
    }
    return out


def _verdict(means: dict) -> str:
    """Applies the pre-registered decision rule from this module's docstring, mechanically, so the
    conclusion is computed rather than argued after the fact.
    """
    d = means["deltas"]
    ships = (
        d["completeness"] >= 0.5
        and d["dependency_correctness"] >= 0.5
        and d["non_redundancy"] >= -0.2
        and d["estimate_reasonableness"] >= -0.2
    )
    estimator_earns_its_cost = d["estimate_reasonableness"] > 0
    return (
        f"DECISION RULE => default the flag to {'TRUE' if ships else 'FALSE'}.\n"
        f"Estimator node => {'keep' if estimator_earns_its_cost else 'CUT (no lift in estimate_reasonableness)'}."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--judge-base-url", default="http://localhost:1234/v1")
    parser.add_argument("--judge-model", default="qwen2.5-72b-instruct")
    parser.add_argument("--limit", type=int, default=None, help="Run only the first N cases (smoke check).")
    parser.add_argument("--out", default=str(Path(__file__).parent / "results" / "planning_multiagent_eval_result.json"))
    args = parser.parse_args()

    settings = Settings()  # the deployed generating config, read from ai-service/.env
    print(f"Generator: provider={settings.llm_provider} model={settings.agent_model}")
    print(f"Judge:     {args.judge_model} @ {args.judge_base_url}")
    print(f"{len(CASES[:args.limit] if args.limit else CASES)} proposals x 2 arms\n")

    result = asyncio.run(run_arms(settings, args.judge_base_url, args.judge_model, args.limit))

    print("\n" + "=" * 78)
    for arm in ("single", "multiagent"):
        print(f"{arm:11s} {result['means'][arm]}")
    print(f"deltas      {result['means']['deltas']}")
    print("=" * 78)
    verdict = _verdict(result["means"])
    print(verdict)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "generator_model": settings.agent_model,
        "judge_model": args.judge_model,
        "verdict": verdict,
        **result,
    }, indent=2))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
