#!/usr/bin/env python3
"""Sprint-recovery diagnosis eval — measures, doesn't assume, how often the confidence-gate
(`clarification_question`) actually fires and what the plan-generation issue-key validation actually
catches, against every real sprint in the AtlasCart corpus (space 5000014) — not synthetic scenarios
crafted to guarantee a specific branch fires.

**Why real sprints, not seeded/rigged ones, and what that trade-off costs.** `RootCauseHypothesis`
grounding and `_validate_plan_issue_keys` are already proven to work *when triggered* — that's what
`tests/test_sprint_recovery_graph.py`'s fake-client tests are for, same shape as Case Study 29's
"deliberately broken plan" probe. What those tests cannot answer is "how often does this actually
matter on data nobody rigged" — the exact question Case Study 29's own re-run answered honestly (a
generator model's one-shot output often leaves little for a review step to catch). This script runs
the real `diagnose` step against the 7 real AtlasCart sprints and reports the true rate, whatever it
turns out to be. Small sample (n=7, all of them, not a subsample) — this is a directional measurement,
not a statistically powered study, named plainly rather than dressed up as more than it is.

Usage: python eval/sprint_recovery_eval.py [--url http://localhost:8200] [--space-id 5000014]
No third-party deps — stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

SPRINTS = [
    (5000068, "AtlasCart Sprint 1"), (5000069, "AtlasCart Sprint 2"), (5000070, "AtlasCart Sprint 3"),
    (5000071, "AtlasCart Sprint 4"), (5000072, "AtlasCart Sprint 5"), (5000073, "AtlasCart Sprint 6"),
    (5000074, "AtlasCart Sprint 7"),
]


def _post(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8200")
    parser.add_argument("--space-id", type=int, default=5000014)
    args = parser.parse_args()
    headers = {"X-User-Id": "5000154", "X-Username": "atlascart.maya"}

    print(f"sprint-recovery diagnosis eval -> {args.url}, {len(SPRINTS)} real sprints (space {args.space_id})")
    print("=" * 88)

    rows = []
    for sprint_id, sprint_name in SPRINTS:
        start = time.perf_counter()
        try:
            result = _post(
                f"{args.url}/sprint-recovery/start",
                {"spaceId": args.space_id, "sprintId": sprint_id, "sprintName": sprint_name},
                headers,
            )
        except urllib.error.HTTPError as exc:
            print(f"[ERROR] {sprint_name}: HTTP {exc.code} {exc.read().decode()[:200]}")
            continue
        elapsed = time.perf_counter() - start

        status = result["status"]
        n_signals = result["riskSignalCount"]
        n_hypotheses = len(result.get("hypotheses", []))
        clarifying = status == "diagnosing" or bool(result.get("clarificationQuestion"))
        n_plans = len(result.get("plans", []))
        n_actions = sum(len(p["actions"]) for p in result.get("plans", []))
        rows.append({
            "sprint": sprint_name, "status": status, "signals": n_signals, "hypotheses": n_hypotheses,
            "clarifying_question_fired": clarifying, "plans": n_plans, "actions": n_actions,
            "seconds": round(elapsed, 1),
        })
        tag = "[CLARIFY]" if clarifying else "[PLANNED]" if n_plans else "[NO-PLAN]"
        print(f"{tag} {sprint_name}: {n_signals} risk signal(s), {n_hypotheses} hypothesis(es), "
              f"{n_plans} plan(s)/{n_actions} action(s), {elapsed:.1f}s")
        if clarifying:
            print(f"          clarifying question: {result.get('clarificationQuestion')!r}")

    print("=" * 88)
    n = len(rows)
    if n == 0:
        print("No sprints completed — nothing to report.")
        return 1
    clarify_rate = sum(r["clarifying_question_fired"] for r in rows) / n
    zero_plan_rate = sum(r["plans"] == 0 and not r["clarifying_question_fired"] for r in rows) / n
    avg_signals = sum(r["signals"] for r in rows) / n
    avg_actions_per_plan = sum(r["actions"] for r in rows) / max(1, sum(r["plans"] for r in rows))

    print(f"SUMMARY (n={n} real sprints, not seeded/rigged):")
    print(f"  confidence-gate (clarifying question) fired on {sum(r['clarifying_question_fired'] for r in rows)}/{n} "
          f"({clarify_rate:.0%})")
    print(f"  avg deterministic risk signals per sprint: {avg_signals:.1f}")
    print(f"  avg actions per generated plan: {avg_actions_per_plan:.1f}")
    print(f"  sprints that reached plan generation with zero valid actions after issue-key "
          f"validation: {zero_plan_rate:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
