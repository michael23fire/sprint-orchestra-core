"""Agentic RAG with corrective retrieval (a CRAG-style loop), via Claude tool use.

Standard tool use is one retrieval, then answer. **Corrective RAG**'s idea is to not trust the first
retrieval blindly: after retrieving, assess whether what came back actually answers the question, and
if not, retrieve again with a different query before answering. The original CRAG paper (Yan et al.,
2024) does this with a small trained relevance-grading classifier. This implementation makes a
deliberate simplification: it lets the *agent model itself* do the grading, as part of its own
reasoning, using its native tool-use loop — no separate classifier model or extra round-trip. This is
how corrective retrieval is commonly implemented in production agentic systems today (the grading
step is just "is this capable model's own judgment," not a bespoke trained component), and it's the
natural fit for a tool-calling loop: re-retrieval is *just another tool call* the same loop already
supports, triggered by the model's own assessment rather than a fixed pipeline stage.

Security-critical design point: the model is given exactly ONE tool parameter it controls — the
search query text (and optionally retrieval mode). It is never given ``space_ids``. Permission
scoping is injected by this loop from the caller's authenticated identity on every single search
call, regardless of what the model asks for — an LLM must never be trusted with an authorization
parameter, since a prompt-injected or simply confused agent could otherwise ask to search spaces the
user has no access to. This mirrors the same per-space permission boundary jira-backend's FTS enforces
(``SpaceMemberRepository.findActiveSpaceIdsByUserId``) — the boundary is enforced by code the model
cannot influence, not by asking the model nicely in the system prompt.
"""
from __future__ import annotations

import json
import logging
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from app.agent.retrieval_tool import (
    IssueCommentsResult,
    IssueDetailsResult,
    IssueHistoryResult,
    IssueQueryResult,
    RetrievalClient,
    RetrievedChunk,
    SprintQueryResult,
)
from app.llm.pricing import estimate_cost_usd
from app.llm.types import LLMClient, Usage
from app.observability import LLM_CALL_SECONDS, RETRIEVAL_CALL_SECONDS, RETRIEVAL_ROUNDS

logger = logging.getLogger(__name__)

# Exact phrase the system prompt requires when the agent cannot ground an answer in retrieved
# content. A fixed, greppable contract is far more reliable than fuzzy-matching "sounds like a
# refusal" on free-form text — both the API response and the eval harness key off this string.
ABSTENTION_PHRASE = "There is no information about this in the knowledge base."

# Per-request stage-time accumulator, same ContextVar-per-asyncio-task pattern already used for
# `_request_id` in app/observability.py: a single `CragAgent` instance is shared across concurrent
# requests (one per app.state.agent), so accumulating into `self` would let concurrent asks bleed
# their timings into each other. A ContextVar is isolated per asyncio task, so each `ask()` call sees
# only its own accumulator even while other requests are in flight on the same event loop. This is
# additive to (not a replacement for) the existing Prometheus histograms below — those already answer
# "what's the P95 llm-call latency across all requests"; this answers "how did *this* request's total
# latency split across stages," for the /ask response (see AskResponse.stage_timings).
_stage_seconds: ContextVar[Dict[str, float] | None] = ContextVar("_stage_seconds", default=None)


def _add_stage_seconds(stage: str, elapsed: float) -> None:
    acc = _stage_seconds.get()
    if acc is not None:
        acc[stage] = acc.get(stage, 0.0) + elapsed


# Human-readable progress labels for `ask()`'s optional `on_stage` callback (see POST /ask/stream) —
# a name -> present-tense label lookup, not a restructuring of the dispatch logic below that already
# switches on these same tool names for the real work.
_TOOL_STAGE_LABELS = {
    "search_knowledge_base": "searching the knowledge base",
    "query_issues": "looking up issue data",
    "query_sprints": "looking up sprint data",
    "get_issue_history": "checking issue change history",
    "get_issue_comments": "reading issue comments",
    "get_issue_details": "reading issue details",
    "get_issue_attachments": "reading issue attachments",
}

_SEARCH_TOOL = {
    "name": "search_knowledge_base",
    "description": (
        "Search the Jira workspace's knowledge base (issues, comments, attachments) for content "
        "relevant to a question. Call this whenever you need information to answer — never answer "
        "from memory. If the results don't actually address the question, call this again with a "
        "differently-worded or more specific query before giving up — don't settle for a weak match."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language or keyword search query."},
            "mode": {
                "type": "string",
                "enum": ["hybrid", "vector", "lexical"],
                "description": (
                    "'hybrid' (default) combines semantic and keyword matching — use it unless you "
                    "have a specific reason. Use 'lexical' for exact identifiers (e.g. an issue key "
                    "like 'PROJ-42') or rare exact technical terms. Use 'vector' for purely "
                    "conceptual questions with no useful keywords."
                ),
            },
        },
        "required": ["query"],
    },
}

_ISSUE_QUERY_TOOL = {
    "name": "query_issues",
    "description": (
        "Structured lookup over issue METADATA — use this for counting, listing, filtering, or "
        "recency questions, NOT search_knowledge_base. It returns an exact total count, a breakdown "
        "by type and by status, and an ordered sample of matching issues. Use it whenever the answer "
        "is about how many / which ones / most recent rather than about the content of an issue. "
        "Examples: 'how many bugs are there', 'list all the stories', 'how many issues in total', "
        "'which issues are blocked', 'what was updated most recently', 'what issue did I just change', "
        "'how many issues are in the active sprint' (call query_sprints first to find the active "
        "sprint's id, then filter here by sprint_ids). "
        "Each returned issue includes its parent_key (the epic/parent issue it belongs to, or null if "
        "it has none) — use this, not search_knowledge_base, for 'which epic is X in' or 'what is the "
        "parent of X' questions: call this with issue_keys=[the issue in question] and read parent_key "
        "directly off the result. "
        "All filters are optional and combine with AND; omit them all to get every issue in scope."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "issue_types": {
                "type": "array", "items": {"type": "string"},
                "description": "Filter to these issue types. Known values: bug, story, task, epic, subtask.",
            },
            "issue_keys": {
                "type": "array", "items": {"type": "string"},
                "description": (
                    "Restrict current-state lookup to these exact issue keys. Use this after "
                    "search_knowledge_base identifies topic-relevant issues, so an unrelated global "
                    "issue is never mistaken for the answer to a topic-specific question."
                ),
            },
            "statuses": {
                "type": "array", "items": {"type": "string"},
                "description": "Filter to these statuses. Known values: done, in_progress, planned, blocked.",
            },
            "priorities": {
                "type": "array", "items": {"type": "string"},
                "description": "Filter to these priorities. Known values: low, medium, high, highest. "
                               "Only current values are known — see get_issue_history for past "
                               "priority changes.",
            },
            "sprint_ids": {
                "type": "array", "items": {"type": "integer"},
                "description": "Filter to issues in these sprints, by id — get ids from query_sprints "
                               "(e.g. its 'active' sprint) rather than guessing.",
            },
            "sprint_names": {
                "type": "array", "items": {"type": "string"},
                "description": "Filter to issues in these sprints, by name (e.g. 'AtlasCart Sprint 7').",
            },
            "created_after": {"type": "string", "description": "ISO 8601 datetime lower bound on creation time."},
            "created_before": {"type": "string", "description": "ISO 8601 datetime upper bound on creation time."},
            "updated_after": {"type": "string", "description": "ISO 8601 datetime lower bound on last-update time."},
            "updated_before": {"type": "string", "description": "ISO 8601 datetime upper bound on last-update time."},
            "order_by": {"type": "string", "enum": ["created_at", "updated_at"],
                         "description": "Which timestamp to order the returned sample by. Default updated_at."},
            "order": {"type": "string", "enum": ["asc", "desc"],
                      "description": "desc = newest first (use for 'most recent'); asc = oldest first. Default desc."},
            "limit": {"type": "integer", "description": "Max issues to return in the sample (the count is always exact). Default 20."},
        },
        "required": [],
    },
}

_SPRINT_QUERY_TOOL = {
    "name": "query_sprints",
    "description": (
        "Structured lookup over sprint METADATA — name, goal, dates, status, and velocity/points "
        "stats. Use it for questions about sprints as a whole, NOT the issues inside them. Examples: "
        "'how many sprints are there', 'which sprint is currently active', 'what's the goal of sprint "
        "N', 'list the completed sprints', 'how many points did we complete last sprint'. To find a "
        "sprint's GOAL by topic/meaning ('which sprint was about accessibility') prefer "
        "search_knowledge_base first — sprint goals are indexed there as chunk_type=sprint — then use "
        "this tool if you need the exact structured fields (dates, points) for a sprint you've already "
        "identified. To count/list the ISSUES inside a sprint, use query_issues with sprint_ids/"
        "sprint_names instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "statuses": {
                "type": "array", "items": {"type": "string"},
                "description": "Filter to these statuses. Known values: future, active, completed.",
            },
            "order_by": {"type": "string", "enum": ["start_date", "end_date"],
                         "description": "Which date to order the returned sample by. Default start_date."},
            "order": {"type": "string", "enum": ["asc", "desc"],
                      "description": "desc = latest first; asc = earliest first. Default desc."},
            "limit": {"type": "integer", "description": "Max sprints to return in the sample (the count is always exact). Default 20."},
        },
        "required": [],
    },
}

_ISSUE_HISTORY_TOOL = {
    "name": "get_issue_history",
    "description": (
        "Query the append-only CHANGE HISTORY of issues — who changed what, when, from what value to "
        "what value (including old/new title and description text). Use it for questions about "
        "transitions and edits, NOT current state. Examples: 'which issues were reopened' "
        "(reopened_only=true), 'what did I change in ATC-77 just now' (issue_keys=['ATC-77']), "
        "'what changed in the last hour' (since=<ISO timestamp>), 'who moved this to blocked'. "
        "For an issue's CURRENT status/type use query_issues instead; for the content/meaning of an "
        "issue use search_knowledge_base."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "issue_keys": {
                "type": "array", "items": {"type": "string"},
                "description": "Restrict to these issues, e.g. ['ATC-77'].",
            },
            "fields": {
                "type": "array", "items": {"type": "string"},
                "description": "Restrict to changes of these fields. Known values: status, title, "
                               "description, priority, assignee, sprint, storyPoints, issueType.",
            },
            "event_types": {
                "type": "array", "items": {"type": "string"},
                "description": "Restrict to these change kinds. Known values: field_change, "
                               "issue_created, comment_created, attachment_uploaded, link_created.",
            },
            "since": {"type": "string", "description": "ISO 8601 datetime — only changes at/after this time."},
            "until": {"type": "string", "description": "ISO 8601 datetime — only changes before this time."},
            "reopened_only": {
                "type": "boolean",
                "description": "true = only status changes that LEFT 'done' (an issue that was finished "
                               "and got reopened).",
            },
            "limit": {"type": "integer", "description": "Max change records to return (count is always exact). Default 20."},
        },
        "required": [],
    },
}

_ISSUE_COMMENTS_TOOL = {
    "name": "get_issue_comments",
    "description": (
        "Fetch the COMPLETE, unranked comment text for specific issue(s) you have already identified "
        "by key — not a top-K semantic match. Use this as a FALLBACK after search_knowledge_base: if "
        "search's top results don't contain the specific detail you need (a reason, an explanation, a "
        "decision) but you already know which issue it's in, call this with that issue's key to see "
        "every comment on it before concluding the information isn't there. Example: after finding "
        "which issue was reopened via get_issue_history, if you still need WHY, call this with that "
        "issue's key rather than guessing another search_knowledge_base query. Only abstain if BOTH "
        "search_knowledge_base and this (when you had a specific issue key to check) come up empty."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "issue_keys": {
                "type": "array", "items": {"type": "string"},
                "description": "Fetch every comment on these issues, e.g. ['ATC-30', 'ATC-68'].",
            },
        },
        "required": ["issue_keys"],
    },
}

_ISSUE_DETAILS_TOOL = {
    "name": "get_issue_details",
    "description": (
        "Fetch the COMPLETE, unranked title+description text for specific issue(s) you have already "
        "identified by key — the counterpart to get_issue_comments, but for the issue's OWN body, not "
        "its comments. Use this whenever a question asks what a specific, already-identified issue "
        "IS or SAYS (e.g. 'show me the details of ATC-43', 'what is ATC-43 about') and "
        "search_knowledge_base's top results don't already show that issue's own title/description. "
        "This matters even when search_knowledge_base found PLENTY of results for that key: an issue "
        "with many comments can have its own single title+description chunk rank BELOW several of "
        "those comments (ranking has no guarantee of surfacing the canonical record just because it "
        "exists), so a pile of comment hits about an issue is not proof its own description was "
        "actually retrieved — check for it explicitly. Only conclude an issue's own details aren't "
        "available after calling this with its key and getting nothing back."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "issue_keys": {
                "type": "array", "items": {"type": "string"},
                "description": "Fetch the title+description of these issues, e.g. ['ATC-43'].",
            },
        },
        "required": ["issue_keys"],
    },
}

_ISSUE_ATTACHMENTS_TOOL = {
    "name": "get_issue_attachments",
    "description": (
        "Fetch the COMPLETE, unranked parsed text of every attachment (PDF, spreadsheet, screenshot, "
        "log, etc.) on specific issue(s) you have already identified by key — the counterpart to "
        "get_issue_comments/get_issue_details, but for attachment content. Use this whenever a "
        "question asks about the content of a document/file/attachment on an already-identified issue "
        "(e.g. 'what SKU does ATC-46's attachment use', 'what does the log attached to ATC-43 show') "
        "and search_knowledge_base's results don't already contain the specific detail you need. This "
        "matters especially for an exact code, ID, or number you don't already know the wording of: "
        "a semantic/hybrid search query can only be as good as the words you guess, so a search that "
        "comes back empty is NOT proof the fact isn't there — call this with the issue's key to read "
        "its attachment text directly and completely before concluding the information is missing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "issue_keys": {
                "type": "array", "items": {"type": "string"},
                "description": "Fetch every attachment's parsed text on these issues, e.g. ['ATC-46'].",
            },
        },
        "required": ["issue_keys"],
    },
}

def _current_time_for_prompt() -> str:
    """Current UTC time, rounded down to the nearest 5 minutes.

    Second-precision defeats Anthropic prompt caching before it starts: a cached prefix is matched by
    exact byte content, so a system prompt that's never byte-identical twice can never hit. 5-minute
    granularity is deliberately chosen to match Anthropic's own default ephemeral cache TTL (5 min) —
    within one cache lifetime the prompt is guaranteed stable, so every call in that window (this
    conversation's next tool round, a different question, even a different user) can share one cache
    entry instead of writing a fresh one. The accuracy cost is at most 5 minutes of drift on relative-
    time questions ("just now", "past 3 months") — negligible for what those resolve into.
    """
    now = datetime.now(timezone.utc)
    rounded = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    return rounded.strftime("%Y-%m-%dT%H:%M:%SZ")


# Built per-request, not a module constant: history questions are anchored to "now" ("what did I
# change 10 minutes ago?", "in the past 3 months?"), and a model with no clock can only guess what
# `since` should be. The timestamp is the only dynamic part; everything else is static routing rules.
def _system_prompt(now_utc: str) -> str:
    return f"""You are a knowledge assistant for a Jira-style engineering workspace. Answer \
questions using ONLY information you retrieve with the tools — never answer from your own knowledge \
or assumptions about what a typical system might do.

The current UTC time is {now_utc}. Use it to resolve relative time in questions ("just now", "in the \
last hour", "past 3 months") into concrete ISO timestamps for tool filters.

You have seven tools, and choosing the right one is the most important decision you make:
- search_knowledge_base — semantic/keyword search over issue, comment, SPRINT GOAL, AND ATTACHMENT \
text. Use it for questions about content or meaning: "why did X break", "how did we fix Y", "what \
does issue Z say", "which sprint's goal was about accessibility", "what does the log/report/spec \
attached to X show". A sprint's goal is real prose indexed here (chunk_type=sprint) — trust what this \
tool returns over anything that merely sounds similar in an issue's own text. Attachments (PDFs, \
markdown, spreadsheets, CSVs, screenshots) are parsed to text and indexed here too (chunk_type= \
attachment) — a citation with this chunk type is the content of an uploaded file, not the issue's own \
description; don't assume a detail must be in a comment just because it isn't in the issue body.
- query_issues — structured lookup over issue METADATA (type, status, priority, sprint, parent_key, \
timestamps), returning exact counts. Use it for counting, listing, filtering, and recency of CURRENT \
state: "how many bugs", "list the stories", "which are blocked", "what was updated most recently", \
"how many \
issues are in sprint N" (filter by sprint_ids/sprint_names), "which epic does X belong to" / "what is \
the parent of X" (issue_keys=[X], read parent_key off the result — do not guess this from \
search_knowledge_base), "how many issues are highest priority" / "list the high priority bugs" \
(priorities=['highest'] / ['high'] — known priority values are low, medium, high, highest; ALWAYS pass \
the priorities filter when the question names a specific priority level, never omit it and read the \
unfiltered total instead). search_knowledge_base returns only its top few best guesses, so it can NEVER \
answer a "how many" or "list all" question correctly — those need query_issues.
- query_sprints — structured lookup over sprint METADATA (dates, status, velocity/points), NOT the \
issues inside a sprint. Use it for "how many sprints", "which sprint is active", "what's sprint N's \
velocity". Combine with query_issues (via sprint_ids) for "how many issues are in the active sprint."
- get_issue_history — the append-only change log (who changed what, when, old value → new value, \
including title/description and sprint-reassignment edits). Use it for questions about CHANGES and \
transitions: "which issues were reopened", "what did I just change", "what happened to ATC-77 this \
week", "who moved X to blocked", "when did this move to a different sprint". Current state is \
query_issues/query_sprints; how it GOT there is get_issue_history.
- get_issue_comments — the COMPLETE, unranked comment text for issue(s) you have already identified \
by key. This is a FALLBACK, not a first move: reach for it only after search_knowledge_base's top \
results don't contain a specific detail (a reason, an explanation) you know must exist on a \
particular issue — e.g. you know via get_issue_history that ATC-30 was reopened, but WHY isn't in \
what search_knowledge_base returned. Never treat a weak search_knowledge_base result as the final \
word on a specific, already-identified issue — check its full comment history with this tool before \
concluding the information isn't there.
- get_issue_details — the COMPLETE, unranked title+description for issue(s) you have already \
identified by key — the get_issue_comments of an issue's OWN body, not its comments. Use it whenever \
a question asks what a specific issue IS or SAYS (e.g. "show me the details of ATC-43", "what is \
ATC-43 about") and search_knowledge_base's results don't already show that issue's own title/ \
description text verbatim. Critically, this can be true even when search_knowledge_base returned \
several hits for that key: those hits are commonly ALL comments, because an issue with many comments \
can have its own single title+description chunk rank below them — a pile of comment hits is not proof \
the issue's own description was retrieved. Never answer a "what is/says" question about a named issue \
from comments alone, or abstain claiming its details are unavailable, without calling this first.
- get_issue_attachments — the COMPLETE, unranked parsed text of every attachment on issue(s) you have \
already identified by key — the get_issue_comments/get_issue_details of attachment content (PDFs, \
spreadsheets, screenshots, logs). This matters MOST for an exact fact you don't already know the \
wording of (a SKU, an ID, a code in a table): search_knowledge_base's semantic/hybrid ranking can only \
be as good as the words you guess, so it can genuinely miss an exact value that's sitting right there \
in an attachment — an empty or weak search_knowledge_base result for a named issue's attachment is NOT \
proof the fact isn't there. If a question names a specific issue and asks about its attachment/ \
document/file content, and search_knowledge_base didn't already surface the specific value you need, \
call this with that issue's key before abstaining.

Combine tools when a question spans them (e.g. "how many bugs are about checkout": query_issues for \
the type filter + search_knowledge_base for the topic; "what did I just edit and what does that \
issue say": get_issue_history then search_knowledge_base; "how many issues are in the active sprint": \
query_sprints to find the active sprint's id, then query_issues filtered to it; "why were issues X \
and Y reopened": get_issue_history to find them, then get_issue_comments on their keys for the reason \
if the reopening's own change record doesn't already state it; "show me the details of ATC-43": \
search_knowledge_base first, then get_issue_details on ATC-43 to confirm its own title/description — \
not just comments about it — actually came back).
For comparisons such as "how many issues including Sprint 7 versus without Sprint 7", call \
query_sprints first to resolve Sprint 7's exact id, then call query_issues once with no sprint filter \
for the inclusive total and once with that sprint_id. The without-Sprint-7 count is the exact total \
minus the exact Sprint-7 count; never estimate it from semantic-search results.
For a topic-specific current-state question such as "what is blocking payments", first use \
search_knowledge_base to identify payment-related issue keys, then call query_issues with BOTH \
issue_keys=<those keys> and statuses=["blocked"]. A globally blocked but topic-irrelevant issue is \
not an answer. Never claim that one issue blocks another merely because both contain similar words.

Ground rules:
1. Always use a tool before answering. Reformulate and try again if the first result is weak, \
off-topic, or only partially answers the question — you may call tools up to a few times. For a \
question about a specific, already-identified issue (by key) where search_knowledge_base's results \
seem incomplete, call get_issue_comments and/or get_issue_details on that issue before concluding the \
answer isn't there (get_issue_details for what the issue itself says, get_issue_comments for \
discussion/reasons) — only abstain once search_knowledge_base and every applicable one of those two \
have come up empty.
2. For content answers, cite every claim with the issue key it came from, e.g. "(PROJ-42)". Never \
state something you cannot trace to a tool result. For count/list answers, report the exact numbers \
the tools returned — do not round or estimate.
In particular, do not add a "likely" mechanism, a general claim about what a log field or network \
component would normally do, or any other causal explanation unless that exact explanation appears \
in a tool result. Evidence such as "two request IDs and retry_count=0 ruled out a proxy retry" supports \
that conclusion only; it does NOT support inventing what a hypothetical proxy retry would have looked \
like. Evidence such as "only one payment was captured" does NOT support guessing why the payment \
provider avoided a second charge. Omit an unasked-for explanation rather than filling the gap.
3. If, after using the tools, you still cannot find information that answers the question, respond \
with EXACTLY this sentence and nothing else: "{ABSTENTION_PHRASE}" \
Do not guess, speculate, or answer partially — an honest "I don't know" is correct behavior, not a \
failure. (But note: a tool returning a count of 0 IS a real answer — "there are none" — not a reason \
to abstain.)
4. Distinguish current state from historical state if the retrieved content shows the situation \
changed over time — answer based on what is current unless the question asks about history.
5. A key appearing in the SAME chunk as another key does not mean they share the relationship you're \
about to state — citing a real key is not the same as correctly characterizing what it IS to the \
issue in question. This matters most for parent/child or split/subtask claims: retrieved text may \
mention several issue keys together while explicitly distinguishing their relationship (e.g. "split \
the follow-up into X, Y, and Z; W stays separate" — W is related, but is NOT one of the split-off \
items). Read that distinction precisely rather than bucketing every nearby key into one list. When a \
question is specifically about subtasks/children of an issue, prefer query_issues with issue_keys set \
to the candidate keys and check issue_type=subtask (or the issue's own get_issue_details text, which \
lists real subtasks under an explicit "Subtasks" heading) over inferring the relationship from which \
keys merely co-occur in a comment.
"""


def _initial_tool_choice(question: str) -> str:
    """Deterministically select the first evidence source for common intent classes.

    The model still supplies the actual filters and may combine more tools after the first result,
    but it cannot route an obvious exact-count question to top-K semantic search. This is deliberately
    conservative: ambiguous/topic questions start with semantic retrieval, while metadata/history
    phrasings with strong lexical signals start on their structured source of truth.
    """
    q = " ".join(question.lower().split())

    if re.search(
        r"\b(reopen(?:ed|ing)?|change history|changed? (?:in|on|to|by)|who (?:changed|moved)|"
        r"when did|just (?:edit|change)|previous(?:ly)? (?:status|state))\b",
        q,
    ):
        return _ISSUE_HISTORY_TOOL["name"]

    issue_terms = bool(re.search(r"\b(issues?|bugs?|stories|tasks?|epics?|subtasks?)\b", q))
    # Users commonly type "sprint7" without a space; treat it the same as "Sprint 7".
    sprint_terms = bool(re.search(r"\bsprints?\b|\bsprint\s*\d+\b", q))
    structured_action = bool(
        re.search(r"\b(how many|number of|count|list|which|active|current|latest|recent|velocity|points?)\b", q)
    )

    # Resolve every sprint reference first, including "Sprint 7": query_issues uses exact sprint ids/
    # names, so guessing a short display name can silently return 0 for "AtlasCart Sprint 7".
    if sprint_terms and structured_action:
        return _SPRINT_QUERY_TOOL["name"]

    if issue_terms and structured_action:
        return _ISSUE_QUERY_TOOL["name"]

    # "Which issues are blocked?" is structured; "what is blocking payments?" is topic-specific
    # and intentionally falls through to semantic search first.
    if re.search(r"\b(which|list|how many)\b.*\b(blocked|planned|done|in progress)\b", q):
        return _ISSUE_QUERY_TOOL["name"]

    return _SEARCH_TOOL["name"]


@dataclass(frozen=True, slots=True)
class ClaimMismatch:
    """One post-generation verification failure: a specific claim in the answer contradicted by a
    structured fact (see Case Study 15 in docs/RAG_ACCURACY_CASE_STUDIES.md and
    `CragAgent._post_generation_verifiers`). `detail` is the exact, human-readable correction fed back
    to the model — the verifier that found the mismatch owns the wording since it knows the specific
    field/relationship it checked; the generic correction-message builder just joins these verbatim."""

    issue_key: str
    detail: str


@dataclass(slots=True)
class AgentAnswer:
    text: str
    retrieval_rounds: int
    queries_used: List[str]
    citations: List[RetrievedChunk] = field(default_factory=list)
    # Human-readable results of structured tool calls (query_issues/query_sprints/get_issue_history)
    # this turn actually made — the same `result_text` already handed to the model as each tool's
    # result, just also kept here. Exists specifically so an eval harness scoring "is this answer
    # grounded in what was retrieved" (e.g. RAGAS faithfulness) has somewhere to look up a claim like
    # "ATC-77 is blocked" — `citations` alone only ever covers search_knowledge_base/get_issue_*
    # prose, never a structured tool's own facts, so a claim sourced purely from query_issues had no
    # matching context to be scored against. See docs/RAG_ACCURACY_CASE_STUDIES.md Case Study 23.
    structured_evidence: List[str] = field(default_factory=list)
    abstained: bool = False
    usage: Usage = field(default_factory=Usage)
    estimated_cost_usd: float = 0.0
    # Wall-clock seconds this specific request spent in each stage ("llm", "retrieval") — see
    # `_stage_seconds`/`_add_stage_seconds` above. Absent keys mean that stage wasn't hit at all
    # (e.g. an abstention that never called a tool has no "retrieval" key), not zero seconds.
    stage_seconds: Dict[str, float] = field(default_factory=dict)


class CragAgent:
    def __init__(
        self, llm: LLMClient, retrieval: RetrievalClient, max_iterations: int, top_k: int, model: str = "",
        faithfulness_check_enabled: bool = False,
    ):
        self._llm = llm
        self._retrieval = retrieval
        self._max_iterations = max_iterations
        self._top_k = top_k
        # Used only for $-cost estimation (app/llm/pricing.py) — the LLM client already knows its own
        # model for the actual API call; the agent needs the name separately just to price tokens.
        self._model = model
        self._faithfulness_check_enabled = faithfulness_check_enabled
        # Rebuilt at the start of every ask() with the current UTC time; this default only exists so
        # a direct _timed_llm_call in a test doesn't explode.
        self._system = _system_prompt("1970-01-01T00:00:00Z")
        # Registry of deterministic post-generation verifiers (see Case Studies 15/17 in
        # docs/RAG_ACCURACY_CASE_STUDIES.md). Additional relationship verifiers can be registered
        # here without adding branches to the main ask loop. `_check_faithfulness` is intentionally
        # NOT in this list (see `_apply_verification`) — it needs the retrieved context itself, which
        # this list's uniform (text, space_ids, subject_keys) signature doesn't carry, unlike these
        # two, which re-query their own source of truth instead of needing it passed in.
        self._post_generation_verifiers = [
            self._check_subtask_claims,
            self._check_current_state_claims,
        ]

    async def ask(
        self, question: str, space_ids: Sequence[int], history: Sequence[dict] | None = None,
        on_stage: Optional[Callable[[str], None]] = None,
    ) -> AgentAnswer:
        # `on_stage`, if given, is called synchronously at existing checkpoints this loop already
        # passes through — tool choice, each tool dispatch, verification — with a human-readable label.
        # Purely additive instrumentation for POST /ask/stream's SSE progress events: it does not
        # change what the loop does or when, only announces it. A no-op default keeps every other
        # caller (the plain POST /ask route, every eval script, every test) completely unaffected.
        stage = on_stage or (lambda _label: None)
        # Snapshot "now" once per ask so every LLM turn in this conversation sees the same clock —
        # needed to resolve relative time ("just now", "past 3 months") into concrete tool filters.
        self._system = _system_prompt(_current_time_for_prompt())
        # `history` is prior turns' FINAL text only ({"role": "user"|"assistant", "content": str}) —
        # deliberately not the intermediate tool_use/tool_result exchanges a completed turn produced
        # internally. Anthropic's API requires every tool_use to be followed by a matching tool_result
        # with the same id in the very next message; persisting those across turns would mean carrying
        # that pairing (and the full retrieved chunk text) forward forever. The model doesn't need to
        # re-see *how* it found last turn's answer, only *what* the answer was — same reason a human
        # chat transcript doesn't replay someone's search history, just what they said.
        messages: List[dict] = [dict(m) for m in (history or [])]
        messages.append({"role": "user", "content": question})
        queries_used: List[str] = []
        citations: List[RetrievedChunk] = []
        structured_evidence: List[str] = []
        seen_citation_ids: set = set()
        evidence_issue_keys: set[str] = set()
        subject_issue_keys: set[str] = {k.upper() for k in _ISSUE_KEY_RE.findall(question)}
        semantic_used = False
        structured_used = False
        retrieval_rounds = 0
        total_usage = Usage()
        initial_tool_choice = _initial_tool_choice(question)
        stage_seconds: Dict[str, float] = {}
        stage_seconds_token = _stage_seconds.set(stage_seconds)

        try:
            for iteration in range(self._max_iterations + 1):
                required_tool = initial_tool_choice if retrieval_rounds == 0 else None
                stage(
                    "choosing the right data source" if iteration == 0
                    else "reviewing results and deciding whether more retrieval is needed"
                )
                turn = await self._timed_llm_call(
                    messages,
                    [_SEARCH_TOOL, _ISSUE_QUERY_TOOL, _SPRINT_QUERY_TOOL, _ISSUE_HISTORY_TOOL,
                     _ISSUE_COMMENTS_TOOL, _ISSUE_DETAILS_TOOL, _ISSUE_ATTACHMENTS_TOOL],
                    tool_choice=required_tool,
                )
                total_usage = total_usage + turn.usage
                messages.append(turn.assistant_message)

                if turn.stop_reason == "end_turn":
                    text = (
                        turn.text
                        if retrieval_rounds > 0 and turn.text.strip()
                        else ABSTENTION_PHRASE
                    )
                    stage("verifying and finalizing the answer")
                    text, selected_citations, verify_usage = await self._apply_verification(
                        text, messages, citations, evidence_issue_keys, semantic_used,
                        structured_used, space_ids, subject_issue_keys, structured_evidence,
                    )
                    total_usage = total_usage + verify_usage
                    return AgentAnswer(
                        text=text,
                        retrieval_rounds=retrieval_rounds,
                        queries_used=queries_used,
                        citations=selected_citations,
                        structured_evidence=structured_evidence,
                        abstained=ABSTENTION_PHRASE in text,
                        usage=total_usage,
                        estimated_cost_usd=self._cost(total_usage),
                        stage_seconds=dict(stage_seconds),
                    )

                if iteration >= self._max_iterations:
                    # Hit the retry cap mid tool-call: force a final answer instead of looping
                    # forever. Same "budget the agent, don't let it run unbounded" principle as a
                    # max_iterations cap on any tool-use loop — corrective retrieval must still be a
                    # bounded-cost operation.
                    messages.append(self._forced_stop_message(turn))
                    final = await self._timed_llm_call(messages, [])
                    total_usage = total_usage + final.usage
                    messages.append(final.assistant_message)
                    # Passing tools=[] means a real model cannot legally emit tool_use here — but
                    # provider behavior with an empty tool list isn't a guarantee we control
                    # (especially across OpenAI-compatible servers), so don't trust `final.text`
                    # blindly if it ignored that anyway; that would silently return blank/garbage
                    # instead of a safe answer, defeating the whole point of the hard cap.
                    text = final.text if final.stop_reason == "end_turn" and final.text else ABSTENTION_PHRASE
                    # Verification must still run here, not just on the natural end_turn path above:
                    # a thorough question that consumes the ENTIRE tool-calling budget always lands on
                    # this branch, and skipping verification for exactly the answers that took the most
                    # research to produce would be backwards (see Case Study 18 / eval Finding 2).
                    stage("verifying and finalizing the answer")
                    text, selected_citations, verify_usage = await self._apply_verification(
                        text, messages, citations, evidence_issue_keys, semantic_used,
                        structured_used, space_ids, subject_issue_keys, structured_evidence,
                    )
                    total_usage = total_usage + verify_usage
                    return AgentAnswer(
                        text=text,
                        retrieval_rounds=retrieval_rounds,
                        queries_used=queries_used,
                        citations=selected_citations,
                        structured_evidence=structured_evidence,
                        abstained=ABSTENTION_PHRASE in text,
                        usage=total_usage,
                        estimated_cost_usd=self._cost(total_usage),
                        stage_seconds=dict(stage_seconds),
                    )

                tool_results = []
                for call in turn.tool_calls:
                    if required_tool and call.name != required_tool:
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": call.id,
                                "content": (
                                    f"Wrong tool for this question. Call {required_tool} first; "
                                    f"{call.name} was not executed."
                                ),
                                "is_error": True,
                            }
                        )
                        continue
                    # Dispatch on tool name. Either tool has `space_ids` injected here from the
                    # authenticated caller, never taken from the model — an LLM must never be trusted
                    # with an authorization parameter (see module docstring). The model only ever
                    # controls the query/filter shape, never which spaces it may read.
                    stage(_TOOL_STAGE_LABELS.get(call.name, f"calling {call.name}"))
                    if call.name == _ISSUE_QUERY_TOOL["name"]:
                        result_text, result_keys = await self._run_issue_query(call.input, space_ids)
                        evidence_issue_keys.update(result_keys)
                        structured_used = True
                        structured_evidence.append(result_text)
                        retrieval_rounds += 1
                    elif call.name == _SPRINT_QUERY_TOOL["name"]:
                        result_text = await self._run_sprint_query(call.input, space_ids)
                        structured_used = True
                        structured_evidence.append(result_text)
                        retrieval_rounds += 1
                    elif call.name == _ISSUE_HISTORY_TOOL["name"]:
                        result_text, result_keys = await self._run_issue_history(call.input, space_ids)
                        evidence_issue_keys.update(result_keys)
                        structured_used = True
                        structured_evidence.append(result_text)
                        retrieval_rounds += 1
                    elif call.name == _ISSUE_COMMENTS_TOOL["name"]:
                        issue_keys_arg = [k for k in call.input.get("issue_keys", []) if isinstance(k, str)]
                        hits = await self._run_issue_comments(issue_keys_arg, space_ids)
                        # Same grounding contract as search_knowledge_base: this hands the model raw
                        # comment text, so the citation-required rule below must apply to it too — a
                        # structured tool's exact counts are self-grounding, but quoted prose is not.
                        semantic_used = True
                        retrieval_rounds += 1
                        for h in hits:
                            evidence_issue_keys.add(h.issue_key.upper())
                            evidence_issue_keys.update(k.upper() for k in _ISSUE_KEY_RE.findall(h.content))
                            if h.id not in seen_citation_ids:
                                seen_citation_ids.add(h.id)
                                citations.append(h)
                        result_text = _format_issue_comments(hits, issue_keys_arg)
                    elif call.name == _ISSUE_DETAILS_TOOL["name"]:
                        issue_keys_arg = [k for k in call.input.get("issue_keys", []) if isinstance(k, str)]
                        hits = await self._run_issue_details(issue_keys_arg, space_ids)
                        # Same grounding contract as get_issue_comments/search_knowledge_base: this
                        # hands the model raw title+description text, so the citation-required rule
                        # below must apply to it too.
                        semantic_used = True
                        retrieval_rounds += 1
                        for h in hits:
                            evidence_issue_keys.add(h.issue_key.upper())
                            evidence_issue_keys.update(k.upper() for k in _ISSUE_KEY_RE.findall(h.content))
                            if h.id not in seen_citation_ids:
                                seen_citation_ids.add(h.id)
                                citations.append(h)
                        result_text = _format_issue_details(hits, issue_keys_arg)
                    elif call.name == _ISSUE_ATTACHMENTS_TOOL["name"]:
                        issue_keys_arg = [k for k in call.input.get("issue_keys", []) if isinstance(k, str)]
                        hits = await self._run_issue_attachments(issue_keys_arg, space_ids)
                        # Same grounding contract as get_issue_comments/get_issue_details: this hands
                        # the model raw attachment text, so the citation-required rule below must
                        # apply to it too.
                        semantic_used = True
                        retrieval_rounds += 1
                        for h in hits:
                            evidence_issue_keys.add(h.issue_key.upper())
                            evidence_issue_keys.update(k.upper() for k in _ISSUE_KEY_RE.findall(h.content))
                            if h.id not in seen_citation_ids:
                                seen_citation_ids.add(h.id)
                                citations.append(h)
                        result_text = _format_issue_attachments(hits, issue_keys_arg)
                    elif call.name == _SEARCH_TOOL["name"]:
                        query = call.input.get("query", "")
                        mode = call.input.get("mode", "hybrid")
                        queries_used.append(query)
                        hits = await self._timed_retrieval_call(query, space_ids, mode)
                        semantic_used = True
                        retrieval_rounds += 1
                        for h in hits:
                            evidence_issue_keys.add(h.issue_key.upper())
                            # A retrieved chunk's own prose can legitimately mention OTHER issue
                            # keys — e.g. a comment on ATC-43 saying "split into ATC-44, ATC-45" as
                            # backstory. Those keys were never independently retrieved this turn, so
                            # without this they read as "unsupported" and _ground_answer rejects an
                            # answer that faithfully quotes text the model was actually handed. If the
                            # model saw the key verbatim in retrieved content, it isn't hallucinated.
                            evidence_issue_keys.update(k.upper() for k in _ISSUE_KEY_RE.findall(h.content))
                            if h.id not in seen_citation_ids:
                                seen_citation_ids.add(h.id)
                                citations.append(h)
                        result_text = _format_hits(hits)
                    else:
                        result_text = f"Unknown tool {call.name!r}; it was not executed."
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": call.id, "content": result_text}
                    )
                messages.append({"role": "user", "content": tool_results})

            # Defensive fallback — the loop above normally returns before reaching this point.
            return AgentAnswer(text=ABSTENTION_PHRASE, retrieval_rounds=retrieval_rounds,
                                queries_used=queries_used, citations=citations,
                                structured_evidence=structured_evidence, abstained=True,
                                usage=total_usage, estimated_cost_usd=self._cost(total_usage),
                                stage_seconds=dict(stage_seconds))
        finally:
            # In `finally`, not duplicated at every return branch, so every exit path — grounded
            # answer, honest abstention, forced stop at the iteration cap — contributes to the same
            # histogram. This is the metric that answers "is corrective retrieval actually firing in
            # practice, and how often" without grepping logs.
            RETRIEVAL_ROUNDS.observe(retrieval_rounds)
            _stage_seconds.reset(stage_seconds_token)

    async def _timed_llm_call(
        self, messages: List[dict], tools: List[dict], tool_choice: str | None = None
    ):
        start = time.perf_counter()
        try:
            return await self._llm.next_turn(
                self._system, messages, tools, tool_choice=tool_choice
            )
        finally:
            elapsed = time.perf_counter() - start
            LLM_CALL_SECONDS.observe(elapsed)
            _add_stage_seconds("llm", elapsed)

    async def _timed_retrieval_call(self, query: str, space_ids: Sequence[int], mode: str):
        start = time.perf_counter()
        try:
            return await self._retrieval.search(query, space_ids, self._top_k, mode)
        finally:
            elapsed = time.perf_counter() - start
            RETRIEVAL_CALL_SECONDS.observe(elapsed)
            _add_stage_seconds("retrieval", elapsed)

    async def _run_issue_query(
        self, model_input: dict, space_ids: Sequence[int]
    ) -> tuple[str, set[str]]:
        # Pass only the whitelisted, model-controllable filter fields downstream — never let the model
        # smuggle in space_ids or arbitrary keys. The retrieval client injects space_ids itself.
        allowed = ("issue_keys", "issue_types", "statuses", "priorities", "sprint_ids", "sprint_names",
                   "created_after", "created_before", "updated_after", "updated_before", "order_by",
                   "order", "limit")
        filters = {k: model_input[k] for k in allowed if k in model_input and model_input[k] is not None}
        start = time.perf_counter()
        try:
            result = await self._retrieval.query_issues(space_ids, filters)
        finally:
            elapsed = time.perf_counter() - start
            RETRIEVAL_CALL_SECONDS.observe(elapsed)
            _add_stage_seconds("retrieval", elapsed)
        keys = {
            str(issue.get("issue_key")).upper()
            for issue in result.issues
            if issue.get("issue_key")
        }
        # parent_key is now rendered directly in the tool result text (see _format_issue_query) so
        # the model can answer "which epic is X in" from this call alone — a cited parent_key is
        # grounded evidence exactly like an issue_key that came back from this same call.
        keys.update(
            str(issue.get("parent_key")).upper()
            for issue in result.issues
            if issue.get("parent_key")
        )
        return _format_issue_query(result, filters), keys

    async def _run_sprint_query(self, model_input: dict, space_ids: Sequence[int]) -> str:
        allowed = ("statuses", "order_by", "order", "limit")
        filters = {k: model_input[k] for k in allowed if k in model_input and model_input[k] is not None}
        start = time.perf_counter()
        try:
            result = await self._retrieval.query_sprints(space_ids, filters)
        finally:
            elapsed = time.perf_counter() - start
            RETRIEVAL_CALL_SECONDS.observe(elapsed)
            _add_stage_seconds("retrieval", elapsed)
        return _format_sprint_query(result, filters)

    async def _run_issue_history(
        self, model_input: dict, space_ids: Sequence[int]
    ) -> tuple[str, set[str]]:
        # Same whitelist discipline as _run_issue_query: only these fields ever reach downstream,
        # space_ids is injected by the client from the authenticated caller.
        allowed = ("issue_keys", "fields", "event_types", "since", "until", "reopened_only", "limit")
        filters = {k: model_input[k] for k in allowed if k in model_input and model_input[k] is not None}
        start = time.perf_counter()
        try:
            result = await self._retrieval.query_issue_history(space_ids, filters)
        finally:
            elapsed = time.perf_counter() - start
            RETRIEVAL_CALL_SECONDS.observe(elapsed)
            _add_stage_seconds("retrieval", elapsed)
        keys = {
            str(change.get("issue_key")).upper()
            for change in result.changes
            if change.get("issue_key")
        }
        return _format_issue_history(result, filters), keys

    async def _run_issue_comments(
        self, issue_keys: Sequence[str], space_ids: Sequence[int]
    ) -> List[RetrievedChunk]:
        """Complete, unranked comment fetch — the plan-B fallback to search_knowledge_base. Returned
        as RetrievedChunk so it flows through the exact same citation/grounding path a search hit
        would (see the dispatch site): the model still must cite what it quotes, and the UI still
        gets a real, inspectable source, not just a text blob."""
        if not issue_keys:
            return []
        start = time.perf_counter()
        try:
            result = await self._retrieval.get_issue_comments(space_ids, issue_keys)
        finally:
            elapsed = time.perf_counter() - start
            RETRIEVAL_CALL_SECONDS.observe(elapsed)
            _add_stage_seconds("retrieval", elapsed)
        return [
            RetrievedChunk(
                id=f"comment:{c.get('source_id')}",
                chunk_type="comment",
                issue_id=c.get("issue_id", 0),
                issue_key=str(c.get("issue_key", "")),
                source_id=c.get("source_id", 0),
                content=c.get("content", ""),
                score=0.0,
                retrievers=["issue_comments"],
            )
            for c in result.comments
        ]

    async def _run_issue_details(
        self, issue_keys: Sequence[str], space_ids: Sequence[int]
    ) -> List[RetrievedChunk]:
        """Complete, unranked issue-body fetch — the get_issue_comments sibling, over an issue's own
        title+description instead of its comments. Returned as RetrievedChunk for the same reason:
        it flows through the exact same citation/grounding path a search hit would (see the dispatch
        site) — the model still must cite what it quotes, and the UI still gets a real, inspectable
        source, not just a text blob."""
        if not issue_keys:
            return []
        start = time.perf_counter()
        try:
            result = await self._retrieval.get_issue_details(space_ids, issue_keys)
        finally:
            elapsed = time.perf_counter() - start
            RETRIEVAL_CALL_SECONDS.observe(elapsed)
            _add_stage_seconds("retrieval", elapsed)
        return [
            RetrievedChunk(
                id=f"issue_details:{d.get('source_id')}",
                chunk_type="issue",
                issue_id=d.get("issue_id", 0),
                issue_key=str(d.get("issue_key", "")),
                source_id=d.get("source_id", 0),
                content=d.get("content", ""),
                score=0.0,
                retrievers=["issue_details"],
            )
            for d in result.details
        ]

    async def _run_issue_attachments(
        self, issue_keys: Sequence[str], space_ids: Sequence[int]
    ) -> List[RetrievedChunk]:
        """Complete, unranked attachment-text fetch — the get_issue_comments/get_issue_details
        sibling, over parsed attachment content. Returned as RetrievedChunk for the same reason: it
        flows through the exact same citation/grounding path a search hit would (see the dispatch
        site) — the model still must cite what it quotes, and the UI still gets a real, inspectable
        source, not just a text blob."""
        if not issue_keys:
            return []
        start = time.perf_counter()
        try:
            result = await self._retrieval.get_issue_attachments(space_ids, issue_keys)
        finally:
            elapsed = time.perf_counter() - start
            RETRIEVAL_CALL_SECONDS.observe(elapsed)
            _add_stage_seconds("retrieval", elapsed)
        return [
            RetrievedChunk(
                id=f"issue_attachments:{a.get('source_id')}",
                chunk_type="attachment",
                issue_id=a.get("issue_id", 0),
                issue_key=str(a.get("issue_key", "")),
                source_id=a.get("source_id", 0),
                content=a.get("content", ""),
                score=0.0,
                retrievers=["issue_attachments"],
                page_number=a.get("page_number"),
                provenance=a.get("provenance") or {},
            )
            for a in result.attachments
        ]

    async def _apply_verification(
        self,
        text: str,
        messages: List[dict],
        citations: List[RetrievedChunk],
        evidence_issue_keys: set[str],
        semantic_used: bool,
        structured_used: bool,
        space_ids: Sequence[int],
        subject_issue_keys: set[str],
        structured_evidence: List[str] = (),
    ) -> Tuple[str, List[RetrievedChunk], Usage]:
        """Ground the answer, then run every registered post-generation verifier exactly once.

        Deliberately independent of the main tool-calling loop's iteration budget: the correction
        round (if any mismatch is found) is a single direct LLM call issued here, not a `continue`
        back into the shared iteration counter. Previously the retry was implemented by looping back
        into the same `for iteration in range(...)` used for the model's own tool-calling, gated by
        `iteration < max_iterations + 1` so a reserved slot was always left over — but that reserved
        slot itself could be consumed by the model's OWN research when it used the full tool-calling
        budget, silently skipping verification for exactly the answers that took the most retrieval
        to produce (eval Finding 2). Calling this from both the natural end_turn path and the
        forced-stop path guarantees the one verification attempt always has a slot, independent of
        how many rounds preceded it. No tools are offered on the correction call: a wrong claim is a
        wording fix over evidence already retrieved, not a reason to do more research.
        """
        text, selected_citations = _ground_answer(
            text, citations, evidence_issue_keys, semantic_used, structured_used
        )
        usage = Usage()
        if ABSTENTION_PHRASE in text:
            return text, selected_citations, usage

        mismatches: List[ClaimMismatch] = []
        for verifier in self._post_generation_verifiers:
            found, verifier_usage = await verifier(text, space_ids, subject_issue_keys)
            mismatches.extend(found)
            usage = usage + verifier_usage

        # Runtime faithfulness guardrail (off by default — see config.py's faithfulness_check_enabled
        # docstring for the cost tradeoff). Separate from the loop above: this needs the actual
        # retrieved context passed in, not a re-query of a specific structured relationship, so it
        # doesn't fit the uniform (text, space_ids, subject_keys) verifier signature the other two use.
        if self._faithfulness_check_enabled:
            found, faith_usage = await self._check_faithfulness(
                text, selected_citations, structured_evidence
            )
            mismatches.extend(found)
            usage = usage + faith_usage

        if not mismatches:
            return text, selected_citations, usage

        messages.append(_verification_correction_message(mismatches))
        corrected = await self._timed_llm_call(messages, [])
        usage = usage + corrected.usage
        corrected_text = (
            corrected.text if corrected.stop_reason == "end_turn" and corrected.text else ABSTENTION_PHRASE
        )
        corrected_text, selected_citations = _ground_answer(
            corrected_text, citations, evidence_issue_keys, semantic_used, structured_used
        )
        return corrected_text, selected_citations, usage

    async def _check_faithfulness(
        self, text: str, citations: List[RetrievedChunk], structured_evidence: List[str]
    ) -> Tuple[List[ClaimMismatch], Usage]:
        """Runtime faithfulness guardrail: ask a fast, focused check whether THIS answer's claims are
        actually supported by what was retrieved for THIS request — the online counterpart to RAGAS's
        faithfulness metric (eval/ragas_eval.py), which only ever measures this offline, after the
        fact, against a fixed golden set. A live hallucination in production gets no RAGAS score at
        all; this is the check that can actually catch one before the caller sees it.

        Same "check, then correct once via the shared retry" shape as
        `_check_subtask_claims`/`_check_current_state_claims`, but doesn't fit their uniform
        (text, space_ids, subject_keys) signature — this one needs the retrieved context itself
        (citations' text + structured tool results), not a re-query of one specific relationship.
        """
        context = "\n\n".join([c.content for c in citations] + list(structured_evidence))
        if not context.strip():
            # Nothing to check faithfulness against — an abstention or a pure-reasoning answer with
            # no retrieved evidence at all (already filtered out by the ABSTENTION_PHRASE check in
            # _apply_verification for the abstention case; this covers the rarer non-abstaining case
            # with truly empty context).
            return [], Usage()

        check_system = (
            "You are a strict fact-checking assistant. You only ever answer in the exact format "
            "requested."
        )
        check_user = (
            "Context (retrieved evidence):\n"
            f'"""\n{context}\n"""\n\n'
            "Answer to check:\n"
            f'"""\n{text}\n"""\n\n'
            "Does the answer contain any factual claim that is NOT supported by the context above? "
            "Ignore phrasing/style differences — only flag claims the context does not actually "
            "back up. If every claim is supported, respond with exactly: SUPPORTED\n"
            "Otherwise respond with exactly one line per unsupported claim, each starting with "
            '"UNSUPPORTED: " followed by the specific claim.'
        )
        start = time.perf_counter()
        try:
            check = await self._llm.next_turn(
                check_system, [{"role": "user", "content": check_user}], [],
            )
        finally:
            elapsed = time.perf_counter() - start
            LLM_CALL_SECONDS.observe(elapsed)
            _add_stage_seconds("llm", elapsed)

        lines = [ln.strip() for ln in check.text.splitlines() if ln.strip()]
        unsupported = [ln[len("UNSUPPORTED:"):].strip() for ln in lines if ln.upper().startswith("UNSUPPORTED:")]
        if not unsupported:
            return [], check.usage
        mismatches = [
            ClaimMismatch(
                issue_key="(faithfulness)",
                detail=f"Not supported by retrieved evidence: {claim}",
            )
            for claim in unsupported
        ]
        return mismatches, check.usage

    async def _check_subtask_claims(
        self, text: str, space_ids: Sequence[int], subject_keys: set[str]
    ) -> Tuple[List[ClaimMismatch], Usage]:
        """Check whether differently typed issues were falsely presented as equivalent subtasks."""
        all_keys = {k.upper() for k in _ISSUE_KEY_RE.findall(text)} - subject_keys
        if len(all_keys) < 2:
            return [], Usage()
        start = time.perf_counter()
        try:
            result = await self._retrieval.query_issues(
                space_ids, {"issue_keys": sorted(all_keys)}
            )
        finally:
            elapsed = time.perf_counter() - start
            RETRIEVAL_CALL_SECONDS.observe(elapsed)
            _add_stage_seconds("retrieval", elapsed)
        by_key = {
            str(issue.get("issue_key", "")).upper(): issue
            for issue in result.issues
        }
        subtask_keys = {
            key
            for key in all_keys
            if by_key.get(key, {}).get("issue_type") == "subtask"
        }
        real_parent_keys = {
            str(by_key[key].get("parent_key", "")).upper()
            for key in subtask_keys
            if by_key.get(key, {}).get("parent_key")
        }
        other_keys = all_keys - subtask_keys - real_parent_keys
        if not subtask_keys or not other_keys:
            return [], Usage()

        classification_system = (
            "You are a precise text-classification assistant. You only ever answer with exactly one "
            "word."
        )
        classification_user = (
            f"An answer text mentions these issues together: {', '.join(sorted(subtask_keys))} (real "
            f"subtasks) and {', '.join(sorted(other_keys))} (a DIFFERENT issue_type — NOT a subtask "
            "of the same parent).\n\n"
            "Does the text present the second group as if it were the same kind of thing as the real "
            "subtasks — e.g. an equivalent item in the same split/list/group — rather than clearly "
            "distinguishing it as a separate, different kind of issue?\n\n"
            f'Text:\n"""\n{text}\n"""\n\n'
            "Answer with exactly one word: EQUIVALENT or DISTINGUISHED."
        )
        start = time.perf_counter()
        try:
            classification = await self._llm.next_turn(
                classification_system,
                [{"role": "user", "content": classification_user}],
                [],
            )
        finally:
            elapsed = time.perf_counter() - start
            LLM_CALL_SECONDS.observe(elapsed)
            _add_stage_seconds("llm", elapsed)
        if "DISTINGUISHED" in classification.text.upper():
            return [], classification.usage

        mismatches = [
            ClaimMismatch(
                issue_key=key,
                detail=(
                    f"{key}: actual issue_type is "
                    f"{by_key.get(key, {}).get('issue_type') or 'unknown/not found'!r}, "
                    f"different from the real subtasks ({', '.join(sorted(subtask_keys))}) it was "
                    "presented alongside — clarify it is a separate, related issue, not an "
                    "equivalent split-off item."
                ),
            )
            for key in sorted(other_keys)
        ]
        return mismatches, classification.usage

    async def _check_current_state_claims(
        self, text: str, space_ids: Sequence[int], subject_keys: set[str]
    ) -> Tuple[List[ClaimMismatch], Usage]:
        """Check whether the answer asserts a CURRENT status for an issue that contradicts
        query_issues' actual current status (see ground rule 4: distinguish current from historical
        state — the eval's Finding 1 traced a real failure here under deep multi-hop synthesis,
        where an earlier comment's state won out over a later, authoritative closing comment).

        The regex below is only a cost-gating precondition — skip the classification call entirely
        when the answer doesn't even mention a status-like word — not the actual detector; the closed
        vocabulary here is the domain's own known status values (done/in_progress/planned/blocked)
        plus their common synonyms, not a growing list of phrasing variants to chase.
        """
        if not _STATUS_HINT_RE.search(text):
            return [], Usage()
        mentioned_keys = {k.upper() for k in _ISSUE_KEY_RE.findall(text)}
        if not mentioned_keys:
            return [], Usage()

        start = time.perf_counter()
        try:
            result = await self._retrieval.query_issues(
                space_ids, {"issue_keys": sorted(mentioned_keys)}
            )
        finally:
            elapsed = time.perf_counter() - start
            RETRIEVAL_CALL_SECONDS.observe(elapsed)
            _add_stage_seconds("retrieval", elapsed)
        known = {
            str(issue.get("issue_key", "")).upper(): issue
            for issue in result.issues
            if issue.get("status")
        }
        if not known:
            return [], Usage()

        facts = "\n".join(f"- {key}: current status = {value['status']!r}" for key, value in sorted(known.items()))
        classification_system = (
            "You are a precise fact-checking assistant. You only ever answer with a JSON array."
        )
        classification_user = (
            "Actual current status of these issues, from structured data (authoritative — trust "
            f"this over the text below):\n{facts}\n\n"
            f'Answer text:\n"""\n{text}\n"""\n\n'
            "For each issue key above, does the answer text assert a CURRENT status/state for it "
            '(e.g. "is done", "still open", "currently blocked") that CONTRADICTS its actual current '
            "status? Ignore mentions of PAST/historical state that the text itself frames as history "
            '(e.g. "was originally blocked, then fixed last sprint") — only flag a contradiction '
            "about what the issue IS right now.\n\n"
            'Answer with a JSON array of the contradicting issue keys only, e.g. ["ATC-43"], or [] '
            "if none contradict."
        )
        start = time.perf_counter()
        try:
            classification = await self._llm.next_turn(
                classification_system,
                [{"role": "user", "content": classification_user}],
                [],
            )
        finally:
            elapsed = time.perf_counter() - start
            LLM_CALL_SECONDS.observe(elapsed)
            _add_stage_seconds("llm", elapsed)

        contradicted = _parse_json_key_list(classification.text) & known.keys()
        if not contradicted:
            return [], classification.usage

        mismatches = [
            ClaimMismatch(
                issue_key=key,
                detail=(
                    f"{key}: actual current status is {known[key]['status']!r} — the answer's claim "
                    "about its current status/state contradicts this; correct it (mentioning a past/"
                    "historical state is fine as long as it is clearly framed as history, not as the "
                    "current state)."
                ),
            )
            for key in sorted(contradicted)
        ]
        return mismatches, classification.usage

    def _cost(self, usage: Usage) -> float:
        return estimate_cost_usd(
            self._model, usage.input_tokens, usage.output_tokens,
            usage.cache_creation_input_tokens, usage.cache_read_input_tokens,
        )

    @staticmethod
    def _forced_stop_message(turn) -> dict:
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": "Retrieval budget exhausted. Answer now with what you have, or state "
                                f'"{ABSTENTION_PHRASE}" if it is not enough.',
                }
                for call in turn.tool_calls
            ],
        }


def _format_hits(hits: List[RetrievedChunk]) -> str:
    if not hits:
        return "No results found."
    return "\n\n".join(
        f"[{h.issue_key}] (chunk_type={h.chunk_type}, retrievers={h.retrievers}"
        f"{f', page={h.page_number}' if h.page_number is not None else ''})\n{h.content}"
        for h in hits
    )


_ISSUE_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b", re.IGNORECASE)

# Cost-gating precondition for _check_current_state_claims — the domain's own closed status
# vocabulary (done/in_progress/planned/blocked, per app/config.py's known statuses) plus common
# synonyms, not an open-ended set of phrasings to keep extending.
_STATUS_HINT_RE = re.compile(
    r"\b(done|resolved|closed|fixed|complete|completed|in[- ]?progress|ongoing|open|unresolved|"
    r"blocked|planned|active|reopened)\b",
    re.IGNORECASE,
)


def _parse_json_key_list(value: str) -> set[str]:
    match = re.search(r"\[.*\]", value, re.DOTALL)
    if not match:
        return set()
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return set()
    return {str(k).upper() for k in parsed if isinstance(k, str)}


def _verification_correction_message(mismatches: List["ClaimMismatch"]) -> dict:
    lines = [f"- {m.detail}" for m in mismatches]
    return {
        "role": "user",
        "content": (
            "Correction needed before this answer is final — structured data contradicts part of "
            "it:\n" + "\n".join(lines) +
            "\nRevise your answer to remove or correctly characterize each of these, then give ONLY "
            "the corrected answer text, exactly as it should be shown to the user. Do not mention "
            "this correction, do not say \"you're right\" or similar, and do not refer to a previous "
            "version of the answer — the user never saw it and has no idea a revision happened."
        ),
    }


_WORD_RE = re.compile(r"[a-z']{4,}")
# Common enough to appear in almost any sentence regardless of topic — excluded so the overlap score
# reflects shared SUBJECT MATTER, not shared grammar.
_STOPWORDS = frozenset({
    "this", "that", "with", "have", "been", "from", "were", "will", "would", "could", "should",
    "their", "there", "which", "when", "what", "into", "before", "after", "because", "then", "than",
    "only", "these", "those", "about", "while", "until", "each", "does", "being", "over",
})
def _citation_words(value: str) -> set[str]:
    return {word for word in _WORD_RE.findall(value.lower()) if word not in _STOPWORDS}


def _content_overlap_score(content: str, answer_text: str) -> int:
    """How many distinctive words a candidate chunk shares with the final answer text.

    The PRIMARY key for picking among multiple citation candidates for the same issue key — see the
    selection in _ground_answer. A cheap, deterministic proxy for "does this specific chunk actually
    support what the answer says," not a semantic judgment; good enough to distinguish a chunk that's
    genuinely off-topic (shares almost no vocabulary with the claim) from the one that is.
    """
    content_words = _citation_words(content)
    answer_words = _citation_words(answer_text)
    return len(content_words & answer_words)


def _ground_answer(
    text: str,
    citations: List[RetrievedChunk],
    evidence_issue_keys: set[str],
    semantic_used: bool,
    structured_used: bool,
) -> tuple[str, List[RetrievedChunk]]:
    """Reject unsupported issue references and return only sources the answer actually cites.

    Retrieval results are candidate evidence, not automatically citations. Previously every top-K
    chunk was returned to the UI, which produced long, duplicated, off-topic source lists and could
    still omit the issue asserted in the answer. This postcondition makes that impossible: any issue
    key in the final answer must have appeared in a real tool result, and semantic-only prose must cite
    at least one retrieved key. Sources are deduplicated to one chunk per cited issue.
    """
    if ABSTENTION_PHRASE in text:
        return ABSTENTION_PHRASE, []

    referenced_keys = {key.upper() for key in _ISSUE_KEY_RE.findall(text)}
    evidence_prefixes = {
        key.rsplit("-", 1)[0] for key in evidence_issue_keys
    }
    issue_key_refs = {k for k in referenced_keys if k.rsplit("-", 1)[0] in evidence_prefixes}
    if issue_key_refs - evidence_issue_keys:
        logger.warning(
            "rejecting answer with unsupported issue references",
            extra={"unsupported_issue_keys": sorted(issue_key_refs - evidence_issue_keys)},
        )
        return ABSTENTION_PHRASE, []

    text_upper = text.upper()
    mentioned_citation_keys = {
        citation.issue_key.upper()
        for citation in citations
        if citation.issue_key.upper() in text_upper
    }

    # A semantic-only answer with no explicit reference to what it retrieved violates the prompt's
    # citation contract. Structured counts/sprint stats are self-grounding and do not need one.
    if semantic_used and not structured_used and (not citations or not (issue_key_refs or mentioned_citation_keys)):
        return ABSTENTION_PHRASE, []

    # Group every citation candidate by issue key (preserving first-seen key order). Historically this
    # picked exactly ONE chunk per key — a safe shortcut when the only source of citations was
    # search_knowledge_base's top-K (already relevance-ranked, so "first" and "most relevant" usually
    # coincided) and a question about one issue usually hinged on one fact from one chunk. Found live: a
    # question about WHY an issue was reopened correctly answered from comment #4 of 6 on that issue,
    # but the UI's "Sources" showed comment #1 — an unrelated story-point-estimate note that happened to
    # be chronologically first.
    #
    # Word-overlap with the actual answer text (see _content_overlap_score) is the PRIMARY ranking key
    # within a group, not a tie-break on top of score. Score looks like a natural primary key — "prefer
    # the real relevance score when there is one" — but scoring is not comparable ACROSS tools: a search
    # hit's cosine/RRF/reranker score and a deterministic fetch's flat 0.0 are different scales for
    # different questions ("how well did this match the query" vs. "this is unranked, here is
    # everything"), the same class of scale mismatch as the RRF-vs-cosine bug (see
    # docs/ARCHITECTURE.md's retrieval section). Found live: search_knowledge_base can return a real hit
    # for an issue's own key (e.g. a comment that merely MENTIONS it) with score > 0, while
    # get_issue_details/get_issue_comments' actually-relevant chunk for that same key carries the
    # deliberately-flat score=0.0 — scoring primary would pick the topically-adjacent-but-wrong chunk
    # every time, never the one the answer actually quotes. Overlap directly measures "does this chunk
    # contain the words the answer used," which is exactly the citation's job; score is only a
    # fine-grained tiebreak among equally-overlapping candidates.
    candidates_by_key: Dict[str, List[RetrievedChunk]] = {}
    key_order: List[str] = []
    for citation in citations:
        key = citation.issue_key.upper()
        if key not in issue_key_refs and key not in mentioned_citation_keys:
            continue
        if key not in candidates_by_key:
            candidates_by_key[key] = []
            key_order.append(key)
        candidates_by_key[key].append(citation)

    _MAX_CITATIONS_PER_KEY = 5
    selected: List[RetrievedChunk] = []
    for key in key_order:
        ranked = sorted(
            candidates_by_key[key],
            key=lambda citation: (_content_overlap_score(citation.content, text), citation.score),
            reverse=True,
        )
        overlapping = [
            citation
            for citation in ranked
            if _content_overlap_score(citation.content, text) > 0
        ]
        selected.extend(overlapping[:_MAX_CITATIONS_PER_KEY] if overlapping else ranked[:1])
    return text, selected


def _format_issue_query(result: IssueQueryResult, filters: dict) -> str:
    """Render a structured issue-query result for the model as an explicit, exact tool result.

    Leads with the total count (the fact the "how many" answer hinges on), then the breakdowns, then a
    listed sample — so the model reports the exact number rather than counting the sample rows, which
    are only the first `limit`. `filters` is echoed back so the model can see what scope the count is
    over (e.g. a count of bugs vs. a count of everything).
    """
    lines = [f"Structured issue query (filters={filters or 'none - all issues in scope'}):",
             f"total_count = {result.total_count} (exact; this is the number to report for 'how many')"]
    if result.counts_by_type:
        lines.append("by type: " + ", ".join(f"{k}={v}" for k, v in sorted(result.counts_by_type.items())))
    if result.counts_by_status:
        lines.append("by status: " + ", ".join(f"{k}={v}" for k, v in sorted(result.counts_by_status.items())))
    if result.issues:
        shown = len(result.issues)
        suffix = "" if shown >= result.total_count else f" (showing first {shown} of {result.total_count})"
        lines.append(f"matching issues{suffix}:")
        for it in result.issues:
            lines.append(
                f"  [{it.get('issue_key')}] type={it.get('issue_type')} status={it.get('status')} "
                f"sprint={it.get('sprint_name') or '(backlog)'} "
                f"parent_key={it.get('parent_key') or '(none)'} "
                f"updated={it.get('updated_at')} — {it.get('title')}"
            )
    return "\n".join(lines)


def _format_sprint_query(result: SprintQueryResult, filters: dict) -> str:
    """Render a structured sprint-query result — same exact-count-first contract as
    _format_issue_query. Includes goal/dates/points so the model can answer velocity questions
    without a second round-trip once it already has the matching sprint(s)."""
    lines = [f"Structured sprint query (filters={filters or 'none - all sprints in scope'}):",
             f"total_count = {result.total_count} (exact; this is the number to report for 'how many')"]
    if result.counts_by_status:
        lines.append("by status: " + ", ".join(f"{k}={v}" for k, v in sorted(result.counts_by_status.items())))
    if result.sprints:
        shown = len(result.sprints)
        suffix = "" if shown >= result.total_count else f" (showing first {shown} of {result.total_count})"
        lines.append(f"matching sprints{suffix}:")
        for sp in result.sprints:
            lines.append(
                f"  [{sp.get('sprint_id')}] {sp.get('sprint_name')} status={sp.get('status')} "
                f"dates={sp.get('start_date')}..{sp.get('end_date')} "
                f"points={sp.get('completed_points')}/{sp.get('final_scope_points')} "
                f"(final) or /{sp.get('initial_committed_points')} (committed) — goal: {sp.get('goal')}"
            )
    return "\n".join(lines)


def _format_issue_history(result: IssueHistoryResult, filters: dict) -> str:
    """Render change records for the model: exact count first, then one line per transition.

    from/to values are included verbatim (truncated) — for title/description edits that IS the
    "what did I change" answer, and for status changes it's the reopen/blocked evidence.
    """

    def _trunc(v, n=300):
        if v is None:
            return "(empty)"
        v = str(v)
        return v if len(v) <= n else v[:n] + "…[truncated]"

    lines = [f"Issue change history (filters={filters or 'none - all changes in scope'}):",
             f"total_count = {result.total_count} (exact)"]
    if not result.changes:
        lines.append("No matching changes.")
    shown = len(result.changes)
    if result.changes and shown < result.total_count:
        lines.append(f"showing the {shown} most recent of {result.total_count}:")
    for c in result.changes:
        who = c.get("actor_name") or "system"
        when = c.get("changed_at")
        if c.get("event_type") == "field_change":
            # `description` on a field_change is free-text narrative the actor entered alongside the
            # raw value transition — e.g. "Daniel reopened the issue after the resent-email check
            # failed." It was previously dropped entirely on this branch (only the else/non-field-
            # change branch below rendered it), silently discarding the one place a "why" like a
            # reopen reason is most likely to already live in structured evidence, no comment lookup
            # needed. This corpus's convention for a CONTENTLESS description is a bare "Updated
            # <Field>" with no elaboration — the substantive ones either don't start that way or add
            # " — <narrative>" after it — so only truly generic ones stay terse.
            desc = c.get("description")
            is_generic = bool(desc) and desc.startswith("Updated ") and " — " not in desc
            suffix = f" — {_trunc(desc)}" if desc and not is_generic else ""
            lines.append(
                f"  [{c.get('issue_key')}] {when} {who} changed {c.get('field_name')}: "
                f"{_trunc(c.get('from_value'))} -> {_trunc(c.get('to_value'))}{suffix}"
            )
        else:
            lines.append(
                f"  [{c.get('issue_key')}] {when} {who}: {c.get('event_type')} — {c.get('description') or ''}"
            )
    return "\n".join(lines)


def _format_issue_comments(hits: List[RetrievedChunk], requested_keys: List[str]) -> str:
    if not hits:
        return (
            f"No comments found for {requested_keys or 'the requested issue(s)'} "
            "(either it has none, or the key doesn't exist in scope)."
        )
    return "\n\n".join(f"[{h.issue_key}] (comment)\n{h.content}" for h in hits)


def _format_issue_details(hits: List[RetrievedChunk], requested_keys: List[str]) -> str:
    if not hits:
        return (
            f"No issue body found for {requested_keys or 'the requested issue(s)'} "
            "(either the key doesn't exist in scope, or that issue has no title/description text)."
        )
    return "\n\n".join(f"[{h.issue_key}] (issue title+description)\n{h.content}" for h in hits)


def _format_issue_attachments(hits: List[RetrievedChunk], requested_keys: List[str]) -> str:
    if not hits:
        return (
            f"No attachment content found for {requested_keys or 'the requested issue(s)'} "
            "(either the key doesn't exist in scope, or that issue has no attachments/none could be parsed)."
        )
    return "\n\n".join(f"[{h.issue_key}] (attachment)\n{h.content}" for h in hits)
