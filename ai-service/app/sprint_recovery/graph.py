"""Sprint recovery: diagnose -> gather evidence -> hypothesize (grounded) -> confidence-gated
clarification -> propose recovery plans -> durable human approval -> idempotent execution ->
wait-and-reevaluate -> escalate or close.

**Honest framing, stated up front, the same way `app/planning/graph.py` and
`app/planning/rollout_graph.py` both do.** The control flow here — a handful of nodes, a couple of
bounded conditional loops — does not *require* LangGraph; a `status` column plus explicit resume
functions would work. What's different from the rollout workflow (which also doesn't require it) is
where the honest case for using it anyway gets *stronger*, not where it becomes strictly necessary:

1. **Multiple, differently-shaped pauses**, not one. The rollout workflow has exactly one interrupt
   shape (approve/edit/reject a finished plan). This graph has at least three: `clarify` (answer one
   specific question mid-diagnosis), `approve_plan` (approve/edit/reject a finished plan, same shape as
   before), and — after execution — a wait that is resumed by *either* a real Kafka event or a human,
   not only a human. A hand-rolled version would need a distinct status value and a bespoke resume
   handler per shape; LangGraph's `interrupt()` gives each pause the same protocol regardless of shape.
2. **Re-entry driven by an external event, not only a human click.** `waiting_reevaluation` is resumed
   by a real Kafka consumer (`app/sprint_recovery/kafka_trigger.py`) reacting to
   `IssueContentChangedEvent` on the *same* topic `vectorization-service` already consumes — this
   graph does not invent new event infrastructure, it adds a second consumer group on an existing one.
3. **A genuinely conditional replan loop**, bounded by `max_escalation_rounds` — not just "retry the
   same thing," but "generate new plans given what happened last time," which is the shape
   `crag_loop.py`'s docstring explicitly says a framework earns its keep on once there are *several*
   distinct decision points, not one.

None of this is "impossible without a framework" — it's "the hand-rolled version's status-string
branching gets meaningfully more error-prone here than it was for the rollout workflow," which is a
cost judgment, not a capability claim.

**Grounding**: mirrors `crag_loop.py`'s `_ground_answer` — every `RootCauseHypothesis.
supporting_evidence_ids` must reference an `EvidenceItem.citation_id` actually gathered this run.
Unlike `_ground_answer`'s hard reject-the-whole-answer behavior, `_apply_grounding` here drops only the
ungrounded hypothesis (partial evidence for other hypotheses is still useful), logging what was cut.

**Runaway-loop protection**: `max_clarification_rounds` bounds the diagnose<->clarify cycle,
`max_escalation_rounds` bounds the commit<->replan cycle, `max_token_budget` is a hard cost ceiling
checked before every LLM call — matches the existing `MAX_REVISION_ROUNDS`/`max_tool_iterations`
pattern already established in `app/planning/graph.py` / `app/agent/crag_loop.py`, applied here to two
independent loops instead of one.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from contextvars import ContextVar
from datetime import date, datetime, timezone
from typing import Callable, Dict, List, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace

# One span per graph *node* execution, nested under whichever parent span the calling route already
# opened (app/api/sprint_recovery_routes.py mirrors app/api/routes.py's `/ask` pattern: one parent
# span per HTTP call, everything inside it nests automatically via OpenTelemetry context propagation).
# Without this, Phoenix would show a flat list of disconnected LLM-call spans with no way to tell
# "these three calls were the diagnose step, this one was plan" — the exact debugging question a
# multi-step agent gets asked about. `thread_id` is tagged on every span specifically because this
# workflow spans multiple, separate HTTP calls (sometimes days apart) — no single Phoenix trace covers
# the whole thread's lifetime, so the thread_id attribute is what makes its spans findable/correlatable
# across traces, not the trace tree itself.
_tracer = trace.get_tracer(__name__)

# Optional SSE progress-label plumbing — same `contextvars.ContextVar` mechanism `crag_loop.py` uses
# for its per-request `stage_seconds` (needed for the identical reason: `build_sprint_recovery_graph`
# builds one shared, long-lived compiled graph at startup, so per-*request* state like "which HTTP call
# wants stage events" can't be a constructor argument — it has to travel via context, scoped to
# whichever request's call stack is currently executing a node). Mirrors `POST /ask/stream`'s `on_stage`
# pattern (app/api/routes.py) exactly: `asyncio.create_task` snapshots the current contextvars.Context
# at creation time, so the streaming endpoint sets this *before* creating the `graph.ainvoke` task, and
# every node's span wrapper below picks it up automatically without any change to `diagnose_node`/
# `plan_node`'s own signatures. `None` (the default) is a real no-op, not just "nothing subscribed yet"
# — every non-streaming caller (tests, the plain JSON endpoints) leaves this unset.
ON_STAGE_VAR: ContextVar[Optional[Callable[[str], None]]] = ContextVar("sprint_recovery_on_stage", default=None)

# Human-readable labels for the two nodes that actually make an LLM call — the only ones worth
# surfacing as a stage event. `approval`/`clarify`/`wait_for_reevaluation` are pauses (nothing to wait
# on), and `commit_one_action` is a fast Jira REST call, not an LLM call.
_STAGE_LABELS = {
    "diagnose": "detecting risk signals and analyzing evidence",
    "plan": "generating recovery plans",
}

# Plain-English names for `RiskSignal.signal_type`. These reach real users — a risk signal is restated
# as citable evidence in `diagnose_node`, and that text is rendered verbatim in the UI's evidence
# panel and citation tooltips. Found live: the raw enum leaked through as
# "low_completion_forecast: 0/3 issues done" in a user-facing tooltip, which reads as debug output.
_SIGNAL_LABELS = {
    "blocked_no_flag": "Marked as blocked",
    "long_in_progress": "Stuck in progress",
    "owner_overloaded": "Assignee overloaded",
    "no_dependency_recorded": "Undocumented dependency",
    "scope_added_late": "Scope added late in the sprint",
    "low_completion_forecast": "Behind on completion",
}

# Same pattern `crag_loop.py`'s `_ground_answer` already uses to recognize an issue key mentioned in
# free text (e.g. a comment saying "waiting on PAY-97" without PAY-97 itself being independently
# flagged as at-risk) — reused rather than reinvented, and for the same reason: a hypothesis about an
# *undocumented* dependency inherently needs to reference an issue that was never flagged on its own.
_ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b", re.IGNORECASE)

from app.auth.space_membership import SpaceMembershipChecker, SpaceMembershipError
from app.llm.pricing import estimate_cost_usd
from app.llm.types import Usage
from app.sprint_recovery.jira_actions_client import JiraActionError, JiraActionsClient
from app.sprint_recovery.schemas import (
    DiagnosisResult,
    EvidenceItem,
    RecoveryAction,
    RecoveryPlan,
    RecoveryPlanSet,
    RiskSignal,
    RootCauseHypothesis,
    SprintIssueSummary,
    SprintSnapshot,
)
from app.sprint_recovery.state import SprintRecoveryState

logger = logging.getLogger(__name__)

# A single revision pass on the clarify loop and a single replan pass on escalation are the
# structural minimum to demonstrate the loop actually works — the *count* itself is a caller-supplied
# ceiling (SprintRecoveryState.max_clarification_rounds / max_escalation_rounds), not hardcoded here.
_TOKENS_PER_LLM_CALL_ESTIMATE = 4000  # coarse, pre-call budget check — see _check_budget's docstring

# `(usage, cost_usd) -> None`, wired by app.main to `ServiceStats.record` so this workflow's spend
# lands in the same GET /stats and `agent_cost_usd_total` Prometheus counter as every other LLM call
# in the service. Passed explicitly down from `build_sprint_recovery_graph` rather than through a
# ContextVar like `ON_STAGE_VAR`: that one is genuinely per-request (which SSE queue is listening),
# whereas this sink is process-wide and known at startup, so a plain argument is both simpler and
# testable without context plumbing.
UsageRecorder = Callable[[Usage, float], None]


def _check_budget(state: SprintRecoveryState) -> bool:
    """True if there's room for one more LLM call. Coarse and pre-emptive on purpose: the real
    per-call token usage is only known *after* the call, but the whole point of a budget ceiling is to
    stop *before* spending more, not to notice afterward that too much was already spent. The *recorded*
    usage is the real thing (see `_call_model`); only this forward-looking guard is an estimate.
    """
    return state["token_usage"] + _TOKENS_PER_LLM_CALL_ESTIMATE <= state["max_token_budget"]


def _usage_from_completion(completion) -> Usage:
    """Normalizes whatever the provider returned alongside a structured response into `Usage`.

    Anthropic reports `input_tokens`/`output_tokens` (plus cache counters); OpenAI-compatible servers
    report `prompt_tokens`/`completion_tokens`, with cached input under
    `prompt_tokens_details.cached_tokens`. Both shapes are read here rather than in each caller so the
    two `create_with_completion` sites below stay about diagnosis and planning, not billing wire
    formats. Anything missing counts as 0: a local server that reports no usage at all (a bare
    llama.cpp) must degrade to "free and unmeasured", never to an exception — same principle
    `app/llm/types.py`'s `Usage` already documents.
    """
    raw = getattr(completion, "usage", None)
    if raw is None:
        return Usage()

    def _int(*names: str) -> int:
        for name in names:
            value = getattr(raw, name, None)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    cached = 0
    details = getattr(raw, "prompt_tokens_details", None)
    if details is not None and isinstance(getattr(details, "cached_tokens", None), (int, float)):
        cached = int(details.cached_tokens)
    input_tokens = _int("input_tokens", "prompt_tokens")
    return Usage(
        # OpenAI's `prompt_tokens` is inclusive of cached tokens; Anthropic's `input_tokens` excludes
        # them (they're reported separately). Subtracting here keeps the two providers priced the same
        # way — cached input is billed at a tenth of fresh input, so folding them together would
        # overstate cost by ~10x on the cached portion.
        input_tokens=max(0, input_tokens - cached) if cached else input_tokens,
        output_tokens=_int("output_tokens", "completion_tokens"),
        cache_creation_input_tokens=_int("cache_creation_input_tokens"),
        cache_read_input_tokens=_int("cache_read_input_tokens") or cached,
    )


async def _call_model(
    client, model: str, response_model, prompt: str, on_usage: Optional[UsageRecorder] = None,
):
    """One structured LLM call, returning `(parsed, tokens, cost_usd)` with the *real* token counts.

    **Found live, from a direct question about what this workflow costs**: every node used to advance
    `token_usage` by a flat `_TOKENS_PER_LLM_CALL_ESTIMATE`, so the number the API reported was a
    guess repeated N times, not a measurement — unanswerable if anyone asked what a run actually cost.
    `create_with_completion` returns the raw provider response alongside the parsed model, which is
    what makes the true counts available without changing how any caller consumes the result.

    Known bound, stated rather than hidden: `max_retries=2` means `instructor` may make more than one
    provider call to get a valid parse, and only the final response's usage is returned here — a
    reparse undercounts. That is a bounded, always-in-the-same-direction error on an observability
    number, which is preferable to threading a per-attempt hook through every call site.
    """
    parsed, completion = await client.chat.completions.create_with_completion(
        model=model, response_model=response_model, max_retries=2,
        messages=[{"role": "user", "content": prompt}],
    )
    usage = _usage_from_completion(completion)
    cost = estimate_cost_usd(
        model, usage.input_tokens, usage.output_tokens,
        usage.cache_creation_input_tokens, usage.cache_read_input_tokens,
    )
    if on_usage is not None:
        try:
            on_usage(usage, cost)
        except Exception:  # noqa: BLE001 - cost bookkeeping must never break a real workflow
            logger.exception("failed recording sprint-recovery LLM usage")
    tokens = usage.input_tokens + usage.output_tokens + usage.cache_read_input_tokens
    # A provider that reports nothing at all would otherwise make the budget ceiling unenforceable
    # (0 tokens forever), so fall back to the same estimate the pre-call guard uses.
    return parsed, tokens or _TOKENS_PER_LLM_CALL_ESTIMATE, cost


async def _gather_evidence(
    retrieval, space_id: int, sprint_id: int, flagged_issue_keys: List[str],
) -> List[EvidenceItem]:
    """Deterministic, unranked fetch — same "get_issue_comments/details/attachments as a fallback to
    top-K search" reasoning `crag_loop.py`'s tool surface already uses, applied here as the *only*
    retrieval path (there's no model in the loop yet at this point to decide whether to search more).
    """
    evidence: List[EvidenceItem] = []
    n = 0
    if not flagged_issue_keys:
        return evidence
    comments = await retrieval.get_issue_comments([space_id], flagged_issue_keys)
    for c in comments.comments:
        n += 1
        evidence.append(EvidenceItem(
            citation_id=f"ev{n}", issue_key=c["issue_key"], source_type="comment", content=c["content"],
        ))
    details = await retrieval.get_issue_details([space_id], flagged_issue_keys)
    for d in details.details:
        n += 1
        evidence.append(EvidenceItem(
            citation_id=f"ev{n}", issue_key=d["issue_key"], source_type="description", content=d["content"],
        ))
    attachments = await retrieval.get_issue_attachments([space_id], flagged_issue_keys)
    for a in attachments.attachments:
        n += 1
        evidence.append(EvidenceItem(
            citation_id=f"ev{n}", issue_key=a["issue_key"], source_type="attachment", content=a["content"],
        ))
    history = await retrieval.query_issue_history([space_id], {"issue_keys": flagged_issue_keys, "limit": 50})
    for h in history.changes:
        # **Found live, not hypothetically**: vectorization-service's /issues/history returns more than
        # field-change rows — `issue_created`/`comment_created`/`code_link_added`/etc all come back with
        # `field_name`/`from_value`/`to_value` all null and the actual human-readable content in
        # `description` instead (e.g. "linked pull request 119"). The original unconditional
        # `f"{field_name}: {from_value} -> {to_value}"` template rendered those as literal, useless
        # `"None: None -> None"` evidence — real signal silently destroyed by a formatting bug, not
        # dropped on purpose. Also drops `issueOrder` changes outright: drag-and-drop backlog
        # reordering, never diagnostically relevant, pure noise that was diluting the evidence count.
        field_name = h.get("field_name")
        if field_name == "issueOrder":
            continue
        if field_name:
            content = f"{field_name}: {h['from_value']} -> {h['to_value']} ({h['changed_at']})"
        elif h.get("description"):
            content = f"{h.get('event_type', 'change')}: {h['description']} ({h['changed_at']})"
        else:
            continue  # neither a field change nor a description — nothing to say, don't manufacture evidence
        n += 1
        evidence.append(EvidenceItem(
            citation_id=f"ev{n}", issue_key=h["issue_key"], source_type="history", content=content,
        ))
    return evidence


_STALE_AFTER_DAYS = 2  # in_progress/blocked with no update for longer than this — see docstring below
# `owner_overloaded` thresholds. Both are product judgements, not technical facts — teams differ, and
# both are named constants specifically so that's obvious and tunable.
#
# Points-based (used whenever the sprint has any estimated issue): one person carrying this share of
# the sprint's still-open story points is overloaded, regardless of how many tickets that is — a
# single 13-point ticket can be a worse bottleneck than five 1-point ones. **Found live, from direct
# product feedback**: a pure issue-count threshold treats a 1-point ticket and an 8-point ticket as
# identical load, which doesn't match how sprints are actually sized and committed.
_OVERLOADED_POINTS_SHARE = 0.4
# Guards the points rule from firing on a single big ticket that just happens to be the only unfinished
# work left for anyone — "overloaded" implies competing demands, not "the last person standing."
_OVERLOADED_MIN_ISSUES = 2
# Fallback when nothing in the sprint is estimated (a legitimate state — not every team points every
# ticket), so the signal degrades to something computable rather than going silent.
_OVERLOADED_OPEN_ISSUES = 3
# How far into a sprint's elapsed time a "sprint" field-change to THIS sprint counts as scope added
# "late" rather than normal pre-sprint/early-sprint grooming. 0.5 = added after the sprint's own
# midpoint. A product judgement, not a technical fact, same as the constants above.
_SCOPE_ADDED_LATE_ELAPSED_FRAC = 0.5


def _days_since(iso_ts: Optional[str]) -> Optional[float]:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400


def _parse_iso_date(iso_ts: Optional[str]) -> Optional[date]:
    if not iso_ts:
        return None
    try:
        return datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00")).date()
    except ValueError:
        return None


async def _sprint_context(retrieval, space_id: int, sprint_id: int) -> dict:
    """Best-effort lookup of one sprint's own record: start/end dates for the timeline-aware
    completion checks, and the sprint GOAL. `query_sprints` has no `sprint_ids` filter (it wasn't
    needed anywhere else), so this fetches the space's sprints and picks the matching one; returns
    empty values (never raises) on anything unexpected — every consumer below degrades to a
    date/goal-independent rule, this must not be able to break diagnosis outright.

    **The goal was being thrown away entirely.** `RecoveryPlan` has always had an `impact_on_goal`
    field, and the plan prompt has always asked the model to fill it in — while never once showing it
    what the goal actually says. So "impact on goal" was the model inferring a plausible-sounding goal
    from ticket titles. A sprint goal is the single most important piece of context a PM applies when
    judging sprint risk ("will we hit the commitment?" is a different question from "will every ticket
    close?"), and it was sitting unused in the sprints table the whole time.
    """
    try:
        result = await retrieval.query_sprints([space_id], {"limit": 200})
        for sprint in result.sprints:
            if sprint.get("sprint_id") == sprint_id:
                # Over the wire these are JSON strings (e.g. "2026-08-14"), not `date` objects — the
                # real RetrievalClient hands back raw `resp.json()` dicts with no date deserialization.
                return {
                    "start_date": date.fromisoformat(str(sprint["start_date"])[:10]) if sprint.get("start_date") else None,
                    "end_date": date.fromisoformat(str(sprint["end_date"])[:10]) if sprint.get("end_date") else None,
                    "goal": sprint.get("goal"),
                    "sprint_name": sprint.get("sprint_name"),
                }
    except Exception:  # noqa: BLE001 - supplementary context, not load-bearing
        return {}
    return {}


# How long `_await_index_catch_up` will keep waiting for the read model before giving up and reading
# anyway. A ceiling on a condition that is normally satisfied on the first poll — not a guessed delay.
_INDEX_CATCH_UP_TIMEOUT_SECONDS = 10.0
_INDEX_CATCH_UP_POLL_SECONDS = 0.25


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def _await_index_catch_up(retrieval, space_id: int, sprint_id: int, watermarks: Dict[str, str]) -> bool:
    """Blocks until vectorization-service's index reflects the writes this workflow just made, or the
    timeout expires. Returns True if it actually caught up.

    **Why this exists.** jira-backend writes synchronously, publishes to Kafka, and
    vectorization-service reindexes asynchronously on its own consumer group — so *every* path that
    resumes this workflow can observe a stale sprint. That includes the real Kafka trigger:
    `kafka_trigger.py` is a **separate consumer group** from vectorization-service's, so both receive
    the same event at the same time and the trigger can easily win the race to read an index that
    hasn't been rebuilt yet. An earlier version of this fix slept a flat 2 seconds in one HTTP route,
    which (a) only covered one of the three trigger paths, and (b) was a guess that a slow broker or a
    consumer-group rebalance would beat anyway.

    **The actual signal.** `commit_one_action_node` records, per issue it wrote to, that issue's
    `updatedAt` read back from jira-backend — the write-side source of truth. This polls the same
    `query_issues` call `_detect_risk_signals` uses and waits until every one of those issues either
    reports an indexed `updated_at` at least as new as the recorded watermark, or has left this sprint
    entirely (which is exactly what a committed `move_out_of_sprint` looks like once it lands, and is
    equally proof the write propagated). No watermarks (nothing was committed) means nothing to wait for.

    **The honest bound.** On timeout this returns False and the caller reads anyway rather than
    stalling the workflow — a stale read is still possible, but it now requires the index to be more
    than `_INDEX_CATCH_UP_TIMEOUT_SECONDS` behind, instead of merely being slower than a fixed guess.
    """
    if not watermarks:
        return True
    deadline = time.monotonic() + _INDEX_CATCH_UP_TIMEOUT_SECONDS
    while True:
        try:
            result = await retrieval.query_issues([space_id], {"sprint_ids": [sprint_id], "limit": 200})
            indexed = {row["issue_key"]: row.get("updated_at") for row in result.issues}
            caught_up = all(
                key not in indexed or _is_at_least(_parse_ts(indexed.get(key)), _parse_ts(expected))
                for key, expected in watermarks.items()
            )
        except Exception:  # noqa: BLE001 - a single flaky poll must not abort a wait that has up to
            # `_INDEX_CATCH_UP_TIMEOUT_SECONDS` of budget left to try again; if vectorization-service is
            # genuinely unreachable rather than just having dropped one request, every subsequent poll
            # fails the same way and the timeout below still bounds how long this waits before giving up
            # — the very next `_detect_risk_signals` call is what actually surfaces a sustained outage.
            logger.warning(
                "sprint-recovery index catch-up poll failed, retrying within the timeout budget",
                exc_info=True, extra={"space_id": space_id, "sprint_id": sprint_id},
            )
            caught_up = False
        if caught_up:
            return True
        if time.monotonic() >= deadline:
            logger.warning(
                "sprint-recovery read model did not catch up before the timeout; reading anyway",
                extra={"space_id": space_id, "sprint_id": sprint_id, "watermarks": watermarks},
            )
            return False
        await asyncio.sleep(_INDEX_CATCH_UP_POLL_SECONDS)


def _is_at_least(indexed: Optional[datetime], expected: Optional[datetime]) -> bool:
    """An unparseable/missing timestamp on either side can't prove staleness — treat it as satisfied
    rather than blocking the workflow on a comparison that will never succeed."""
    if indexed is None or expected is None:
        return True
    return indexed >= expected


def _last_update_not_by_this_workflow(issue: dict, own_writes: Optional[Dict[str, tuple]]) -> Optional[str]:
    """The issue's `updated_at`, except when the most recent update was *this workflow's own* — in
    which case the honest answer to "when did somebody last move this along" is the timestamp from
    before we touched it.

    **Found live, and it produced a false `recovered`**: jira-backend stamps `updatedAt = now()` on
    every issue write (`@PreUpdate` on the `Issue` entity), so committing a `change_priority` on a
    ticket that had been untouched for six days made it look updated-just-now. `long_in_progress` and
    `blocked_no_flag` both gate on `_days_since(updated_at)`, so both signals silently vanished, and a
    sprint where two tickets were still sitting in `in_progress` with zero real progress reported
    `status="recovered"` on the very next reevaluation. The workflow was declaring victory by touching
    the ticket. (Reproduced deterministically: 86%-complete sprint, two 6-day-stale in_progress
    tickets, priority bump only → `recovered`, zero signals.)

    `own_writes` maps issue_key -> (before, after) `updatedAt` values recorded by
    `commit_one_action_node`. If the indexed timestamp is newer than our own write, a *human* has
    since touched the issue and that is genuinely the freshest signal — use it. Otherwise the last
    writer was us, so fall back to the pre-write baseline.
    """
    indexed = issue.get("updated_at")
    if not own_writes:
        return indexed
    entry = own_writes.get(issue.get("issue_key"))
    if not entry:
        return indexed
    before, after = entry
    if not before:
        return indexed
    indexed_ts, after_ts = _parse_ts(indexed), _parse_ts(after)
    if indexed_ts is not None and after_ts is not None and indexed_ts > after_ts:
        return indexed  # somebody real updated it after we did
    return before


async def _detect_risk_signals(
    retrieval, space_id: int, sprint_id: int, own_writes: Optional[Dict[str, tuple]] = None,
) -> List[RiskSignal]:
    """Deterministic — code computes this, never the LLM (same "code computes, LLM explains" split
    `app/sprint_health/service.py` and `app/planning/graph.py`'s critic already use). Chosen because
    all are directly computable from `query_issues`/`query_sprints`'s existing fields without
    inventing data this project doesn't have (e.g. no assignee-workload signal — `IssueRowOut` carries
    no assignee field, so that signal type stays in the schema as an LLM-inferable-from-evidence
    category rather than a fabricated deterministic one).

    **Two refinements found live, from direct product feedback, not hypothetically:**

    1. `long_in_progress`/`blocked_no_flag` used to fire on ANY issue in that status, with no regard
       for how long it had sat there or whether anyone had touched it recently — a ticket that moved to
       in_progress five minutes ago looked exactly as risky as one untouched for two weeks. This is
       also the deterministic answer to "can the system tell if an owner just isn't updating their
       ticket": yes — `_days_since(updated_at)` is exactly that check, now gating both signals.
    2. `low_completion_forecast` used a single flat 70% cutoff regardless of where the sprint actually
       stood in its own timeline — 80% done reads very differently on day 2 of a 10-day sprint (way
       ahead) than with 1 day left (likely to miss). Rather than model full velocity/burndown math
       (a much bigger, separately-justified feature), this adds two explicit, human-auditable rules
       alongside the flat one: inside the sprint's final 2 days, the bar rises to 90%; inside its first
       20% of elapsed time, the flat check is suppressed entirely. That second half **matters for when
       this tool actually gets run**: a sprint's own completion forecast starts at 0% by definition on
       day 1 — without this, running a health check on day 1 would flag "low completion" every single
       time, which is not a finding, it's the sprint just having started. In practice this is a
       mid-sprint-or-later check, and this rule is what makes that assumption safe to run early too,
       instead of quietly relying on nobody clicking the button too soon.

    A fourth signal, `scope_added_late`, is genuinely the most consequential in real agile practice —
    scope added mid-sprint is the classic way a sprint quietly stops being the thing the team
    committed to. It was left un-implemented until now for the same reason the timeline thresholds
    above are named constants: "how late is late" is a product judgement, not a technical fact. Having
    made that call (`_SCOPE_ADDED_LATE_ELAPSED_FRAC`), it's implemented below the same way as the
    other three: computed from real history, never asserted by the model.
    """
    result = await retrieval.query_issues([space_id], {"sprint_ids": [sprint_id], "limit": 200})
    sprint_ctx = await _sprint_context(retrieval, space_id, sprint_id)
    signals: List[RiskSignal] = []
    # issue_key -> owner name, so `_gather_evidence` and the prompts downstream can name a human
    # instead of asking one to identify themselves. Empty when a ticket is genuinely unassigned —
    # which is itself worth saying out loud rather than silently omitting.
    unfinished_by_owner: Dict[str, List[str]] = {}
    unfinished_points_by_owner: Dict[str, int] = {}
    total_unfinished_points = 0

    for issue in result.issues:
        status = (issue.get("status") or "").lower()
        owner = issue.get("assignee_name")
        owner_note = f" Owner: {owner}." if owner else " No one is assigned to it."
        if status not in ("done", "completed"):
            points = issue.get("story_points") or 0
            total_unfinished_points += points
            if owner:
                unfinished_by_owner.setdefault(owner, []).append(issue["issue_key"])
                unfinished_points_by_owner[owner] = unfinished_points_by_owner.get(owner, 0) + points

        days_stale = _days_since(_last_update_not_by_this_workflow(issue, own_writes))
        if days_stale is None or days_stale < _STALE_AFTER_DAYS:
            continue  # too fresh to call a problem yet, regardless of status
        if status in ("in_progress", "in progress"):
            signals.append(RiskSignal(
                issue_key=issue["issue_key"], signal_type="long_in_progress",
                description=(
                    f"{issue['issue_key']} has been in_progress for {days_stale:.0f} day(s) with no "
                    f"update — the owner may not be reporting status.{owner_note}"
                ),
            ))
        elif status == "blocked":
            # **Found live**: `blocked` is a first-class status in the board's own type system
            # (sprint-orchestra-studio/src/types/ticket.ts), and `RiskSignal.signal_type` already
            # named `blocked_no_flag` in its schema — but nothing ever set it, so a genuinely blocked
            # issue tripped no per-issue signal at all.
            signals.append(RiskSignal(
                issue_key=issue["issue_key"], signal_type="blocked_no_flag",
                description=(
                    f"{issue['issue_key']} has been blocked for {days_stale:.0f} day(s) with no "
                    f"update.{owner_note}"
                ),
            ))

    # `owner_overloaded` has been declared in `RiskSignal.signal_type` since the schema was written
    # but was impossible to compute — `IssueRowOut` carried no assignee field until migrations/012.
    # One person holding most of a sprint's open work is a genuine, common delivery risk (and the one
    # a standup would catch first), so with the data finally available it's worth detecting: the
    # actionable form is naming who, not just noting that the sprint is behind.
    use_points = total_unfinished_points > 0
    for owner, keys in sorted(unfinished_by_owner.items()):
        overloaded = False
        detail = ""
        if use_points:
            share = unfinished_points_by_owner[owner] / total_unfinished_points
            if share >= _OVERLOADED_POINTS_SHARE and len(keys) >= _OVERLOADED_MIN_ISSUES:
                overloaded = True
                detail = (
                    f"{owner} is carrying {unfinished_points_by_owner[owner]}/{total_unfinished_points} "
                    f"story points ({share:.0%}) of this sprint's unfinished work, across "
                    f"{len(keys)} issues ({', '.join(sorted(keys))}) — more than one person can "
                    "realistically land."
                )
        elif len(keys) >= _OVERLOADED_OPEN_ISSUES:
            overloaded = True
            detail = (
                f"{owner} is carrying {len(keys)} unfinished issues in this sprint "
                f"({', '.join(sorted(keys))}, no story points estimated) — more than one person can "
                "realistically land."
            )
        if overloaded:
            signals.append(RiskSignal(
                # Attributed to the owner's first ticket so plan_node has a concrete, actionable
                # target — a signal with no issue_key can't be acted on (see plan_node's handoff branch).
                issue_key=sorted(keys)[0], signal_type="owner_overloaded", description=detail,
            ))

    start_date, end_date = sprint_ctx.get("start_date"), sprint_ctx.get("end_date")

    total = result.total_count
    done = result.counts_by_status.get("done", 0) + result.counts_by_status.get("completed", 0)
    if total > 0:
        # Points, not just issue count. Real sprints are committed and tracked in story points; by
        # count alone, 8 trivial tickets done out of 10 reads as "80% complete" even when the two
        # left carry most of the sprint's weight. Falls back to count when nothing is estimated,
        # which is a legitimate state (`sprints.unestimated_issue_count` tracks it at sprint level).
        total_points = sum(i.get("story_points") or 0 for i in result.issues)
        done_points = sum(
            i.get("story_points") or 0 for i in result.issues
            if (i.get("status") or "").lower() in ("done", "completed")
        )
        if total_points > 0:
            forecast = round(100 * done_points / total_points, 1)
            basis = f"{done_points}/{total_points} story points done ({forecast}%)"
        else:
            forecast = round(100 * done / total, 1)
            basis = f"{done}/{total} issues done ({forecast}%, no story points estimated)"

        days_left = (end_date - date.today()).days if end_date else None
        duration_days = (end_date - start_date).days if start_date and end_date else None
        elapsed_frac = (
            max(0.0, min(1.0, (date.today() - start_date).days / duration_days))
            if duration_days else None
        )
        too_early = elapsed_frac is not None and elapsed_frac < 0.2
        near_deadline_and_short = days_left is not None and days_left <= 2 and forecast < 90
        if not too_early and (forecast < 70 or near_deadline_and_short):
            deadline_note = f" — only {max(days_left, 0)} day(s) left in the sprint" if near_deadline_and_short else ""
            signals.append(RiskSignal(
                signal_type="low_completion_forecast",
                description=f"{basis}{deadline_note} — below the checkpoint for this stage of the sprint.",
            ))

    # scope_added_late: an issue whose "sprint" field changed TO this sprint after the sprint was
    # already well underway. Real example this catches: a sprint starts with 10 committed tickets: on
    # day 7 of a 10-day sprint someone drags in 2 "quick" extra tickets during grooming. Every other
    # signal here reads that as the team's own fault (falling completion %, an overloaded assignee) —
    # none of them can say the actual cause was scope moving, not the team executing worse. This is
    # the one deterministic fact that names that specific cause. See this function's own docstring for
    # why "how late is late" (`_SCOPE_ADDED_LATE_ELAPSED_FRAC`) is a named, tunable judgement call.
    sprint_name = sprint_ctx.get("sprint_name")
    duration_days = (end_date - start_date).days if start_date and end_date else None
    if duration_days and sprint_name and result.issues:
        try:
            history = await retrieval.query_issue_history(
                [space_id], {"issue_keys": [i["issue_key"] for i in result.issues], "fields": ["sprint"], "limit": 200},
            )
            latest_add_by_issue: Dict[str, date] = {}
            for change in history.changes:
                if change.get("to_value") != sprint_name:
                    continue
                changed_at = _parse_iso_date(change.get("changed_at"))
                if changed_at is None:
                    continue
                issue_key = change.get("issue_key")
                if issue_key and (issue_key not in latest_add_by_issue or changed_at > latest_add_by_issue[issue_key]):
                    latest_add_by_issue[issue_key] = changed_at
            for issue_key, added_on in sorted(latest_add_by_issue.items()):
                elapsed_at_add = max(0.0, min(1.0, (added_on - start_date).days / duration_days))
                if elapsed_at_add >= _SCOPE_ADDED_LATE_ELAPSED_FRAC:
                    signals.append(RiskSignal(
                        issue_key=issue_key, signal_type="scope_added_late",
                        description=(
                            f"{issue_key} was added to {sprint_name} on {added_on.isoformat()}, "
                            f"{elapsed_at_add:.0%} of the way through the sprint — scope added this "
                            "late is a common reason a sprint misses its goal independent of how the "
                            "originally committed work is going."
                        ),
                    ))
        except Exception:  # noqa: BLE001 - supplementary signal, must not break diagnosis outright
            pass

    return signals


def _apply_grounding(hypotheses: List[RootCauseHypothesis], evidence: List[EvidenceItem]) -> List[RootCauseHypothesis]:
    """Drops any hypothesis citing an evidence id that was never actually gathered — mirrors
    `crag_loop.py`'s `_ground_answer` hard-reject rule, scoped per-hypothesis instead of per-answer so
    one ungrounded claim doesn't discard otherwise-valid ones.
    """
    valid_ids = {e.citation_id for e in evidence}
    kept = []
    for h in hypotheses:
        if not h.supporting_evidence_ids:
            logger.warning("dropping hypothesis with no supporting_evidence_ids", extra={"statement": h.statement})
            continue
        if not set(h.supporting_evidence_ids).issubset(valid_ids):
            logger.warning(
                "dropping ungrounded hypothesis citing unknown evidence ids",
                extra={"statement": h.statement, "cited": h.supporting_evidence_ids},
            )
            continue
        kept.append(h)
    return kept


async def _build_sprint_snapshot(retrieval, space_id: int, sprint_id: int) -> SprintSnapshot:
    """The sprint's whole shape — goal, timeline position, and every issue in it.

    **Found live**: `_detect_risk_signals` already fetches exactly this data, then returns only the
    signals it derived and discards the rest. That single omission is why a sprint-level problem had
    no ticket to act on: `plan_node` builds `known_issue_keys` purely from signals/evidence, so an
    issue that never independently tripped a per-issue signal was invisible to planning even though
    the system had just read it. Keeping the snapshot is what makes a *strategic* option ("move the
    two lowest-priority tickets out") expressible at all.

    Costs one extra structured query per diagnosis (plain SQL over the issues table, not an LLM call)
    rather than changing `_detect_risk_signals`'s return type, which every caller and a dozen tests
    depend on.
    """
    result = await retrieval.query_issues([space_id], {"sprint_ids": [sprint_id], "limit": 200})
    ctx = await _sprint_context(retrieval, space_id, sprint_id)
    start_date, end_date = ctx.get("start_date"), ctx.get("end_date")
    duration = (end_date - start_date).days if start_date and end_date else None
    return SprintSnapshot(
        sprint_name=ctx.get("sprint_name"),
        goal=ctx.get("goal"),
        days_remaining=(end_date - date.today()).days if end_date else None,
        elapsed_percent=(
            int(round(100 * max(0.0, min(1.0, (date.today() - start_date).days / duration))))
            if duration else None
        ),
        issues=[
            SprintIssueSummary(
                issue_key=i["issue_key"], title=i.get("title"), status=i.get("status"),
                priority=i.get("priority"), assignee_name=i.get("assignee_name"),
                story_points=i.get("story_points"),
            )
            for i in result.issues
        ],
    )


async def diagnose_node(
    state: SprintRecoveryState, client, model: str, retrieval,
    on_usage: Optional[UsageRecorder] = None,
) -> dict:
    if not _check_budget(state):
        return {"status": "failed", "error": "token budget exhausted before diagnosis could complete"}

    signals = await _detect_risk_signals(retrieval, state["space_id"], state["sprint_id"])

    # A sprint with zero deterministic risk signals is simply healthy — say so and stop, without
    # spending an LLM call. **Found live**: without this short-circuit, such a sprint still went to the
    # model with an empty signals+evidence prompt, which then (reasonably, given nothing to work with)
    # returned overall_confidence="insufficient" and asked the *human* a question like "what risk
    # signals or evidence were gathered for this sprint?" — the model asking the user to supply the
    # very data the system is supposed to gather for itself. That is nonsense from a user's point of
    # view, and it fired on all 7 real AtlasCart sprints (100% of them), making the confidence gate
    # look like the normal path when it should be the exception.
    if not signals:
        return {
            "status": "no_risk_found", "risk_signals": [], "evidence": [], "hypotheses": [],
            "clarification_question": None,
        }

    flagged_keys = sorted({s.issue_key for s in signals if s.issue_key})
    evidence = await _gather_evidence(retrieval, state["space_id"], state["sprint_id"], flagged_keys)

    # Restate every deterministic signal as citable evidence. These are the most reliable facts in the
    # whole run (computed by code, never inferred), yet the original prompt showed them in a separate
    # "risk signals" block the model was structurally forbidden to cite — `_apply_grounding` only
    # accepts ids that exist in `evidence`. **Found live**: a sprint whose only signal was sprint-level
    # (`low_completion_forecast` carries no issue_key, so `_gather_evidence` fetched nothing at all)
    # gave the model facts it could not legally reference; every hypothesis it produced was then
    # dropped for citing an unknown id, leaving zero hypotheses and, one node later, a bare
    # "every proposed plan cited only invalid/unknown issue keys" failure with no explanation.
    signal_evidence = [
        EvidenceItem(
            citation_id=f"risk{i}", issue_key=s.issue_key or "", source_type="risk_signal",
            content=f"{_SIGNAL_LABELS.get(s.signal_type, s.signal_type)}: {s.description}",
        )
        for i, s in enumerate(signals, start=1)
    ]
    evidence = signal_evidence + evidence

    if state["clarification_answer"]:
        evidence = evidence + [EvidenceItem(
            citation_id=f"human-clarification-{state['clarification_rounds']}",
            issue_key="", source_type="comment", content=state["clarification_answer"],
        )]

    evidence_text = "\n".join(f"[{e.citation_id}] ({e.issue_key} / {e.source_type}) {e.content}" for e in evidence)
    signals_text = "\n".join(f"- {s.signal_type}: {s.description}" for s in signals)
    # The sprint's own committed goal. "Will we hit the commitment?" is a different question from
    # "will every ticket close?", and it's the one a PM actually asks — see `_sprint_context`.
    sprint_goal = (await _sprint_context(retrieval, state["space_id"], state["sprint_id"])).get("goal")
    goal_text = f"Sprint goal (what this sprint committed to deliver): {sprint_goal}\n" if sprint_goal else ""
    prompt = (
        f"Sprint: {state['sprint_name']}\n{goal_text}\nDeterministic risk signals:\n{signals_text}\n\n"
        f"Evidence gathered (cite by id, never invent an id):\n{evidence_text}\n\n"
        "Propose root-cause hypotheses for why this sprint may be at risk. Judge risk against the "
        "sprint goal above where one is given — a sprint can miss individual tickets and still hit "
        "its goal, or close most tickets and still miss it. Every hypothesis must cite "
        "at least one real evidence id above. Within `statement` itself, write the evidence id in "
        "square brackets — e.g. [ev1] or [risk2] — directly after the specific clause it supports, not "
        "bundled at the end of the sentence; a reader should be able to tell which fact backs which "
        "claim. Every bracketed id in `statement` must also appear in `supporting_evidence_ids`, and "
        "vice versa — the two must match exactly. Set overall_confidence='insufficient' only if there "
        "is a genuine, specific gap a human could fill — not as a default hedge."
    )
    result, tokens_used, cost_used = await _call_model(client, model, DiagnosisResult, prompt, on_usage)
    grounded = _apply_grounding(result.hypotheses, evidence)
    update = {
        "risk_signals": signals, "evidence": evidence, "hypotheses": grounded,
        # Carried forward, not discarded — see `_build_sprint_snapshot`. This is what lets `plan_node`
        # reason about issues that never tripped a per-issue signal of their own.
        "sprint_snapshot": await _build_sprint_snapshot(retrieval, state["space_id"], state["sprint_id"]),
        "token_usage": state["token_usage"] + tokens_used,
        "cost_usd": state.get("cost_usd", 0.0) + cost_used,
    }
    if result.overall_confidence == "insufficient" and state["clarification_rounds"] < state["max_clarification_rounds"]:
        update["status"] = "diagnosing"  # will route to clarify
        update["clarification_question"] = result.clarifying_question
    else:
        update["status"] = "diagnosing"  # will route to plan
        update["clarification_question"] = None
    return update


def _after_diagnose(state: SprintRecoveryState) -> str:
    if state["status"] in ("failed", "no_risk_found"):
        return END
    if state["clarification_question"] and state["clarification_rounds"] < state["max_clarification_rounds"]:
        return "clarify"
    return "plan"


async def clarify_node(state: SprintRecoveryState) -> dict:
    """Pauses for exactly one specific question, then loops back to `diagnose` with the answer folded
    in as evidence — this is the confidence-gated pause, distinct in shape from the plan-approval pause
    below (it carries a free-text answer, not a decision enum).
    """
    answer = interrupt({"question": state["clarification_question"], "round": state["clarification_rounds"] + 1})
    return {
        "clarification_answer": answer.get("answer", ""),
        # Held separately from `clarification_answer` (which persists) so `plan_node` can tell "a human
        # just said this, reply to it" apart from "a human said this several rounds ago" — see the
        # field's own comment in state.py.
        "unanswered_human_note": answer.get("answer", ""),
        "clarification_rounds": state["clarification_rounds"] + 1,
        "status": "diagnosing",
    }


def _render_snapshot(snapshot: Optional[SprintSnapshot]) -> str:
    """The sprint's roster as prompt text. Deliberately includes every issue with its status, owner,
    points and priority — a strategic option ("protect the goal, drop the rest") can only be proposed
    by something that can see what "the rest" actually is.
    """
    if snapshot is None or not snapshot.issues:
        return ""
    lines = [
        f"- {i.issue_key} [{i.status or 'unknown status'}] "
        f"{'(' + str(i.story_points) + ' pts) ' if i.story_points is not None else ''}"
        f"{'priority=' + i.priority + ' ' if i.priority else ''}"
        f"{'owner=' + i.assignee_name + ' ' if i.assignee_name else 'unassigned '}"
        f"— {i.title or ''}".rstrip()
        for i in snapshot.issues
    ]
    header = "Every issue in this sprint right now:"
    timing = []
    if snapshot.elapsed_percent is not None:
        timing.append(f"{snapshot.elapsed_percent}% of the sprint has elapsed")
    if snapshot.days_remaining is not None:
        timing.append(f"{snapshot.days_remaining} day(s) remain")
    timing_text = f"({', '.join(timing)})\n" if timing else ""
    return f"\n{timing_text}{header}\n" + "\n".join(lines) + "\n"


async def plan_node(
    state: SprintRecoveryState, client, model: str, retrieval,
    on_usage: Optional[UsageRecorder] = None,
) -> dict:
    """**Found live, not hypothetically**: the first end-to-end run against real AtlasCart data
    produced actions with `target_issue_key` values like `"ev15"`, `"ev3"` — the model had confused
    `EvidenceItem.citation_id` (only ever meaningful for `RootCauseHypothesis.supporting_evidence_ids`)
    with a real issue key, because the original prompt showed it citation ids without ever separately
    listing which issue keys actually exist. Fixed two ways, not just one: the prompt now lists real
    issue keys as their own explicit, separately-labeled set (never call it "evidence" or "citations"
    in that list), *and* `_validate_plan_issue_keys` below drops — doesn't silently keep — any action
    naming a key outside that set, the same "prompt clarity is necessary but insufficient, verify in
    code" split every grounding check in this codebase already uses.
    """
    if not _check_budget(state):
        return {"status": "failed", "error": "token budget exhausted before plan generation could complete"}
    snapshot = state.get("sprint_snapshot")
    flagged_issue_keys = sorted(
        {e.issue_key for e in state["evidence"] if e.issue_key}
        | {s.issue_key for s in state["risk_signals"] if s.issue_key}
        | {k.upper() for e in state["evidence"] for k in _ISSUE_KEY_RE.findall(e.content)}
    )
    # Every action type this workflow can execute targets a specific issue, so `known_issue_keys` is
    # what bounds what a plan may legally act on. It used to be *only* the flagged set above — which
    # meant an issue the system had already read but which hadn't independently tripped a signal was
    # invisible to planning. The snapshot widens this to the whole sprint (see `_build_sprint_snapshot`):
    # a strategic direction like "move the two lowest-priority tickets out" inherently acts on issues
    # that are not themselves the thing that went wrong.
    known_issue_keys = sorted(set(flagged_issue_keys) | {i.issue_key for i in (snapshot.issues if snapshot else [])})

    # No specific issue tripped a signal, but the sprint is at risk anyway (e.g. `low_completion_forecast`
    # alone, which carries no issue_key). The sprint roster above is what keeps this plannable rather
    # than a dead end: a plan can act on any issue in the sprint, not only ones that independently
    # tripped a signal, so "move the two lowest-priority tickets out" is expressible even though no
    # single ticket is "broken."
    if not known_issue_keys:
        # Nothing at all to reason about — not even a sprint roster. Genuinely nothing to offer.
        return {
            "status": "escalated",
            "error": "Risk was detected at the sprint level, but it isn't tied to any specific issue "
                     "this workflow can act on — a human needs to decide what to do.",
        }
    hyps_text ="\n".join(f"- ({h.confidence}) {h.statement} [cites: {', '.join(h.supporting_evidence_ids)}]" for h in state["hypotheses"])
    prior_note = ""
    if state["escalation_round"] > 0:
        prior_actions = state.get("prior_committed_actions") or []
        done_text = "\n".join(f"  - {a}" for a in prior_actions) or "  (nothing recorded)"
        prior_note = (
            f"\n\nThis is escalation round {state['escalation_round']}: the risk is still present after "
            f"{len(prior_actions)} action(s) already committed in earlier rounds, listed below. Do not "
            "repeat any of these — in particular, do not raise a priority that a prior action already "
            "raised (it has no further effect once at the ceiling), and do not re-comment the same "
            "request already made. Propose genuinely different actions, or escalate visibility/ownership "
            "in a way the list below hasn't already tried:\n" + done_text
        )
    if state.get("human_plan_feedback"):
        prior_note += (
            f"\n\nA human reviewed the previous plans and rejected all of them with this feedback: "
            f"\"{state['human_plan_feedback']}\". Address this feedback directly in the new plans — do "
            "not repeat what was rejected."
        )
    # `RecoveryPlan.impact_on_goal` has always been a required field of the schema, and this prompt has
    # always asked the model to fill it in — while never once showing it what the goal actually says.
    # "Impact on goal" was therefore the model inferring a plausible goal from ticket titles. Showing
    # the real one makes that field mean what it claims to.
    sprint_goal = (await _sprint_context(retrieval, state["space_id"], state["sprint_id"])).get("goal")
    goal_text = f"Sprint goal (judge every plan's impact_on_goal against THIS): {sprint_goal}\n" if sprint_goal else ""
    prompt = (
        f"Sprint: {state['sprint_name']}\n{goal_text}{_render_snapshot(snapshot)}"
        f"\nRoot-cause hypotheses (the bracketed [cites: ...] ids are "
        f"internal evidence references, NEVER usable as target_issue_key):\n{hyps_text}{prior_note}\n\n"
        f"Real issue keys this plan may act on — target_issue_key/depends_on_issue_key MUST be one of "
        f"exactly these, verbatim, never an evidence citation id and never invented:\n"
        f"{', '.join(known_issue_keys)}\n\n"
        "Propose 1-3 concrete recovery plans — as many genuinely distinct, worthwhile approaches as "
        "actually exist for this situation. Do not pad the count to reach 2 or 3 with a weak or "
        "redundant option; if there is really only one sensible approach, propose just that one. "
        "Every action must use one of these action_types: "
        "link_dependency, change_priority, move_out_of_sprint, add_comment. When the evidence names "
        "an issue's owner, address that person by name at the start of any add_comment aimed at them "
        "(e.g. 'Maya Chen — this has been blocked for 5 days...') and ask them for the specific thing "
        "only they can supply. Never ask a comment's reader to identify the owner when the evidence "
        "already states who it is. If the evidence explains an issue's lack of progress by an external "
        "blocker outside that owner's control (e.g. the team was diverted to a production incident, "
        "waiting on another team, or blocked by a dependency), any comment addressed to that owner "
        "must acknowledge the blocker directly and ask about ITS status (e.g. 'is the incident "
        "resolved yet, when can you get back to this') — do not ask them to simply confirm they've "
        "resumed the original work or commit to a completion date as if the blocker no longer applies; "
        "that contradicts the evidence the plan itself is built on. If a hypothesis itself "
        "concludes the signal is a false positive, already resolved, or that evidence explicitly asks "
        "not to escalate/pull the issue, every proposed plan must respect that: prefer a conservative "
        "action (e.g. add_comment to confirm/close the loop) and do not offer move_out_of_sprint or a "
        "raised priority as an option for that issue, even as an alternative choice."
    )
    # **Found live, from direct product feedback**: a human rewound with "we will add 2 extra engineers
    # to this sprint" and got back plans that never mentioned engineers. It had genuinely been taken
    # into account — but only inside a hypothesis, which reads as silence from the plan's point of view.
    # Asked for only when a note is actually outstanding, so no tokens are spent (and no answer is
    # invented) on rounds where nobody said anything.
    human_note = state.get("unanswered_human_note")
    if human_note:
        prompt += (
            f"\n\nThe person reviewing this sprint just told you: \"{human_note}\"\n"
            "Fill in `response_to_human_note`: reply to them directly, in 1-3 plain sentences, "
            "addressing them as \"you\". If what they said changed these plans, say specifically what "
            "it changed. If it could NOT change them, say exactly why and name the concrete obstacle — "
            "for example the work is blocked on a party outside this team's control, or what they "
            "described isn't something any available action can do (the only actions that exist are "
            "link_dependency, change_priority, move_out_of_sprint, add_comment; there is no way to "
            "assign people, change scope estimates, or contact anyone outside Jira). Never claim to "
            "have acted on what they said if the plans above don't actually reflect it — being straight "
            "about why it doesn't help is more useful to them than a plan that pretends."
        )
    result, tokens_used, cost_used = await _call_model(client, model, RecoveryPlanSet, prompt, on_usage)
    plans = _validate_plan_issue_keys(result.plans, known_issue_keys)
    if not plans:
        return {
            "status": "failed", "error": "every proposed plan cited only invalid/unknown issue keys",
            # Charged even though the result was unusable — the call was still paid for, and a cost
            # number that only counts successful calls would understate exactly the runs worth noticing.
            "token_usage": state["token_usage"] + tokens_used,
            "cost_usd": state.get("cost_usd", 0.0) + cost_used,
        }
    return {
        "plans": plans, "status": "awaiting_plan_approval",
        "token_usage": state["token_usage"] + tokens_used,
        "cost_usd": state.get("cost_usd", 0.0) + cost_used,
        # Both reset unconditionally: the note has now been answered, and `note_response` must only
        # ever describe the plans it is shown beside — a stale reply carried into a later round would
        # be worse than none, since it would look like an answer to something nobody just asked.
        "note_response": result.response_to_human_note if human_note else None,
        "unanswered_human_note": None,
    }


def _validate_plan_issue_keys(plans: List[RecoveryPlan], known_issue_keys: List[str]) -> List[RecoveryPlan]:
    """Drops any action naming an issue key outside what was actually gathered as evidence/risk
    signals this run — the code-level backstop for the prompt fix above. A plan that loses all its
    actions this way is dropped entirely rather than shown to a human as an empty, unapprovable plan.
    """
    known = set(known_issue_keys)
    kept_plans = []
    for plan in plans:
        kept_actions = []
        for a in plan.actions:
            bad = {a.target_issue_key} | ({a.depends_on_issue_key} if a.depends_on_issue_key else set())
            bad -= known
            if bad:
                logger.warning("dropping action citing unknown issue key(s)", extra={"action_type": a.action_type, "bad_keys": list(bad)})
                continue
            kept_actions.append(a)
        if kept_actions:
            kept_plans.append(RecoveryPlan(
                plan_id=plan.plan_id, name=plan.name, rationale=plan.rationale,
                impact_on_goal=plan.impact_on_goal, actions=kept_actions,
            ))
        else:
            logger.warning("dropping plan with zero valid actions after issue-key validation", extra={"plan_id": plan.plan_id})
    return kept_plans


def _after_plan(state: SprintRecoveryState) -> str:
    return END if state["status"] in ("failed", "escalated") else "approval"


async def approval_node(
    state: SprintRecoveryState, space_membership: SpaceMembershipChecker, jira: JiraActionsClient,
) -> dict:
    """The plan-approval pause — same *shape* as `rollout_graph.py`'s `review_node` (approve/edit/
    reject a finished artifact), included here to show the two pause shapes side by side in one
    workflow rather than because this one alone would justify a new graph. Unlike rollout's binary
    approve-or-reject, this one has a third real path: `revise` — a human who doesn't like any of the
    offered plans but *does* have something concrete to say about why loops back into `plan_node` with
    that feedback folded in, rather than the workflow dead-ending on `reject`. Bounded by
    `max_plan_revision_rounds` for the same reason every other loop here is bounded; past the cap this
    degrades to a plain reject rather than looping forever.
    """
    payload = interrupt({"plans": [p.model_dump() for p in state["plans"]]})
    decision = payload.get("decision")

    try:
        await space_membership.validate(state["user_id"], state["username"], [state["space_id"]])
    except SpaceMembershipError as exc:
        return {"decision": "reject", "status": "rejected", "error": str(exc)}

    if decision == "revise":
        feedback = (payload.get("feedback") or "").strip()
        if not feedback:
            return {"decision": "reject", "status": "rejected", "error": "revise requires feedback text"}
        if state["plan_revision_round"] >= state["max_plan_revision_rounds"]:
            return {
                "decision": "reject", "status": "rejected",
                "error": f"reached the {state['max_plan_revision_rounds']}-revision limit without an "
                         "approved plan — rejecting rather than looping forever",
            }
        return {
            "decision": "revise", "status": "revising",
            "human_plan_feedback": feedback, "plan_revision_round": state["plan_revision_round"] + 1,
            # The third way a human says something to this workflow, and it deserves the same direct
            # reply as a clarification answer or a rewind note — see `unanswered_human_note` in state.py.
            "unanswered_human_note": feedback,
        }

    if decision == "reject":
        return {"decision": "reject", "status": "rejected"}

    plan_id = payload.get("planId") or payload.get("plan_id")
    chosen = next((p for p in state["plans"] if p.plan_id == plan_id), None)
    if chosen is None:
        return {"decision": "reject", "status": "failed", "error": f"unknown plan_id {plan_id!r}"}

    if decision == "edit" and payload.get("actions") is not None:
        chosen = RecoveryPlan(
            plan_id=chosen.plan_id, name=chosen.name, rationale=chosen.rationale,
            impact_on_goal=chosen.impact_on_goal,
            actions=[RecoveryAction.model_validate(a) for a in payload["actions"]],
        )

    # **Found live, empirically reproduced, not theorized**: capturing each issue's pre-write baseline
    # lazily inside commit_one_action_node — right before that specific write — has a real gap a caught
    # JiraActionError can't close: a genuine process crash between the write landing on jira-backend and
    # commit_one_action_node returning (so LangGraph never checkpoints that attempt at all) means the
    # resumed attempt recomputes the baseline from scratch, via a GET that now runs *after* the crashed
    # write already landed. Reproduced with a two-process Postgres crash-resume harness (same shape as
    # the real crash-resume test): the resumed baseline for the crashed action came back as the
    # post-write timestamp, not the true pre-write one — reintroducing Follow-up 12's false-`recovered`
    # bug through exactly the scenario this feature's durable-execution claim is built on. Fixed by
    # capturing every target issue's baseline here instead, on the *final*, post-edit action list,
    # before any write in this round has happened — this return is a normal node completion LangGraph
    # checkpoints atomically, so by the time commit_one_action_node ever runs (first attempt or any
    # resume), the baseline it needs already exists. commit_one_action_node's own lazy capture stays as
    # a defensive fallback, not the primary path.
    #
    # **Found live, a second time, in the very fix meant to close the first one**: this originally also
    # captured `depends_on_issue_key` "for completeness." That's wrong, not just unnecessary —
    # `commit_one_action_node` only ever writes a *watermark* for `action.target_issue_key`
    # (`link_dependency` never touches jira-backend's row for the depended-on issue at all, only
    # creates a link entity), so a `depends_on_issue_key` given a baseline here would get `after=None`
    # forever. `_last_update_not_by_this_workflow` reads a missing watermark as "we can't prove someone
    # touched it after us" and falls back to the baseline unconditionally — which means a real human
    # updating that issue later in the same round would be silently ignored, staleness frozen at the
    # pre-approval snapshot with no way to ever un-stick it. Scoped to target_issue_key only, the only
    # key type `commit_one_action_node` ever actually produces a watermark for.
    pre_writes = dict(state.get("pre_write_updated_at") or {})
    keys_needing_baseline = {
        a.target_issue_key for a in chosen.actions if a.target_issue_key not in pre_writes
    }
    for key in sorted(keys_needing_baseline):
        before = await jira.issue_updated_at(state["space_id"], key, state["user_id"], state["username"])
        if before:
            pre_writes[key] = before

    return {
        "decision": decision or "approve", "approved_plan": chosen, "status": "committing",
        "committed_actions": {}, "index_watermarks": {}, "pre_write_updated_at": pre_writes,
    }


def _after_approval(state: SprintRecoveryState) -> str:
    if state["status"] == "revising":
        return "plan"
    return END if state["status"] in ("rejected", "failed") else "commit_one_action"


async def commit_one_action_node(state: SprintRecoveryState, jira: JiraActionsClient) -> dict:
    """One action per graph step — identical reasoning to `rollout_graph.py::commit_one_node`: the
    checkpointer only persists between steps, so a crash mid-action-list must not be able to lose the
    record of which actions already executed.
    """
    plan = state["approved_plan"]
    remaining = [
        (i, a) for i, a in enumerate(plan.actions) if str(i) not in state["committed_actions"]
    ]
    if not remaining:
        return {"status": "waiting_reevaluation"}

    idx, action = remaining[0]
    # space_id + sprint_id + escalation_round + plan_revision_round + action index is unique per action
    # within this sprint's active recovery lifecycle without needing thread_id (which lives in LangGraph's
    # RunnableConfig, not state — see `_traced`'s own comment on why it's kept out of the state dict). Not
    # unique across two threads racing the *same* sprint at the *same* round simultaneously, but nothing in
    # this workflow supports starting two concurrent recovery threads for one sprint today anyway.
    idempotency_key = (
        f"sprint-recovery:{state['space_id']}:{state['sprint_id']}:"
        f"{state['escalation_round']}:{state['plan_revision_round']}:{idx}"
    )
    # Captured *before* the write: jira-backend stamps `updatedAt = now()` on any issue update, which
    # would otherwise make this workflow's own action look like somebody making progress on a stalled
    # ticket. See `_last_update_not_by_this_workflow` for the false-`recovered` this prevents. First
    # write wins — if a later round touches the same issue again, the original human baseline is the
    # one that matters, not a timestamp this workflow itself produced in an earlier round.
    pre_writes = dict(state.get("pre_write_updated_at") or {})
    if action.target_issue_key not in pre_writes:
        before = await jira.issue_updated_at(
            state["space_id"], action.target_issue_key, state["user_id"], state["username"],
        )
        if before:
            pre_writes[action.target_issue_key] = before

    try:
        result_summary = await jira.execute(
            state["space_id"], action, state["user_id"], state["username"], idempotency_key=idempotency_key,
        )
    except JiraActionError as exc:
        # **Found live in review, not by a failing test**: this used to return without `pre_writes`,
        # discarding the baseline just captured above. Harmless-looking on a clean failure (the write
        # never happened, so re-reading the baseline on retry gets the same answer) — but on the exact
        # case the idempotency key above exists for (the write actually succeeded server-side, the
        # client only saw a timeout), a retry's baseline GET would run *after* that write already
        # landed, silently reintroducing the same false-`recovered` bug through the retry path instead
        # of the happy path. Persisting the pre-write baseline here, captured before this attempt ever
        # touched Jira, closes that regardless of which way the retry's own GET would have gone.
        return {"status": "failed", "error": str(exc), "pre_write_updated_at": pre_writes}

    committed = dict(state["committed_actions"])
    committed[str(idx)] = result_summary
    done = len(committed) == len(plan.actions)
    # Read the issue's own `updatedAt` back from jira-backend (write-side source of truth) so
    # `reevaluate_node` can wait for the read model to actually reflect this write instead of guessing
    # how long propagation takes — see `_await_index_catch_up`.
    watermarks = dict(state.get("index_watermarks") or {})
    updated_at = await jira.issue_updated_at(
        state["space_id"], action.target_issue_key, state["user_id"], state["username"],
    )
    if updated_at:
        watermarks[action.target_issue_key] = updated_at
    return {
        "committed_actions": committed, "index_watermarks": watermarks, "pre_write_updated_at": pre_writes,
        "status": "waiting_reevaluation" if done else "committing",
    }


def _after_commit_one_action(state: SprintRecoveryState) -> str:
    if state["status"] == "failed":
        return END
    if state["status"] == "waiting_reevaluation":
        return "wait_for_reevaluation"
    return "commit_one_action"


async def wait_for_reevaluation_node(state: SprintRecoveryState) -> dict:
    """The third, differently-shaped pause: unlike `clarify` (a free-text answer) and `approval` (a
    decision + optional edit), this one's resume payload can come from **two different kinds of
    caller** using the identical `Command(resume=...)` protocol — a human hitting "re-check now" in the
    UI, or `kafka_trigger.py`'s consumer reacting to a real `IssueContentChangedEvent` on an issue this
    workflow cares about. Both just call the same resume; this node doesn't need to know which.
    """
    interrupt({"waiting_since_committed_actions": len(state["committed_actions"]), "sprint_id": state["sprint_id"]})
    return {"status": "diagnosing"}


async def reevaluate_node(state: SprintRecoveryState, retrieval) -> dict:
    """Resumed either by a real Kafka event (`kafka_trigger.py`) or a manual trigger — re-runs the
    same deterministic risk detection used at the start, so "recovered" is a code-computed fact, not
    an LLM's opinion that things look better.

    Waits for the read model to catch up to this workflow's own committed writes first: every resume
    path (auto-after-approve, a human clicking "re-check now", and the Kafka trigger — which runs on a
    *different consumer group* than vectorization-service and so can beat it to the index) is capable
    of reading a sprint that doesn't yet reflect the actions just taken. See `_await_index_catch_up`.
    """
    watermarks = state.get("index_watermarks") or {}
    caught_up = await _await_index_catch_up(
        retrieval, state["space_id"], state["sprint_id"], watermarks,
    )
    # Pair each issue this workflow wrote to with (its timestamp before our write, the one our write
    # produced) so staleness isn't measured from our own automated touch — see
    # `_last_update_not_by_this_workflow` for the false-`recovered` this closes.
    pre_writes = state.get("pre_write_updated_at") or {}
    own_writes = {key: (before, watermarks.get(key)) for key, before in pre_writes.items()}
    signals = await _detect_risk_signals(
        retrieval, state["space_id"], state["sprint_id"], own_writes=own_writes,
    )
    # **Found live**: this used to only check for `long_in_progress`/`low_completion_forecast` by
    # name — when `blocked_no_flag` was added as a real signal type, it was never added here, so a
    # sprint whose only remaining problem was still-blocked issues (with completion otherwise healthy)
    # would have been reported "recovered" while genuinely still blocked. Any signal firing at all
    # means still at risk; there is no case where `_detect_risk_signals` returns a non-empty list and
    # the sprint should be considered healthy.
    still_at_risk = bool(signals)
    if not still_at_risk:
        # Set fresh even on the happy path — a stale read can never manufacture a false "recovered" (see
        # the field's own comment in state.py), but it's still the honest, current answer to "did this
        # particular check actually confirm the read model caught up."
        return {"status": "recovered", "risk_signals": signals, "index_catch_up_timed_out": not caught_up}

    if state["escalation_round"] >= state["max_escalation_rounds"]:
        return {"status": "escalated", "risk_signals": signals, "index_catch_up_timed_out": not caught_up}
    plan = state.get("approved_plan")
    just_done = [
        _describe_action(a) for i, a in enumerate(plan.actions) if str(i) in (state.get("committed_actions") or {})
    ] if plan else []
    return {
        "status": "diagnosing", "risk_signals": signals, "escalation_round": state["escalation_round"] + 1,
        "committed_actions": {}, "index_watermarks": {}, "index_catch_up_timed_out": not caught_up,
        "approved_plan": None, "decision": None,
        "prior_committed_actions": state.get("prior_committed_actions", []) + just_done,
    }


def _after_reevaluate(state: SprintRecoveryState) -> str:
    if state["status"] == "diagnosing":
        return "plan"  # re-plan directly — hypotheses already exist, no need to re-diagnose from scratch
    return END


def _traced(node_name: str, fn):
    """Wraps a node function in one span per execution — see the module-level `_tracer` comment for
    why this is done here (wrapping the closures bound into the graph) rather than inside each node
    function body: it reaches every node uniformly, including `clarify_node`/`wait_for_reevaluation_node`
    (which take no extra dependencies to close over), without re-indenting each function's existing
    body. `thread_id` is read from LangGraph's own `RunnableConfig` (threaded through automatically by
    `ainvoke`), not from the state dict, since it's a run identity, not workflow data.
    """
    async def _wrapped(state: SprintRecoveryState, config=None) -> dict:
        thread_id = (config or {}).get("configurable", {}).get("thread_id", "unknown")
        on_stage = ON_STAGE_VAR.get()
        label = _STAGE_LABELS.get(node_name)
        if on_stage and label:
            on_stage(label)
        with _tracer.start_as_current_span(
            f"sprint_recovery.{node_name}",
            attributes={
                SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
                "sprint_recovery.thread_id": thread_id,
                "sprint_recovery.status_before": state.get("status", ""),
            },
        ) as span:
            result = await fn(state)
            if "status" in result:
                span.set_attribute("sprint_recovery.status_after", result["status"])
            return result
    return _wrapped


def build_sprint_recovery_graph(
    client, model: str, space_membership: SpaceMembershipChecker, retrieval, jira: JiraActionsClient,
    on_usage: Optional[UsageRecorder] = None,
):
    async def _diagnose(state: SprintRecoveryState) -> dict:
        return await diagnose_node(state, client, model, retrieval, on_usage)

    async def _plan(state: SprintRecoveryState) -> dict:
        return await plan_node(state, client, model, retrieval, on_usage)

    async def _approval(state: SprintRecoveryState) -> dict:
        return await approval_node(state, space_membership, jira)

    async def _commit_one(state: SprintRecoveryState) -> dict:
        return await commit_one_action_node(state, jira)

    async def _reevaluate(state: SprintRecoveryState) -> dict:
        return await reevaluate_node(state, retrieval)

    graph = StateGraph(SprintRecoveryState)
    graph.add_node("diagnose", _traced("diagnose", _diagnose))
    graph.add_node("clarify", _traced("clarify", clarify_node))
    graph.add_node("plan", _traced("plan", _plan))
    graph.add_node("approval", _traced("approval", _approval))
    graph.add_node("commit_one_action", _traced("commit_one_action", _commit_one))
    graph.add_node("wait_for_reevaluation", _traced("wait_for_reevaluation", wait_for_reevaluation_node))
    graph.add_node("reevaluate", _traced("reevaluate", _reevaluate))

    graph.add_edge(START, "diagnose")
    graph.add_conditional_edges("diagnose", _after_diagnose, {"clarify": "clarify", "plan": "plan", END: END})
    graph.add_edge("clarify", "diagnose")
    graph.add_conditional_edges("plan", _after_plan, {"approval": "approval", END: END})
    graph.add_conditional_edges(
        "approval", _after_approval, {"commit_one_action": "commit_one_action", "plan": "plan", END: END},
    )
    graph.add_conditional_edges(
        "commit_one_action", _after_commit_one_action,
        {"commit_one_action": "commit_one_action", "wait_for_reevaluation": "wait_for_reevaluation", END: END},
    )
    graph.add_edge("wait_for_reevaluation", "reevaluate")
    graph.add_conditional_edges("reevaluate", _after_reevaluate, {"plan": "plan", END: END})
    return graph


async def trigger_reevaluation(compiled_graph, thread_id: str, source: str, detail: str = "") -> dict:
    """Resumes the `wait_for_reevaluation` pause — the one interrupt in this graph answerable by
    either a human ("re-check now") or `kafka_trigger.py` reacting to a real Jira event, via the
    identical `Command(resume=...)` protocol. `source` is recorded for observability, not branched on.
    """
    from langgraph.types import Command

    config = {"configurable": {"thread_id": thread_id}}
    return await compiled_graph.ainvoke(Command(resume={"source": source, "detail": detail}), config=config)


def _has_uninterrupted_pending_task(snap) -> bool:
    """True when LangGraph itself has a real pending task with nobody actually waiting on a human:
    `snap.next` is non-empty and no task on it carries an `interrupt()`. This is the general form of "a
    real crash left a pending task" — `status == "committing"` used to be the only instance of it this
    module checked by name.

    **Found live**: a crash between `reevaluate_node` looping back for the next escalation round and
    `plan_node` finishing left a checkpoint at `status == "diagnosing"` with `snap.next == ("plan",)`
    and no interrupt — a genuine crash, structurally identical to a crash mid-commit, but `status`
    alone can't tell it apart from the *other* thing that also leaves `status == "diagnosing"`: a
    real, intentional pause inside `clarify_node` waiting on a human's answer (which always carries a
    real interrupt on its task). Checking `snap.next`/interrupts directly, instead of hardcoding one
    status string, is what makes this correct for either case without conflating them: a genuine pause
    must never be silently resumed this way, only a crash with nothing pending on a human should be.
    """
    return bool(snap.next) and not any(t.interrupts for t in snap.tasks)


def is_resumable_after_a_crash(snap) -> bool:
    """Whether `retry_recovery_commit` can do anything useful with this checkpoint at all — either a
    `status == "failed"` thread (needs re-arming, see that function) or a genuine crash with real
    pending work (see `_has_uninterrupted_pending_task`). Used by the `/retry` route's own guard so it
    rejects the same cases `retry_recovery_commit` itself wouldn't know what to do with — for example a
    checkpoint genuinely waiting on a human at `clarify`/`approval`/`wait_for_reevaluation`, which has
    its own proper resume endpoint and must not be retried through this generic one.
    """
    return snap.values.get("status") == "failed" or _has_uninterrupted_pending_task(snap)


async def retry_recovery_commit(compiled_graph, thread_id: str) -> dict:
    """Un-sticks a thread `is_resumable_after_a_crash` says is safe to continue. Three cases:

    1. A real crash left a pending task with no interrupt (`status == "committing"`, or any other node
       a process died in the middle of — see `is_resumable_after_a_crash`): bare `ainvoke(None, ...)`
       resumes it exactly where LangGraph's own checkpoint says it stopped, same as
       `app/planning/rollout_graph.py::retry_rollout`.
    2. `status == "failed"` *after* a plan was approved (a `JiraActionError` mid-commit): nothing new
       needs deciding, just re-arm `commit_one_action` by replaying `approval`'s outgoing edge via
       `as_node="approval"` — see that sibling function's docstring for why `as_node` names the node
       whose edge should fire, not the node that reruns.
    3. `status == "failed"` *before* any plan was ever approved (diagnose/plan-generation hit the token
       budget, or `plan_node`'s `_validate_plan_issue_keys` guardrail rejected every proposed action as
       citing an unknown issue key). Here `approved_plan` is still `None`. Case 2's `as_node="approval"`
       path would re-arm `commit_one_action_node`, which unconditionally reads
       `state["approved_plan"].actions` — a guaranteed crash on `None`. Retrying this case has to
       re-enter `plan` instead, via `as_node="diagnose"` (evidence/hypotheses are already in state from
       the diagnose that already succeeded, so this cleanly re-runs just the plan LLM call, not the
       whole diagnosis).

    Case 1 must be checked first: for a crash landing anywhere *other* than mid-commit, `approved_plan`
    can easily be non-`None` too (a stale value from an *earlier*, already-fully-committed escalation
    round that nothing has cleared yet) — falling into case 2 there would re-arm `commit_one_action`
    against actions already posted to Jira, re-sending them a second time.
    """
    config = {"configurable": {"thread_id": thread_id}}
    snap = await compiled_graph.aget_state(config)
    if _has_uninterrupted_pending_task(snap):
        return await compiled_graph.ainvoke(None, config=config)
    if snap.values.get("approved_plan") is not None:
        await compiled_graph.aupdate_state(config, {"status": "committing", "error": None}, as_node="approval")
        return await compiled_graph.ainvoke(None, config=config)
    await compiled_graph.aupdate_state(config, {"status": "diagnosing", "error": None}, as_node="diagnose")
    return await compiled_graph.ainvoke(None, config=config)


async def list_checkpoint_history(compiled_graph, thread_id: str) -> list:
    """All checkpoints for this thread, newest first — what the UI's time-travel picker lists from.
    `next` (which node would run next from that point) is what makes a checkpoint meaningfully
    "rewindable to" vs. just a log entry — e.g. a checkpoint with `next=('plan',)` is the point right
    after diagnosis finished, before any plan existed yet.
    """
    config = {"configurable": {"thread_id": thread_id}}
    return [s async for s in compiled_graph.aget_state_history(config)]


def _describe_action(action: RecoveryAction) -> str:
    """One executed action, in a sentence a non-engineer can audit — including the full comment text.
    Used by the escalation handoff panel and its notification/log equivalent, both of which exist so a
    human can see exactly what was already done in Jira on their behalf.
    """
    key = action.target_issue_key
    if action.action_type == "add_comment":
        return f'Commented on {key}: "{action.comment_body}"'
    if action.action_type == "change_priority":
        return f"Raised {key} priority to {action.new_priority}"
    if action.action_type == "move_out_of_sprint":
        return f"Moved {key} out of the sprint"
    if action.action_type == "link_dependency":
        return f"Marked {key} as blocked by {action.depends_on_issue_key}"
    return f"{action.action_type} on {key}"


async def build_escalation_summary(compiled_graph, thread_id: str) -> dict:
    """Synthesizes what was actually tried across every escalation round — for the moment
    `reevaluate_node` gives up and hands off to a human, who shouldn't have to reconstruct "what did
    the automation already attempt" from a raw checkpoint list themselves.

    Returns **structured** data (`{"rounds": [{"round", "plan_name", "rationale", "actions"}, ...],
    "still_at_risk_reasons": [str], "unaddressed_issue_keys": [str]}`), not a pre-flattened paragraph —
    found live that one run-on paragraph covering 3 rounds was unreadable in the UI. Callers render it
    for their own audience: `sprint_recovery_routes.py` returns it structured for the frontend to lay
    out as per-round cards; `flatten_escalation_summary` below turns it into a single string for the
    notification/log path, which has no UI to lay cards out in.

    **`unaddressed_issue_keys`, found live**: every escalation looks the same at a glance — red,
    "needs a human decision" — but two very different situations land there. One: every per-issue risk
    signal still open at the final check was already acted on at least once across the rounds above
    (a comment, a reprioritization, a link, a move) — the automation genuinely exhausted what its 4
    action types can do, and what's left is real engineering time, not more planning. The other: some
    flagged issue was never acted on at all (budget ran out first, or a plan never targeted it) — a
    real gap, not just "waiting on a human to do the work." Sprint-level signals (e.g.
    `low_completion_forecast`, which carries no `issue_key`) are excluded from this check on purpose:
    none of the 4 action types can ever mark story points done, so that signal is *expected* to still
    be firing at every escalation regardless of how complete the response was — counting it here would
    make every single escalation look "unresolved," which defeats the point of the distinction.

    Reuses the same `aget_state_history` walk `list_checkpoint_history` already does for the
    time-travel picker rather than a separate accumulating state field: every checkpoint LangGraph
    stores is already a full state snapshot (already relied on for time-travel to work at all), and a
    `status="waiting_reevaluation"` checkpoint is exactly "one approved plan just finished committing"
    — one such checkpoint per escalation round, with `approved_plan`/`committed_actions` already sitting
    right there in that snapshot's `values`. Nothing new to track, just a different read of what's
    already durably recorded.

    Deliberately NOT called from inside `reevaluate_node`: a graph node only has the state dict, not the
    compiled graph object `aget_state_history` needs — and the current checkpoint isn't even saved yet
    while the node producing it is still running, so a node cannot read its own workflow's history
    mid-execution regardless. This is meant to be called from the caller that just resumed the graph and
    observed status=="escalated" in the result, same shape as `_register_if_waiting`'s "react to the
    status *after* the fact" pattern in sprint_recovery_routes.py.
    """
    config = {"configurable": {"thread_id": thread_id}}
    history = [s async for s in compiled_graph.aget_state_history(config)]
    history.reverse()  # aget_state_history yields newest-first; want chronological order here

    rounds: list = []
    seen_rounds = set()
    actioned_issue_keys: set = set()
    for snap in history:
        if snap.values.get("status") != "waiting_reevaluation":
            continue
        plan = snap.values.get("approved_plan")
        if plan is None:
            continue
        round_num = snap.values.get("escalation_round", 0)
        if round_num in seen_rounds:
            continue  # aget_state_history can repeat a snapshot around branch points (e.g. time-travel)
        seen_rounds.add(round_num)
        # Describe each action from the plan itself, not from `committed_actions`' terse result
        # strings. Found live: `JiraActionsClient._add_comment` returns "comment added to ATC-122",
        # which tells a reviewer that *a* comment was posted but not what it said — and the whole
        # point of this panel is letting a human audit what the automation already did on their
        # behalf. The plan's own `RecoveryAction` still carries the full comment body, target
        # priority, and dependency, so read the detail from there and use `committed_actions` only
        # for what it actually knows: which indices really executed.
        committed = snap.values.get("committed_actions") or {}
        actions = []
        for i, a in enumerate(plan.actions):
            if str(i) not in committed:
                continue
            actions.append(_describe_action(a))
            actioned_issue_keys.add(a.target_issue_key)
        rounds.append({
            "round": round_num + 1, "plan_name": plan.name, "rationale": plan.rationale, "actions": actions,
        })

    final_signals = (history[-1].values.get("risk_signals") if history else None) or []
    flagged_issue_keys = {s.issue_key for s in final_signals if s.issue_key}
    return {
        "rounds": rounds,
        "still_at_risk_reasons": [s.description for s in final_signals],
        "unaddressed_issue_keys": sorted(flagged_issue_keys - actioned_issue_keys),
    }


def flatten_escalation_summary(summary: dict) -> str:
    """The single-string rendering of `build_escalation_summary`'s structured output — for the
    notification/log path (Slack text field, WARNING log line), which has no UI to lay per-round cards
    out in."""
    rounds = summary.get("rounds") or []
    if not rounds:
        return "No recovery plan was ever committed before this workflow reached its escalation cap."
    lines = [
        f"Round {r['round']}: approved \"{r['plan_name']}\" ({r['rationale']}). "
        f"Actions taken: {'; '.join(r['actions']) if r['actions'] else 'none'}."
        for r in rounds
    ]
    reasons = summary.get("still_at_risk_reasons") or []
    if reasons:
        lines.append("Still at risk because: " + " ".join(reasons))
    unaddressed = summary.get("unaddressed_issue_keys") or []
    if unaddressed:
        lines.append(f"Never got attempted: {', '.join(unaddressed)}.")
    return " ".join(lines)


async def time_travel_resume(compiled_graph, thread_id: str, target_checkpoint_id: str, note: str) -> dict:
    """Rewinds to an earlier checkpoint and continues forward *from there*, with `note` folded in as
    if a human had just answered a clarifying question at that point — same `as_node="clarify"` trick
    (clarify's own outgoing edge unconditionally goes to `diagnose`, so this re-enters diagnosis with
    the new context, regardless of which later node the original run had already reached). This
    rewrites the thread's own forward history rather than forking a new thread_id — verified directly:
    after resuming from a historical checkpoint, `aget_state(config)` on the *original* thread_id
    reflects the new branch, and the pre-rewind future is still visible via `aget_state_history`, not
    deleted.
    """
    target_config = {
        "configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": target_checkpoint_id}
    }
    forked_config = await compiled_graph.aupdate_state(
        target_config,
        # `unanswered_human_note` too, not just `clarification_answer`: this writes state *as if*
        # `clarify_node` had returned, so anything that node sets has to be set here as well — the node's
        # own body never runs on this path. Without it a rewind note would silently skip `plan_node`'s
        # "reply to what the human just told you" step, which is exactly the case that motivated it.
        {"clarification_answer": note, "unanswered_human_note": note, "clarification_rounds": 0},
        as_node="clarify",
    )
    return await compiled_graph.ainvoke(None, config=forked_config)
