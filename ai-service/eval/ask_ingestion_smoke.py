#!/usr/bin/env python3
"""One-click ingestion smoke check for the Ask-AI (/ask) endpoint.

This is the *light, deterministic* companion to eval/run_agentic_eval.py — NOT a replacement. That
one runs CragAgent in-process and grades answer quality with an LLM judge; this one hits the **live**
service over HTTP and asks a fixed battery of questions whose answers are pinned to real facts in the
AtlasCart corpus (space 5000014). Its job is narrow and blunt: prove each *kind* of content actually
made it into the index and is reachable through the real API — issue text, comments, sprint goals,
change history, structured counts — and flag the one place it currently hasn't (see GAP below).

Why HTTP + hardcoded ground truth instead of the LLM judge:
  * It answers a different question — "is ingestion good enough to stop working on it?" — with a
    yes/no you can read in one screen, per content type, against the actually-running stack.
  * Every check here is deterministic (a count, an issue key, an abstention flag), so a FAIL is
    unambiguous and needs no second model or API key to interpret.
  * The same question bank doubles as the manual UI script: `--list` prints it copy-paste-ready.

Ground truth was read straight from the vecdb on 2026-07-25 (docker exec poc-vecdb psql ...). If the
corpus is re-seeded, regenerate GROUND_TRUTH with the queries noted next to each field.

Usage:
    # run the whole battery against the live service (default http://localhost:8200)
    python eval/ask_ingestion_smoke.py

    # just print the questions (numbered, with pass criteria) to paste into the Ask-AI UI
    python eval/ask_ingestion_smoke.py --list

    # point at a different host / space
    python eval/ask_ingestion_smoke.py --url http://localhost:8200 --space-ids 5000014

No third-party deps — stdlib only, so it runs with any python3 without activating the venv.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, List, Optional

# --- Ground truth: AtlasCart, space 5000014 (read from vecdb 2026-07-25) -------------------------
# Regenerate with, e.g.:
#   docker exec -e PGPASSWORD=vec123 poc-vecdb psql -U vec -d vecdb -At -c \
#     "SELECT issue_type,count(*) FROM issues WHERE space_id=5000014 GROUP BY issue_type"
GT = {
    "total_issues": 83,
    "bugs": 11,
    "open_bugs": 0,             # all 11 bugs are status=done
    "sprints": 7,              # Sprint 1-6 completed, Sprint 7 active
    "reopened": ["ATC-30", "ATC-68"],   # status field_change with from_value='done'
    "only_blocked_issue": "ATC-77",     # the sole blocked issue — catalog caching, NOT checkout
    "double_click_bug": "ATC-43",       # "Checkout can create two orders from a double click"
    "last_completed_sprint_points": 37, # Sprint 6 completed_points; goal mentions "catalog"/"import"
    "most_recent_issue": "ATC-77",      # max(updated_at)
    "latest_sprint_goal_words": ["accessible", "observable", "beta"],  # Sprint 7 goal
    "active_sprint_issue_count": 8,     # Sprint 7 has 8 issues (after the sprint_id backfill)
    # ATC-68's reopen field_change carries the reason in its OWN `description` column (not a comment)
    # — the fact _format_issue_history used to silently drop for every field_change event.
    "atc68_reopen_reason_words": ["resend", "email"],
    # ATC-30's reason lives ONLY in a comment (its field_change description is a bare "Updated
    # status") — the case get_issue_comments (Plan B) exists for.
    "atc30_reopen_reason_words": ["browser", "twice"],
}

# --- HTTP client (stdlib) -----------------------------------------------------------------------


@dataclass
class AskResult:
    answer: str
    abstained: bool
    retrieval_rounds: int
    queries_used: List[str]
    citation_keys: List[str]
    error: Optional[str] = None

    @property
    def tool_label(self) -> str:
        """Coarse inference of which tool class fired. The /ask response records semantic queries
        (queriesUsed) and the *total* tool-call count (retrievalRounds) but NOT which structured tool
        ran, so this is deliberately a class-level label, not the exact tool name."""
        if self.retrieval_rounds == 0:
            return "NO-TOOL (answered without retrieving!)"
        n_sem = len(self.queries_used)
        if n_sem == 0:
            return "structured-only"
        if self.retrieval_rounds == n_sem:
            return "semantic-only"
        return "mixed (semantic+structured)"


def ask(base_url: str, question: str, space_ids: List[int], history: List[dict]) -> AskResult:
    payload = json.dumps({"question": question, "spaceIds": space_ids, "history": history}).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/ask", data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return AskResult("", False, 0, [], [], error=f"HTTP {e.code}: {e.read().decode()[:200]}")
    except Exception as e:  # noqa: BLE001 - a smoke tool; any failure is just a reported ERROR row
        return AskResult("", False, 0, [], [], error=f"{type(e).__name__}: {e}")
    return AskResult(
        answer=body.get("answer", ""),
        abstained=body.get("abstained", False),
        retrieval_rounds=body.get("retrievalRounds", 0),
        queries_used=body.get("queriesUsed", []),
        citation_keys=[c.get("issueKey", "") for c in body.get("citations", [])],
    )


# --- Check helpers: each returns (ok: bool | None, note: str). None => human-judgment (CHECK). -----


def _has(text: str, *needles: str) -> bool:
    low = text.lower()
    return any(n.lower() in low for n in needles)


def _has_all(text: str, needles) -> bool:
    low = text.lower()
    return all(n.lower() in low for n in needles)


@dataclass
class Question:
    n: int
    facet: str
    text: str
    criteria: str                       # human-readable PASS description (also shown by --list)
    check: Callable[[AskResult], tuple]  # (ok, note); ok True/False/None(=manual)
    # A follow-up turn: (question, checker) run with the first turn threaded into history.
    followup: Optional[tuple] = None


# --- The battery --------------------------------------------------------------------------------
# 17 questions, each targeting a distinct code path / content type. Grouped by what they prove.

QUESTIONS: List[Question] = [
    # A. Ingestion completeness — the real "is it good enough" gates -----------------------------
    Question(
        1, "comment-ingest",
        "How was the double-click checkout bug reproduced, and what did the request logs show?",
        f"not abstained; cites {GT['double_click_bug']}; mentions a repro/log detail that only "
        "lives in a COMMENT (e.g. clicking twice before the spinner, HTTP 201, no proxy retry).",
        lambda r: (
            (not r.abstained)
            and _has(r.answer + " ".join(r.citation_keys), GT["double_click_bug"])
            and _has(r.answer, "twice", "spinner", "201", "proxy", "slow 3g", "chrome"),
            "comment text retrievable" if not r.abstained else "abstained — comments not found?",
        ),
    ),
    Question(
        2, "sprint-goal-semantic",
        "Which sprint was focused on making the storefront accessible and observable?",
        "not abstained; identifies Sprint 7 via its GOAL text (chunk_type=sprint), not a "
        "similarly-worded issue.",
        lambda r: (
            (not r.abstained) and _has(r.answer, "sprint 7", "5000074"),
            "sprint-goal chunk reachable",
        ),
    ),
    Question(
        3, "history-reopened",
        "Which issues were reopened after being marked done, and who reopened them?",
        f"names both {GT['reopened'][0]} and {GT['reopened'][1]} (the only two that left 'done'); "
        "also states ATC-68's OWN reopen reason (from get_issue_history's description field, not a "
        "comment lookup) — regression guard for _format_issue_history silently dropping field_change "
        "descriptions.",
        lambda r: (
            _has_all(r.answer, GT["reopened"]) and _has(r.answer, *GT["atc68_reopen_reason_words"]),
            f"expected {GT['reopened']} plus ATC-68 reason words {GT['atc68_reopen_reason_words']}",
        ),
    ),
    Question(
        4, "recency",
        "What is the single most recently updated issue?",
        f"identifies {GT['most_recent_issue']} (max updated_at).",
        lambda r: (_has(r.answer, GT["most_recent_issue"]), f"expected {GT['most_recent_issue']}"),
    ),
    # B. Counting / metadata correctness — semantic search can't do these ------------------------
    Question(
        5, "count-filter",
        "How many bugs are there, and how many of them are still open (not done)?",
        f"exact bug count {GT['bugs']}; states {GT['open_bugs']} are still open (all are done).",
        lambda r: (
            _has(r.answer, str(GT["bugs"]))
            and _has(r.answer, "0", "zero", "none", "all", "all done"),
            f"expected bugs={GT['bugs']}, open={GT['open_bugs']}",
        ),
    ),
    Question(
        6, "count-total",
        "How many issues are there in total in this workspace?",
        f"exact total {GT['total_issues']}.",
        lambda r: (_has(r.answer, str(GT["total_issues"])), f"expected {GT['total_issues']}"),
    ),
    Question(
        7, "sprint-velocity",
        "How many story points did we complete in the last completed sprint, and what was that "
        "sprint's goal?",
        f"{GT['last_completed_sprint_points']} points (Sprint 6) + its goal (searchable catalog / "
        "auditable import).",
        lambda r: (
            _has(r.answer, str(GT["last_completed_sprint_points"])) and _has(r.answer, "catalog"),
            f"expected {GT['last_completed_sprint_points']} pts + catalog goal",
        ),
    ),
    # C. Disambiguation / grounding — the hardest; test resistance to false positives -------------
    Question(
        8, "disambiguation-blocked",
        "What is currently blocking checkout or payments?",
        f"CORRECT = says nothing checkout/payment-related is blocked (the only blocked issue, "
        f"{GT['only_blocked_issue']}, is about catalog caching) OR abstains. FAIL = claims "
        f"{GT['only_blocked_issue']} blocks checkout.",
        lambda r: (
            None,  # human judgment
            f"WARN: names {GT['only_blocked_issue']} as a blocker — likely false positive"
            if _has(r.answer, GT["only_blocked_issue"]) and _has(r.answer, "block")
            else "no obvious false-positive",
        ),
    ),
    Question(
        9, "false-link",
        f"Does {GT['double_click_bug']} block ATC-59?",
        "CORRECT = no evidence of such a link (they only share 'duplicate/double' wording). "
        "FAIL = asserts a blocking relationship.",
        lambda r: (
            None,
            "WARN: asserts a link" if _has(r.answer, "yes", "blocks", "does block") else "no assert",
        ),
    ),
    Question(
        10, "abstention",
        "What was our AWS bill last month?",
        "abstains — this is not in the knowledge base.",
        lambda r: (r.abstained, "must abstain"),
    ),
    # D. Distinct code paths — follow-up, lexical, breadth, multi-hop -----------------------------
    Question(
        11, "multi-turn-followup",
        "What is the latest sprint?",
        "REGRESSION GUARD: turn 2 ('Does it have a goal?') must answer from a fresh tool call, not "
        "falsely abstain. PASS = turn 2 states the Sprint 7 goal.",
        lambda r: ((not r.abstained), "turn 1 should identify a sprint"),
        followup=(
            "Does it have a goal?",
            lambda r: (
                (not r.abstained) and _has(r.answer, *GT["latest_sprint_goal_words"], "sprint 7"),
                "turn 2 must NOT abstain and should state the goal",
            ),
        ),
    ),
    Question(
        12, "lexical-exact-key",
        f"Show me the details of {GT['double_click_bug']}.",
        f"exact-key lookup returns {GT['double_click_bug']} (lexical path).",
        lambda r: (
            (not r.abstained) and _has(r.answer + " ".join(r.citation_keys), GT["double_click_bug"]),
            "exact key resolvable",
        ),
    ),
    Question(
        13, "all-sprints-breadth",
        "List all the sprints with their status.",
        f"returns the COMPLETE set of {GT['sprints']} sprints (Sprint 1 through Sprint 7), not a "
        "top-K sample.",
        lambda r: (
            _has(r.answer, "sprint 1") and _has(r.answer, "sprint 7"),
            f"expected all {GT['sprints']} (Sprint 1..7)",
        ),
    ),
    Question(
        14, "multi-hop-single-issue",
        "Summarize the double-order checkout bug: what it was, how it was fixed, and its current "
        "status.",
        f"synthesizes description + comment(s) + current status for {GT['double_click_bug']} "
        "(content quality is a human read).",
        lambda r: (
            None,
            f"cites {GT['double_click_bug']}" if _has(" ".join(r.citation_keys), GT["double_click_bug"])
            else f"WARN: {GT['double_click_bug']} not in citations",
        ),
    ),
    Question(
        15, "comment-aggregation",
        "What concerns or findings did people raise in the comments about the duplicate-order "
        "problem?",
        "aggregates across MULTIPLE comment threads (e.g. ATC-43 / ATC-46 / ATC-59); human read for "
        "whether it's genuinely synthesized.",
        lambda r: (None, "manual: is it a real multi-comment synthesis?"),
    ),
    # Sprint membership — multi-hop query_sprints -> query_issues (gap-closed regression guard) -----
    Question(
        16, "sprint-membership",
        "How many issues are in the active sprint?",
        f"resolves the active sprint (Sprint 7) then counts its issues = "
        f"{GT['active_sprint_issue_count']}. Guards the sprint_id backfill: a 0/none answer means "
        "issue-sprint membership regressed to NULL again.",
        lambda r: (
            (not r.abstained) and _has(r.answer, str(GT["active_sprint_issue_count"]))
            and not _has(r.answer, "no issues", "0 issues"),
            f"expected {GT['active_sprint_issue_count']} (regression if 0/none)",
        ),
    ),
    # get_issue_comments (Plan B) — regression guard for the reopen-reason bug: search_knowledge_base
    # alone can bury the one comment with the answer, and get_issue_history's own field_change
    # description doesn't always carry it either (ATC-30's is a bare "Updated status"). This follow-up
    # can only be answered by falling back to a full, unranked comment read on the identified issues.
    Question(
        17, "issue-comments-fallback",
        "Which issues were reopened after being marked done, and who reopened them?",
        "REGRESSION GUARD for get_issue_comments (Plan B): turn 2 ('what is the reason...') must not "
        "abstain, and must state BOTH reasons — ATC-68's (from history) AND ATC-30's (which lives "
        "ONLY in a comment, not in any structured field — the case this tool exists for).",
        lambda r: ((not r.abstained), "turn 1 should identify the two reopened issues"),
        followup=(
            "then what is the reason people reopened the 2 issues?",
            lambda r: (
                (not r.abstained)
                and _has(r.answer, *GT["atc68_reopen_reason_words"])
                and _has(r.answer, *GT["atc30_reopen_reason_words"]),
                "turn 2 must NOT abstain and must state both ATC-68's and ATC-30's actual reasons "
                f"({GT['atc68_reopen_reason_words']} / {GT['atc30_reopen_reason_words']})",
            ),
        ),
    ),
]


# --- Runner -------------------------------------------------------------------------------------

TAG = {True: "[PASS]", False: "[FAIL]", None: "[CHECK]"}


def run(base_url: str, space_ids: List[int]) -> int:
    print(f"Ask-AI ingestion smoke  ->  {base_url}/ask   space_ids={space_ids}\n" + "=" * 78)
    n_pass = n_fail = n_check = n_err = 0
    for q in QUESTIONS:
        r = ask(base_url, q.text, space_ids, history=[])
        if r.error:
            print(f"[ERR ] Q{q.n:>2} {q.facet}: {r.error}")
            n_err += 1
            continue

        ok, note = q.check(r)

        # Follow-up turn (Q11): thread turn 1's final text as history, re-check on turn 2.
        if q.followup:
            fu_text, fu_check = q.followup
            history = [
                {"role": "user", "content": q.text},
                {"role": "assistant", "content": r.answer},
            ]
            r2 = ask(base_url, fu_text, space_ids, history=history)
            if r2.error:
                ok, note, r = False, f"follow-up errored: {r2.error}", r
            else:
                ok, note = fu_check(r2)
                r = r2  # report the turn that actually gets judged

        tag = TAG[ok]
        if ok is True:
            n_pass += 1
        elif ok is False:
            n_fail += 1
        else:
            n_check += 1

        print(f"\n{tag} Q{q.n:>2}  [{q.facet}]  tools={r.tool_label}  rounds={r.retrieval_rounds}")
        print(f"       Q: {q.text}")
        print(f"       PASS-if: {q.criteria}")
        if r.queries_used:
            print(f"       semantic queries: {r.queries_used}")
        if r.citation_keys:
            print(f"       citations: {r.citation_keys}")
        print(f"       note: {note}")
        ans = " ".join(r.answer.split())
        print(f"       answer: {ans[:400]}{'…' if len(ans) > 400 else ''}")

    print("\n" + "=" * 78)
    print(f"SUMMARY: {n_pass} PASS   {n_fail} FAIL   {n_check} CHECK(manual)   {n_err} ERROR")
    print("CHECK rows need a human eyeball (grounding/synthesis/false-positive). Q16 is a known-gap "
          "diagnostic, not a failure.")
    # Non-zero exit only on hard, deterministic FAILs — so this can gate CI without a human, while
    # the manual-judgment CHECK rows never break the build on their own.
    return 1 if (n_fail or n_err) else 0


def list_questions() -> None:
    print("# Ask-AI ingestion test battery — paste each into the Ask AI panel (space: AtlasCart).\n")
    for q in QUESTIONS:
        print(f"{q.n:>2}. [{q.facet}] {q.text}")
        print(f"    PASS-if: {q.criteria}")
        if q.followup:
            print(f"    then ask: {q.followup[0]}")
        print()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8200", help="ai-service base URL")
    p.add_argument("--space-ids", default="5000014", help="comma-separated space ids (default AtlasCart)")
    p.add_argument("--list", action="store_true", help="print the questions for UI copy-paste and exit")
    args = p.parse_args()
    if args.list:
        list_questions()
        return 0
    space_ids = [int(x) for x in args.space_ids.split(",") if x.strip()]
    t0 = time.time()
    code = run(args.url, space_ids)
    print(f"({time.time() - t0:.1f}s total)")
    return code


if __name__ == "__main__":
    sys.exit(main())
