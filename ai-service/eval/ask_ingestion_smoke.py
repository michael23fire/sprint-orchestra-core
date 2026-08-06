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
    "most_recent_issue": "ATC-79",      # max(updated_at) — re-verified 2026-08-03 against jira-backend
    "latest_sprint_goal_words": ["accessible", "observable", "beta"],  # Sprint 7 goal
    "active_sprint_issue_count": 8,     # Sprint 7 has 8 issues (after the sprint_id backfill)
    # ATC-68's reopen field_change carries the reason in its OWN `description` column (not a comment)
    # — the fact _format_issue_history used to silently drop for every field_change event.
    "atc68_reopen_reason_words": ["resend", "email"],
    # ATC-30's reason lives ONLY in a comment (its field_change description is a bare "Updated
    # status") — the case get_issue_comments (Plan B) exists for.
    "atc30_reopen_reason_words": ["browser", "twice"],
    # ATC-43's real subtasks (parent_key relationship, structured — see EVAL_GOLDEN_SET_TAXONOMY.md's
    # "known gap" #1: RAGAS already covers this, smoke test didn't until now). ATC-46 is a RELATED
    # issue under the same parent epic, not a subtask of ATC-43 — a wrong answer here would name it too.
    "atc43_subtasks": ["ATC-44", "ATC-45", "ATC-47"],
    # ATC-46's attached PDF (inventory-correction-requirement.pdf, Docling-parsed -> chunk_type=
    # attachment) is the ONLY place this SKU appears — not in the issue body or any comment.
    "atc46_attachment_sku": "A-104",
    # ATC-20's attached CSV (invalid-price-reproduction.csv) — a second file type (plain-text/table
    # CSV, no OCR involved), ground truth only in the reproduction table.
    "atc20_attachment_sku": "D-402",
    "atc20_attachment_row": "19",
    # ATC-27's attached XLSX (stock-concurrency-results.xlsx, Docling table-structure parse) — a
    # third file type, ground truth only in the results table.
    "atc27_attachment_lock_ms": "138",
    # ATC-13's attached Markdown (product-detail-api-contract.md) — a fourth file type. The issue body
    # describes the endpoint generically; the exact stock number and 404 error code are only in the
    # attached HTTP contract examples.
    "atc13_attachment_stock": "4",
    "atc13_attachment_404_code": "PRODUCT_NOT_FOUND",
    # ATC-59's attached .log (cart-merge-doubles-a-matching-sku-diagnostic.log) — a fifth file type
    # (plain-text log, decoded directly, no Docling/OCR involved). guest_token is a log-only field —
    # the issue body's narrative covers the user-facing bug story but not this internal token.
    "atc59_attachment_guest_token": "gt_7f2",
    # ATC-15's attached PNG (missing-image-ac1-before.png), OCR'd via EasyOCR. Deliberately testing on
    # the PRICE, not the product-name text: OCR noise on this fixture reads "Oak" as "QAK" and "Slow
    # 3G" as "SLON 36", but numbers/prices come through reliably — this is the standard practice for
    # noisy-OCR eval (grade on the robust signal, not brittle exact-text match on the noisy parts).
    # The issue body never states this price at all.
    "atc15_attachment_price": "28.00",
    # Second examples per format (a single passing case per format could be a lucky coincidence of
    # that one issue's phrasing; a second, different issue/fact closes that gap).
    "atc73_attachment_rejected": "2",
    "atc68_attachment_error_code": "order_link_reference_mismatch",
    "atc63_attachment_actor": "operations",
    "atc56_attachment_expected": "x4",
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
# 28 questions, each targeting a distinct code path / content type. Grouped by what they prove.

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
    # Attachment content — regression guard for a real gap found live: attachment parsing/embedding
    # code existed and was wired in, but zero attachment chunks were actually in the index (Kafka
    # ingestion flag off; the one-time backfill of pre-existing files silently no-opped because
    # boto3/docling weren't installed in the venv despite being pinned in requirements.txt). PASS here
    # requires the ONE fact this SKU exists only inside a parsed PDF, never in issue/comment text.
    # Phrasing note: found live that this question is genuinely sensitive to exact wording — a longer,
    # more verbose phrasing ("In the inventory correction requirement document attached to ATC-46,
    # what SKU...") reliably drove the model into a 4-round chain of natural-language semantic queries
    # that never scored the attachment chunk highly enough (hybrid/vector ranking struggles with an
    # exact alphanumeric code it doesn't know in advance), while this terser phrasing consistently
    # nudges it toward a lexical-friendly query that finds it. Retrieval itself is not flaky — a direct
    # lexical-mode /search for "A-104" finds the chunk every time — this is LLM tool-query-generation
    # variance for a needle-in-haystack exact-value fact, not a data or ranking bug.
    Question(
        18, "attachment-content",
        "ATC-46 attachment worked example: what SKU and order reference does it use?",
        f"states SKU {GT['atc46_attachment_sku']} and order reference BETA-1043 — both only exist "
        "inside the attached PDF's parsed text (chunk_type=attachment), nowhere else in the corpus.",
        lambda r: (
            (not r.abstained) and _has(r.answer, GT["atc46_attachment_sku"], "beta-1043"),
            f"expected SKU {GT['atc46_attachment_sku']} + order BETA-1043 from the attachment",
        ),
    ),
    # A second file type: CSV (plain-text/table parse, no OCR). Ground truth is in the reproduction
    # table only — the issue body names row 18 (blank price), not row 19 (negative price).
    Question(
        19, "attachment-content-csv",
        "ATC-20 attachment reproduction data: which SKU has a negative input price, and what row "
        "number is it?",
        f"states SKU {GT['atc20_attachment_sku']} and row {GT['atc20_attachment_row']} — both only "
        "exist inside the attached CSV, nowhere else in the corpus (the issue body only names row 18).",
        lambda r: (
            (not r.abstained)
            and _has(r.answer, GT["atc20_attachment_sku"])
            and _has(r.answer, GT["atc20_attachment_row"]),
            f"expected SKU {GT['atc20_attachment_sku']} + row {GT['atc20_attachment_row']}",
        ),
    ),
    # A third file type: XLSX (Docling table-structure parse). Ground truth is a specific numeric
    # result buried in a 5-row test table, not summarized anywhere in the issue's own text.
    Question(
        20, "attachment-content-xlsx",
        "ATC-27 attachment concurrency results: what was the lock-release time in ms for run 4, and "
        "what happened in that run?",
        f"states {GT['atc27_attachment_lock_ms']} ms and that the first request timed out while the "
        "second succeeded — both only exist inside the attached XLSX's results table.",
        lambda r: (
            (not r.abstained) and _has(r.answer, GT["atc27_attachment_lock_ms"]),
            f"expected lock-release time {GT['atc27_attachment_lock_ms']} ms",
        ),
    ),
    # A fourth file type: Markdown (plain-text parse, no OCR/table-structure involved).
    Question(
        21, "attachment-content-markdown",
        "ATC-13 attached API contract: what stock value is returned for the active SKU example, and "
        "what error code is returned for an unknown SKU?",
        f"states stock {GT['atc13_attachment_stock']} and error code {GT['atc13_attachment_404_code']} "
        "— both only exist inside the attached Markdown's HTTP examples, nowhere else in the corpus.",
        lambda r: (
            (not r.abstained)
            and _has(r.answer, GT["atc13_attachment_stock"])
            and _has(r.answer, GT["atc13_attachment_404_code"]),
            f"expected stock {GT['atc13_attachment_stock']} + code {GT['atc13_attachment_404_code']}",
        ),
    ),
    # A fifth file type: plain-text .log (direct UTF-8 decode, no parser library involved at all).
    Question(
        22, "attachment-content-log",
        "ATC-59 attached diagnostic log: what is the guest_token value logged?",
        f"states {GT['atc59_attachment_guest_token']} — this internal token only exists inside the "
        "attached log, not in the issue's own bug-report narrative.",
        lambda r: (
            (not r.abstained) and _has(r.answer, GT["atc59_attachment_guest_token"]),
            f"expected guest_token {GT['atc59_attachment_guest_token']}",
        ),
    ),
    # PNG screenshot, OCR'd via EasyOCR — deliberately graded on the price (a robust OCR signal), not
    # the garbled product-name text (see GT's comment on this fixture).
    Question(
        23, "attachment-content-png-ocr",
        "ATC-15 attached screenshot (before state): what price is shown for the item?",
        f"states {GT['atc15_attachment_price']} — this exact price is never stated in the issue body, "
        "only visible in the OCR'd screenshot text.",
        lambda r: (
            (not r.abstained) and _has(r.answer, GT["atc15_attachment_price"]),
            f"expected price {GT['atc15_attachment_price']}",
        ),
    ),
    # --- Second examples per attachment format — a single passing case per format could be a lucky
    # coincidence of that one issue's phrasing; these use a different issue/fact per format. ---
    Question(
        24, "attachment-content-csv-2",
        "ATC-73 attached background-import-run.csv: how many rows were rejected during the 'failed' "
        "phase?",
        f"states {GT['atc73_attachment_rejected']} — only in the CSV's phase-by-phase table, not the "
        "issue body.",
        lambda r: (
            (not r.abstained) and _has(r.answer, GT["atc73_attachment_rejected"]),
            f"expected {GT['atc73_attachment_rejected']} rejected",
        ),
    ),
    Question(
        25, "attachment-content-log-2",
        "ATC-68 attached diagnostic log: what error code is logged?",
        f"states {GT['atc68_attachment_error_code']} — the issue body tells the BETA-1128/BETA-1123 "
        "story but never states this internal error code, only the log does.",
        lambda r: (
            (not r.abstained) and _has(r.answer, GT["atc68_attachment_error_code"]),
            f"expected error code {GT['atc68_attachment_error_code']}",
        ),
    ),
    Question(
        26, "attachment-content-pdf-2",
        "ATC-63 attached beta order state model: who is the actor for the transition from "
        "'cancelled' to 'refund pending'?",
        f"states {GT['atc63_attachment_actor']} — only in the PDF's transition table, not the issue "
        "body (which only asks that transitions name an actor, without naming one itself).",
        lambda r: (
            (not r.abstained) and _has(r.answer, GT["atc63_attachment_actor"]),
            f"expected actor {GT['atc63_attachment_actor']}",
        ),
    ),
    Question(
        27, "attachment-content-xlsx-2",
        "ATC-56 attached guest-cart-merge-matrix: what is the expected result for the 'Stock limit' "
        "test case?",
        f"states {GT['atc56_attachment_expected']} (quantity capped at available stock, with an "
        "explanatory note) — only in the matrix, not the issue body.",
        lambda r: (
            (not r.abstained) and _has(r.answer, GT["atc56_attachment_expected"]),
            f"expected result containing {GT['atc56_attachment_expected']}",
        ),
    ),
    # Epic/subtask relationship (parent_key) — RAGAS already covers this (subtask list + epic-of x2),
    # smoke test didn't until now (see EVAL_GOLDEN_SET_TAXONOMY.md's "known gap" #1). ATC-46 is a
    # related issue under the same parent epic (ATC-4), NOT a subtask of ATC-43 — naming it here would
    # be a false positive, the same class of mistake Case Study 17/18 already found and fixed.
    Question(
        28, "structured-relationship-subtasks",
        "Which issues are the real subtasks of ATC-43?",
        f"names exactly {GT['atc43_subtasks']} — and does NOT include ATC-46, which is a related "
        "issue under the same parent epic, not a subtask of ATC-43.",
        lambda r: (
            (not r.abstained)
            and _has_all(r.answer, GT["atc43_subtasks"])
            and not _has(r.answer, "ATC-46 is a subtask", "ATC-46, a subtask"),
            f"expected exactly {GT['atc43_subtasks']}, and ATC-46 not asserted as a subtask",
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
