"""HTTP surface: the agentic Q&A endpoint, plus a health check."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.auth.space_membership import SpaceMembershipError
from app.drafting.service import draft_task
from app.observability import CACHE_HITS_TOTAL, CACHE_LOOKUP_SECONDS
from app.planning.graph import plan_epic_multiagent
from app.planning.rollout_graph import ON_STAGE_VAR as ROLLOUT_ON_STAGE_VAR
from app.planning.rollout_graph import retry_rollout
from app.planning.rollout_schemas import initial_rollout_state
from app.planning.schemas import EpicDraft, IssueDraft
from app.planning.service import allocate_sprints, plan_epic, refine_plan, validate_and_order
from app.search.service import DEFAULT_MIN_SCORE, RERANKED_MIN_SCORE, dedupe_by_issue
from app.sprint_health.schemas import FlaggedIssue, SprintStats
from app.sprint_health.service import summarize_sprint_health

logger = logging.getLogger(__name__)
router = APIRouter()
_tracer = trace.get_tracer(__name__)


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/stats")
async def stats(request: Request) -> dict:
    """Cumulative token usage + estimated $ cost since process start. See app/stats.py."""
    return request.app.state.stats.as_dict()


class _CamelModel(BaseModel):
    """JSON in/out as camelCase — matches jira-backend's Jackson serialization (and, through it, the
    TypeScript frontend's convention) rather than Python's own snake_case. Same pattern
    vectorization-service's inbound Kafka messages already use (`app/models.py::_CamelModel` there)
    for the same reason: anything a JVM/TS consumer touches should speak camelCase at the wire, not
    make every caller translate.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ChatTurnIn(_CamelModel):
    role: Literal["user", "assistant"]
    content: str


class AskRequest(_CamelModel):
    question: str = Field(min_length=1)
    space_ids: List[int] = Field(min_length=1, description="Caller's authorized spaces (permission scope).")
    # Prior turns' final text only, oldest first — NOT the tool-call plumbing a completed turn used
    # internally (see CragAgent.ask's docstring note). The caller (frontend) is the one holding
    # conversation state; this service stays stateless across turns, same as it always has been for a
    # single question — it just now optionally sees more than one.
    history: List[ChatTurnIn] = Field(default_factory=list)


class CitationOut(_CamelModel):
    issue_key: str
    chunk_type: str
    source_id: int
    content: str
    page_number: int | None = None
    provenance: dict = Field(default_factory=dict)


class StageTimingsOut(_CamelModel):
    """Wall-clock breakdown of one /ask call, for the demo UI's latency toggle — not a replacement
    for the Prometheus P95 histograms (agent_llm_call_seconds etc.), which answer "typical latency
    across all traffic"; this answers "where did *this* request's time actually go," including the
    cache-lookup step CragAgent itself never sees."""

    cache_lookup_ms: float
    retrieval_ms: float
    llm_ms: float
    total_ms: float


class AskResponse(_CamelModel):
    answer: str
    abstained: bool
    retrieval_rounds: int
    queries_used: List[str]
    citations: List[CitationOut]
    # Human-readable results of structured tool calls (query_issues/query_sprints/get_issue_history)
    # this answer actually used — see AgentAnswer.structured_evidence's docstring for why this exists
    # (RAGAS/eval context needs somewhere to find a structured fact like "ATC-77 is blocked", which
    # `citations` alone never carries).
    structured_evidence: List[str] = []
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    cache_hit: bool = False
    stage_timings: Optional[StageTimingsOut] = None


@router.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest, request: Request) -> AskResponse:
    await _authorize_space_ids(request, req.space_ids)
    agent = request.app.state.agent
    cache = request.app.state.cache
    stats = request.app.state.stats
    route_start = time.perf_counter()

    # The semantic cache is keyed on (question text, space_ids) alone — correct only when the question
    # means the same thing on its own, which a follow-up in a conversation ("what are their issue
    # keys?") does not. Skip the cache entirely once there's history rather than risk serving another
    # conversation's answer to what's actually a context-dependent follow-up.
    use_cache = not req.history
    cache_ms = 0.0
    if use_cache:
        cache_start = time.perf_counter()
        cached = await _safe_cache_get(cache, req.question, req.space_ids)
        cache_ms = (time.perf_counter() - cache_start) * 1000
        if cached is not None:
            CACHE_HITS_TOTAL.labels("hit").inc()
            # A cache hit never touches CragAgent at all, so any stage_timings in the cached blob
            # belong to whichever earlier request actually computed the answer — replace them with
            # this request's own (near-zero retrieval/llm) numbers rather than serving stale ones.
            timings = StageTimingsOut(
                cache_lookup_ms=round(cache_ms, 2), retrieval_ms=0.0, llm_ms=0.0,
                total_ms=round((time.perf_counter() - route_start) * 1000, 2),
            )
            return AskResponse(**{**cached, "cache_hit": True, "stage_timings": timings})
        CACHE_HITS_TOTAL.labels("miss").inc()

    try:
        history = [{"role": t.role, "content": t.content} for t in req.history]
        # One parent span per /ask call: the OpenInference instrumentors in app/tracing.py already
        # give each individual LLM call its own span, but with no active parent span at the time
        # they're created, each one starts as its own trace root — Phoenix shows N disconnected
        # single-call traces per question instead of one tree with every corrective-retrieval round
        # nested under it. Opening this span first (and running the whole CragAgent loop inside it)
        # is what makes those child spans attach here instead.
        with _tracer.start_as_current_span(
            "CragAgent.ask",
            attributes={
                SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.AGENT.value,
                SpanAttributes.INPUT_VALUE: req.question,
            },
        ) as span:
            result = await agent.ask(req.question, req.space_ids, history=history)
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, result.text)
            span.set_attribute("retrieval_rounds", result.retrieval_rounds)
    except Exception as exc:  # noqa: BLE001 - surface as a clean 502, don't leak internals/tracebacks
        # Observed for real under concurrency load (loadtest/, see loadtest/README.md): a local LLM
        # server (LM Studio) errors a request that arrives while it's busy with another.
        # OpenAICompatibleClient already retries that transiently for 5xx
        # (app/llm/openai_compatible_client.py); this is the backstop for whatever still fails after
        # retries are exhausted (including a 400, which is deliberately NOT retried — see that
        # module's docstring), or any other downstream failure (retrieval).
        logger.exception("agent.ask() failed")
        raise HTTPException(status_code=502, detail="AI request failed") from exc
    stats.record(result.usage, result.estimated_cost_usd)

    stage_timings = StageTimingsOut(
        cache_lookup_ms=round(cache_ms, 2),
        retrieval_ms=round(result.stage_seconds.get("retrieval", 0.0) * 1000, 2),
        llm_ms=round(result.stage_seconds.get("llm", 0.0) * 1000, 2),
        total_ms=round((time.perf_counter() - route_start) * 1000, 2),
    )
    response = AskResponse(
        answer=result.text,
        abstained=result.abstained,
        retrieval_rounds=result.retrieval_rounds,
        queries_used=result.queries_used,
        citations=[
            CitationOut(
                issue_key=c.issue_key, chunk_type=c.chunk_type, source_id=c.source_id,
                content=c.content, page_number=c.page_number,
                provenance=c.provenance,
            )
            for c in result.citations
        ],
        structured_evidence=result.structured_evidence,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        estimated_cost_usd=result.estimated_cost_usd,
        cache_hit=False,
        stage_timings=stage_timings,
    )
    # Cache the answer, not the citations' full content — see app/cache/semantic_cache.py for why
    # (staleness / invalidation reasoning) and what's deliberately excluded from what gets cached.
    # Never cache a follow-up's answer under its bare question text either — same reasoning as the
    # read side above, just applied to the write. stage_timings is excluded too: it's overwritten
    # unconditionally on the cache-hit path above regardless, so caching it would just be dead weight.
    if use_cache:
        await _safe_cache_put(
            cache, req.question, req.space_ids,
            response.model_dump(exclude={"cache_hit", "stage_timings"}),
        )
    return response


def _sse(event_name: str, data: dict) -> str:
    return f"event: {event_name}\ndata: {json.dumps(data)}\n\n"


@router.post("/ask/stream")
async def ask_stream(req: AskRequest, request: Request) -> StreamingResponse:
    """SSE variant of POST /ask: same CragAgent call, same response shape, but progress events
    ("searching the knowledge base", "verifying and finalizing the answer", ...) are pushed to the
    client as they happen inside the loop instead of the caller seeing nothing until the whole thing
    finishes. Purely a surfacing change — see `CragAgent.ask`'s `on_stage` parameter, which announces
    checkpoints the loop already passes through; nothing about the loop's control flow changes here.

    Implementation note: `agent.ask()` runs as a background task while this generator drains a queue
    `on_stage` feeds — the two run concurrently so stage events reach the client in real time instead
    of being collected and replayed after the fact, which would defeat the point.
    """
    await _authorize_space_ids(request, req.space_ids)
    agent = request.app.state.agent
    cache = request.app.state.cache
    stats = request.app.state.stats
    route_start = time.perf_counter()

    async def generate() -> AsyncIterator[str]:
        use_cache = not req.history
        cache_ms = 0.0
        if use_cache:
            cache_start = time.perf_counter()
            cached = await _safe_cache_get(cache, req.question, req.space_ids)
            cache_ms = (time.perf_counter() - cache_start) * 1000
            if cached is not None:
                CACHE_HITS_TOTAL.labels("hit").inc()
                yield _sse("stage", {"label": "using a cached answer"})
                timings = StageTimingsOut(
                    cache_lookup_ms=round(cache_ms, 2), retrieval_ms=0.0, llm_ms=0.0,
                    total_ms=round((time.perf_counter() - route_start) * 1000, 2),
                )
                payload = AskResponse(**{**cached, "cache_hit": True, "stage_timings": timings})
                yield _sse("result", payload.model_dump(by_alias=True))
                return
            CACHE_HITS_TOTAL.labels("miss").inc()

        stage_queue: asyncio.Queue[str] = asyncio.Queue()
        history = [{"role": t.role, "content": t.content} for t in req.history]

        # Same "one parent span per question" fix as the non-streaming /ask handler (see its comment)
        # — opened here, before the task exists, because asyncio.create_task snapshots the *current*
        # contextvars.Context at creation time. Opening the span first means that snapshot includes
        # it, so every LLM-call span the OpenInference instrumentors create inside agent.ask() (running
        # in that separate task) still nests under this one instead of each becoming its own trace root.
        with _tracer.start_as_current_span(
            "CragAgent.ask",
            attributes={
                SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.AGENT.value,
                SpanAttributes.INPUT_VALUE: req.question,
            },
        ) as span:
            agent_task = asyncio.create_task(
                agent.ask(req.question, req.space_ids, history=history, on_stage=stage_queue.put_nowait)
            )

            while not agent_task.done():
                try:
                    label = await asyncio.wait_for(stage_queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                yield _sse("stage", {"label": label})
            # The loop above can exit right after agent_task finishes but before the last stage event
            # (e.g. "verifying and finalizing the answer") was drained — flush whatever's left so the
            # client sees every checkpoint in order before the final result.
            while not stage_queue.empty():
                yield _sse("stage", {"label": stage_queue.get_nowait()})

            try:
                result = agent_task.result()
            except Exception as exc:  # noqa: BLE001 - see the non-streaming /ask handler's identical backstop
                logger.exception("agent.ask() failed (stream)")
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
                yield _sse("error", {"detail": "AI request failed"})
                return
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, result.text)
            span.set_attribute("retrieval_rounds", result.retrieval_rounds)
        stats.record(result.usage, result.estimated_cost_usd)

        stage_timings = StageTimingsOut(
            cache_lookup_ms=round(cache_ms, 2),
            retrieval_ms=round(result.stage_seconds.get("retrieval", 0.0) * 1000, 2),
            llm_ms=round(result.stage_seconds.get("llm", 0.0) * 1000, 2),
            total_ms=round((time.perf_counter() - route_start) * 1000, 2),
        )
        response = AskResponse(
            answer=result.text,
            abstained=result.abstained,
            retrieval_rounds=result.retrieval_rounds,
            queries_used=result.queries_used,
            citations=[
                CitationOut(
                    issue_key=c.issue_key, chunk_type=c.chunk_type, source_id=c.source_id,
                    content=c.content, page_number=c.page_number,
                    provenance=c.provenance,
                )
                for c in result.citations
            ],
            structured_evidence=result.structured_evidence,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            estimated_cost_usd=result.estimated_cost_usd,
            cache_hit=False,
            stage_timings=stage_timings,
        )
        if use_cache:
            await _safe_cache_put(
                cache, req.question, req.space_ids,
                response.model_dump(exclude={"cache_hit", "stage_timings"}),
            )
        yield _sse("result", response.model_dump(by_alias=True))

    return StreamingResponse(generate(), media_type="text/event-stream")


class SemanticSearchRequest(_CamelModel):
    query: str = Field(min_length=1)
    space_ids: List[int] = Field(min_length=1, description="Caller's authorized spaces (permission scope).")
    limit: int = Field(10, ge=1, le=50)
    min_score: Optional[float] = Field(
        None, description="Relevance floor below which a result isn't returned at all, rather than "
                           "always handing back the top N regardless of actual relevance. No fixed "
                           "range: the meaningful scale depends on whether vectorization-service "
                           "reranks results (0-1 cosine similarity when it doesn't; an unbounded "
                           "cross-encoder logit when it does — see app/search/service.py). Null uses "
                           "the scale-appropriate service default, chosen automatically based on "
                           "whether this specific response was reranked."
    )


class SemanticSearchHitOut(_CamelModel):
    issue_id: int
    issue_key: str
    score: float
    snippet: str


class SemanticSearchResponse(_CamelModel):
    results: List[SemanticSearchHitOut]


@router.post("/search", response_model=SemanticSearchResponse)
async def semantic_search_endpoint(req: SemanticSearchRequest, request: Request) -> SemanticSearchResponse:
    """Retrieval only — no agent loop, no LLM call. Returns ranked *issues* (deduped, best chunk score
    per issue) instead of chunks, which is what both the "find related issues" search mode and
    duplicate-issue detection actually want: "is there already an issue about this," not a passage to
    cite in a sentence.

    Uses `mode="vector"` deliberately, unlike /ask's own retrieval (hybrid, RRF-fused) — vector mode's
    raw score is real cosine similarity (0-1), which `min_score` (see app/search/service.py) can
    filter on meaningfully; RRF's rank-fusion score cannot. Lexical/keyword matching is intentionally
    not part of this endpoint's ranking as a result — it's optimized for "does this mean the same
    thing," not "does this contain the same words."

    One thing `mode` does NOT control: whether vectorization-service reranks results with a
    cross-encoder (`VEC_RERANK_ENABLED`, a server-side setting). When it does, the returned `score` is
    an unbounded cross-encoder logit, not cosine similarity — applying the cosine-calibrated
    `min_score` against that scale would silently filter on the wrong number (found live: a real
    deployment with reranking on returned scores like 5.4/1.7 for genuine matches). This endpoint
    checks `reranked` and switches to `RERANKED_MIN_SCORE` in that case (see app/search/service.py for
    how that number was grounded in the same live observation) rather than either applying the wrong
    threshold or applying none at all.
    """
    await _authorize_space_ids(request, req.space_ids)
    retrieval = request.app.state.retrieval
    try:
        # Over-fetch chunks before dedup: several chunks can belong to the same issue, and
        # dedupe_by_issue drops attachment chunks and below-threshold chunks entirely (see its
        # docstring), so a shallow raw pool could easily under-fill `limit` distinct issues. Capped
        # at 50: vectorization-service's own SearchRequest.limit is `le=50` (app/api/routes.py there),
        # and this endpoint's `req.limit` alone goes up to 50 too, so `req.limit * 5` can ask for up
        # to 250 — well past what the downstream service will accept, turning any caller-supplied
        # limit above 10 into a 502 (found live: the "find related issues" UI defaults to limit=20,
        # so this wasn't a corner case).
        fetch_limit = min(max(req.limit * 5, 30), 50)
        result = await retrieval.search_with_meta(req.query, req.space_ids, limit=fetch_limit, mode="vector")
    except Exception as exc:  # noqa: BLE001 - same clean-502 contract as /ask, don't leak internals
        logger.exception("semantic search failed")
        raise HTTPException(status_code=502, detail="search request failed") from exc

    if req.min_score is not None:
        effective_min_score = req.min_score
    elif result.reranked:
        effective_min_score = RERANKED_MIN_SCORE
    else:
        effective_min_score = DEFAULT_MIN_SCORE
    ranked = dedupe_by_issue(result.hits, req.limit, min_score=effective_min_score)

    return SemanticSearchResponse(
        results=[
            SemanticSearchHitOut(issue_id=h.issue_id, issue_key=h.issue_key, score=h.score, snippet=h.content[:280])
            for h in ranked
        ]
    )


class DraftTaskRequest(_CamelModel):
    description: str = Field(min_length=1, description="Rough, informal description of the work.")
    existing_labels: List[str] = Field(default_factory=list, description="Labels already in use, for the model to prefer reusing over inventing new ones.")


class TaskDraftOut(_CamelModel):
    title: str
    issue_type: str
    labels: List[str]
    estimate_story_points: Optional[int]
    dependencies: List[str]


class DraftTaskResponse(_CamelModel):
    draft: TaskDraftOut
    degraded: bool
    latency_seconds: float


@router.post("/draft-task", response_model=DraftTaskResponse)
async def draft_task_endpoint(req: DraftTaskRequest, request: Request) -> DraftTaskResponse:
    """AI-assisted task creation: one structured-output call -> title/labels/estimate/dependencies.

    Never 500s on a model/provider failure — see app/drafting/service.py's degradation strategy.
    `degraded=True` tells the caller the draft is an unassisted fallback, not a model failure hidden
    behind a normal-looking response.
    """
    client = request.app.state.instructor_client
    model = request.app.state.instructor_model
    result = await draft_task(client, model, req.description, req.existing_labels)
    return DraftTaskResponse(
        draft=TaskDraftOut(**result.draft.model_dump()),
        degraded=result.degraded,
        latency_seconds=round(result.latency_seconds, 3),
    )


class PlanEpicRequest(_CamelModel):
    proposal: str = Field(min_length=1, description="High-level description of the work to plan.")
    existing_labels: List[str] = Field(default_factory=list, description="Labels already in use, for the model to prefer reusing over inventing new ones.")
    sprint_capacity_points: Optional[float] = Field(
        None, description="Real historical sprint velocity (e.g. average completedPoints of recent "
                           "sprints), computed by the caller. Null means open-ended bin-packing with "
                           "no capacity cap."
    )
    target_sprint_count: Optional[int] = Field(
        None, ge=1, description="If set, distribute issues across exactly this many sprints instead "
                                 "of an open-ended number of capacity-sized buckets."
    )


class EpicDraftOut(_CamelModel):
    title: str
    description: str
    goals: List[str]


class IssueDraftOut(_CamelModel):
    temp_id: str
    title: str
    description: str
    issue_type: str
    labels: List[str]
    estimate_story_points: Optional[int]
    estimate_rationale: Optional[str]
    depends_on: List[str]


class SprintBucketOut(_CamelModel):
    sprint_index: int
    issue_temp_ids: List[str]
    total_points: int


class PlanEpicResponse(_CamelModel):
    epic: EpicDraftOut
    issues: List[IssueDraftOut]
    sprint_plan: List[SprintBucketOut]
    degraded: bool
    latency_seconds: float


@router.post("/plan-epic", response_model=PlanEpicResponse)
async def plan_epic_endpoint(req: PlanEpicRequest, request: Request) -> PlanEpicResponse:
    """AI-assisted epic planning: one structured-output call -> epic + decomposed issues, then two
    deterministic passes (dependency-cycle validation + topological sort, then sprint bin-packing).

    With `epic_planning_multiagent_enabled`, the first step instead runs a LangGraph
    planner -> estimator -> critic pipeline (app/planning/graph.py). Both paths return the same
    `PlanResult`, so everything below this line — and the response shape the frontend depends on — is
    identical either way.

    Never 500s on a model/provider failure — see app/planning/service.py's degradation strategy.
    Nothing is persisted here; the caller reviews and commits via the existing issue/sprint/link APIs.
    """
    client = request.app.state.instructor_client
    model = request.app.state.instructor_model
    planning_graph = getattr(request.app.state, "planning_graph", None)
    if planning_graph is not None:
        result = await plan_epic_multiagent(planning_graph, req.proposal, req.existing_labels)
    else:
        result = await plan_epic(client, model, req.proposal, req.existing_labels)
    ordered_issues = validate_and_order(result.plan.issues)
    buckets = allocate_sprints(ordered_issues, req.sprint_capacity_points, req.target_sprint_count)
    return PlanEpicResponse(
        epic=EpicDraftOut(**result.plan.epic.model_dump()),
        issues=[IssueDraftOut(**issue.model_dump()) for issue in ordered_issues],
        sprint_plan=[
            SprintBucketOut(
                sprint_index=b.sprint_index,
                issue_temp_ids=b.issue_temp_ids,
                total_points=b.total_points,
            )
            for b in buckets
        ],
        degraded=result.degraded,
        latency_seconds=round(result.latency_seconds, 3),
    )


class EpicDraftIn(_CamelModel):
    title: str = Field(min_length=1, max_length=120)
    description: str
    goals: List[str] = Field(default_factory=list)


class IssueDraftIn(_CamelModel):
    temp_id: str
    title: str = Field(min_length=1, max_length=120)
    description: str = ""
    issue_type: str = "task"
    labels: List[str] = Field(default_factory=list)
    estimate_story_points: Optional[int] = None
    estimate_rationale: Optional[str] = None
    depends_on: List[str] = Field(default_factory=list)


class RefinePlanRequest(_CamelModel):
    epic: EpicDraftIn
    issues: List[IssueDraftIn] = Field(min_length=1)
    instruction: str = Field(min_length=1, description="Free-text change request, e.g. 'add a QA task' or 'drop the inventory issue and its dependents'.")
    existing_labels: List[str] = Field(default_factory=list)
    sprint_capacity_points: Optional[float] = None
    target_sprint_count: Optional[int] = None


@router.post("/plan-epic/refine", response_model=PlanEpicResponse)
async def refine_plan_endpoint(req: RefinePlanRequest, request: Request) -> PlanEpicResponse:
    """Applies a free-text edit instruction to a plan already returned by /plan-epic (or a previous
    /refine call) — the "actually, we also need a QA task" iteration loop of real sprint planning.
    Same never-persists-anything contract as /plan-epic; the caller still reviews before committing.
    """
    client = request.app.state.instructor_client
    model = request.app.state.instructor_model
    epic = EpicDraft(title=req.epic.title, description=req.epic.description, goals=req.epic.goals)
    issues = [
        IssueDraft(
            temp_id=i.temp_id, title=i.title, description=i.description, issue_type=i.issue_type,
            labels=i.labels, estimate_story_points=i.estimate_story_points,
            estimate_rationale=i.estimate_rationale, depends_on=i.depends_on,
        )
        for i in req.issues
    ]
    result = await refine_plan(client, model, epic, issues, req.instruction, req.existing_labels)
    ordered_issues = validate_and_order(result.plan.issues)
    buckets = allocate_sprints(ordered_issues, req.sprint_capacity_points, req.target_sprint_count)
    return PlanEpicResponse(
        epic=EpicDraftOut(**result.plan.epic.model_dump()),
        issues=[IssueDraftOut(**issue.model_dump()) for issue in ordered_issues],
        sprint_plan=[
            SprintBucketOut(
                sprint_index=b.sprint_index,
                issue_temp_ids=b.issue_temp_ids,
                total_points=b.total_points,
            )
            for b in buckets
        ],
        degraded=result.degraded,
        latency_seconds=round(result.latency_seconds, 3),
    )


# --- Epic rollout: durable, human-approved commit-to-Jira workflow (app/planning/rollout_graph.py) ---
# Distinct from /plan-epic above: that endpoint never persists anything (see its own docstring) and
# returns in one blocking call. This one can pause for an arbitrary real amount of time waiting on a
# human decision (Postgres-checkpointed, survives an ai-service restart while paused) and, once
# approved, actually writes the issues to jira-backend — the one place in the planning feature with an
# irreversible side effect, which is why it is the one place this codebase uses LangGraph's durable-
# execution primitives rather than a plain function call.


class RolloutIssueOut(_CamelModel):
    temp_id: str
    title: str
    description: str
    issue_type: str
    labels: List[str]
    estimate_story_points: Optional[int]
    estimate_rationale: Optional[str]
    depends_on: List[str]


class RolloutPlanOut(_CamelModel):
    epic: Optional[EpicDraftOut]
    issues: List[RolloutIssueOut]
    sprint_plan: List[SprintBucketOut]


class RolloutStatusResponse(_CamelModel):
    thread_id: str
    status: str
    plan: Optional[RolloutPlanOut]
    epic_issue_key: Optional[str]
    committed_issue_keys: Dict[str, str]
    error: Optional[str]


def _rollout_status_response(thread_id: str, values: dict) -> RolloutStatusResponse:
    epic = values.get("epic")
    issues = values.get("issues") or []
    plan = RolloutPlanOut(
        epic=EpicDraftOut(**epic.model_dump()) if epic else None,
        issues=[RolloutIssueOut(**i.model_dump()) for i in issues],
        sprint_plan=[SprintBucketOut(**b) for b in values.get("sprint_buckets") or []],
    )
    return RolloutStatusResponse(
        thread_id=thread_id,
        status=values.get("status", "pending_approval"),
        plan=plan,
        epic_issue_key=values.get("epic_issue_key"),
        committed_issue_keys=values.get("committed") or {},
        error=values.get("error"),
    )


class PlanRolloutRequest(_CamelModel):
    proposal: str = Field(min_length=1)
    existing_labels: List[str] = Field(default_factory=list)
    sprint_capacity_points: Optional[float] = None
    target_sprint_count: Optional[int] = Field(None, ge=1)
    space_id: int = Field(description="The single space this epic's issues will be created in.")


def _rollout_graph(request: Request):
    graph = getattr(request.app.state, "rollout_graph", None)
    if graph is None:
        raise HTTPException(status_code=503, detail="epic rollout is disabled (AI_EPIC_ROLLOUT_ENABLED=false)")
    return graph


def _caller_identity(request: Request) -> tuple[str, str]:
    # Deliberately empty-string, not a placeholder like "internal" — SpaceMembershipChecker.validate's
    # `if not user_id: return` skip (and review_node's identical recheck) depends on this being falsy
    # for a direct/internal caller (this project's own eval/demo scripts, bypassing the gateway) to be
    # treated as trusted, same shape _authorize_space_ids already uses. A non-empty placeholder here
    # would silently defeat that skip and send a fabricated identity to jira-backend's real membership
    # endpoint instead.
    user_id = request.headers.get("x-user-id") or ""
    username = request.headers.get("x-username") or ""
    return user_id, username


@router.post("/plan-epic/rollout", response_model=RolloutStatusResponse)
async def start_rollout_endpoint(req: PlanRolloutRequest, request: Request) -> RolloutStatusResponse:
    """Starts a new durable rollout workflow: generates a plan, then pauses for approval. Always
    returns with status="pending_approval" (or "failed" if plan generation itself failed) — this call
    never commits anything, regardless of how the plan looks.
    """
    await _authorize_space_ids(request, [req.space_id])
    graph = _rollout_graph(request)
    user_id, username = _caller_identity(request)
    thread_id = str(uuid.uuid4())
    initial = initial_rollout_state(
        req.proposal, req.existing_labels, req.sprint_capacity_points, req.target_sprint_count,
        req.space_id, user_id, username,
    )
    config = {"configurable": {"thread_id": thread_id}}
    await graph.ainvoke(initial, config=config)
    snap = await graph.aget_state(config)
    return _rollout_status_response(thread_id, snap.values)


@router.post("/plan-epic/rollout/stream")
async def start_rollout_stream_endpoint(req: PlanRolloutRequest, request: Request) -> StreamingResponse:
    """SSE variant of POST /plan-epic/rollout — same "stage progress labels, not token streaming"
    pattern as /ask/stream and /sprint-recovery/start/stream. `plan_node` is the only node here that
    makes an LLM call (`review` is a pause, `create_epic`/`commit_one` are fast Jira REST writes), so
    this carries at most one `stage` event before the `result` frame — still worth it since that one
    call (especially through the multi-agent planner<->critic path, when enabled) is the entire wait.
    """
    await _authorize_space_ids(request, [req.space_id])
    graph = _rollout_graph(request)
    user_id, username = _caller_identity(request)
    thread_id = str(uuid.uuid4())
    initial = initial_rollout_state(
        req.proposal, req.existing_labels, req.sprint_capacity_points, req.target_sprint_count,
        req.space_id, user_id, username,
    )
    config = {"configurable": {"thread_id": thread_id}}

    async def generate() -> AsyncIterator[str]:
        stage_queue: asyncio.Queue[str] = asyncio.Queue()
        token = ROLLOUT_ON_STAGE_VAR.set(stage_queue.put_nowait)
        try:
            task = asyncio.create_task(graph.ainvoke(initial, config=config))
            while not task.done():
                try:
                    label = await asyncio.wait_for(stage_queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                yield _sse("stage", {"label": label})
            while not stage_queue.empty():
                yield _sse("stage", {"label": stage_queue.get_nowait()})
            try:
                task.result()
            except Exception as exc:  # noqa: BLE001 - clean SSE error frame, same as ask_stream's backstop
                logger.exception("epic rollout stream failed")
                yield _sse("error", {"detail": "epic rollout failed"})
                return
        finally:
            ROLLOUT_ON_STAGE_VAR.reset(token)
        snap = await graph.aget_state(config)
        yield _sse("result", _rollout_status_response(thread_id, snap.values).model_dump(by_alias=True))

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get("/plan-epic/rollout/{thread_id}", response_model=RolloutStatusResponse)
async def get_rollout_status_endpoint(thread_id: str, request: Request) -> RolloutStatusResponse:
    graph = _rollout_graph(request)
    config = {"configurable": {"thread_id": thread_id}}
    snap = await graph.aget_state(config)
    if not snap.values:
        raise HTTPException(status_code=404, detail=f"no rollout workflow with thread_id={thread_id}")
    await _authorize_workflow_state(request, snap.values)
    return _rollout_status_response(thread_id, snap.values)


class RolloutDecisionRequest(_CamelModel):
    decision: Literal["approve", "edit", "reject"]
    epic: Optional[EpicDraftIn] = None
    issues: Optional[List[IssueDraftIn]] = None


@router.post("/plan-epic/rollout/{thread_id}/decision", response_model=RolloutStatusResponse)
async def submit_rollout_decision_endpoint(
    thread_id: str, req: RolloutDecisionRequest, request: Request
) -> RolloutStatusResponse:
    """Resumes a paused rollout with a human decision. `edit` requires `epic`+`issues` (the caller's
    modified version of the paused plan); `approve`/`reject` ignore them if present. Runs the commit
    loop to completion (or failure) before returning — see rollout_graph.py's module docstring for why
    that loop is safe to interrupt with a process crash, not just with this ordinary blocking wait.
    """
    graph = _rollout_graph(request)
    config = {"configurable": {"thread_id": thread_id}}
    snap = await graph.aget_state(config)
    if not snap.values:
        raise HTTPException(status_code=404, detail=f"no rollout workflow with thread_id={thread_id}")
    await _authorize_workflow_state(request, snap.values)
    if snap.values.get("status") != "pending_approval":
        raise HTTPException(
            status_code=409,
            detail=f"rollout {thread_id} is not awaiting approval (status={snap.values.get('status')})",
        )

    resume_payload: dict = {"decision": req.decision}
    if req.decision == "edit":
        if req.epic is None or not req.issues:
            raise HTTPException(status_code=422, detail="decision=edit requires epic and issues")
        resume_payload["epic"] = req.epic.model_dump()
        resume_payload["issues"] = [i.model_dump() for i in req.issues]

    await graph.ainvoke(Command(resume=resume_payload), config=config)
    final = await graph.aget_state(config)
    return _rollout_status_response(thread_id, final.values)


@router.post("/plan-epic/rollout/{thread_id}/retry", response_model=RolloutStatusResponse)
async def retry_rollout_endpoint(thread_id: str, request: Request) -> RolloutStatusResponse:
    """Un-sticks a rollout at `status="committing"` (a suspected process crash — `GET
    /plan-epic/rollout/{id}` alone, i.e. "Refresh status" in the UI, is a pure state *read* and will
    never advance a crashed run on its own, verified live: a crashed thread sat at "committing"
    unchanged across repeated status reads) or `status="failed"` (a clean failure — jira-backend was
    unreachable, returned a 5xx, etc, while ai-service itself stayed up). See
    rollout_graph.py's `retry_rollout` docstring for why these two need different internal handling.
    409s for any other status, including `pending_approval` — this isn't another way to submit the
    original decision, only to un-stick one that already got one.
    """
    graph = _rollout_graph(request)
    config = {"configurable": {"thread_id": thread_id}}
    snap = await graph.aget_state(config)
    if not snap.values:
        raise HTTPException(status_code=404, detail=f"no rollout workflow with thread_id={thread_id}")
    await _authorize_workflow_state(request, snap.values)
    if snap.values.get("status") not in ("committing", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"rollout {thread_id} is not committing or failed (status={snap.values.get('status')})",
        )
    await retry_rollout(graph, thread_id)
    final = await graph.aget_state(config)
    return _rollout_status_response(thread_id, final.values)


class FlaggedIssueIn(_CamelModel):
    issue_key: str
    title: str
    detail: Optional[str] = None


class SprintHealthRequest(_CamelModel):
    sprint_name: str = Field(min_length=1)
    risk_level: Literal["on_track", "at_risk", "behind"] = Field(
        description="Computed by the caller from real burndown math (points/days) — ai-service never guesses this."
    )
    days_remaining: Optional[int] = None
    committed_points: Optional[float] = None
    completed_points: Optional[float] = None
    total_points: Optional[float] = None
    issue_counts_by_status: Dict[str, int] = Field(default_factory=dict)
    blocked_issues: List[FlaggedIssueIn] = Field(default_factory=list)
    stale_issues: List[FlaggedIssueIn] = Field(default_factory=list)
    unestimated_issues: List[FlaggedIssueIn] = Field(default_factory=list)


class SprintHealthResponse(_CamelModel):
    summary: str
    recommendations: List[str]
    degraded: bool
    latency_seconds: float


@router.post("/sprint-health", response_model=SprintHealthResponse)
async def sprint_health_endpoint(req: SprintHealthRequest, request: Request) -> SprintHealthResponse:
    """Turns pre-computed sprint stats + flagged issues into a short narrative + recommendations.
    Nothing here is calculated by the model — see app/sprint_health/schemas.py's module docstring —
    and a model failure degrades to a mechanically-assembled summary, never a 500.
    """
    client = request.app.state.instructor_client
    model = request.app.state.instructor_model
    stats = SprintStats(
        sprint_name=req.sprint_name,
        risk_level=req.risk_level,
        days_remaining=req.days_remaining,
        committed_points=req.committed_points,
        completed_points=req.completed_points,
        total_points=req.total_points,
        issue_counts_by_status=req.issue_counts_by_status,
        blocked_issues=[FlaggedIssue(**i.model_dump()) for i in req.blocked_issues],
        stale_issues=[FlaggedIssue(**i.model_dump()) for i in req.stale_issues],
        unestimated_issues=[FlaggedIssue(**i.model_dump()) for i in req.unestimated_issues],
    )
    result = await summarize_sprint_health(client, model, stats)
    return SprintHealthResponse(
        summary=result.insight.summary,
        recommendations=result.insight.recommendations,
        degraded=result.degraded,
        latency_seconds=round(result.latency_seconds, 3),
    )


async def _safe_cache_get(cache, question: str, space_ids: List[int]) -> Optional[dict]:
    # Caching is an optimization, not a correctness requirement — a broken cache backend (e.g. the
    # embedding call to vectorization-service's /embed timing out under load, observed for real in
    # loadtest/) must degrade to "treat as a cache miss," never take down the whole request. Same
    # principle as the 502 backstop above, applied to the cache specifically.
    start = time.perf_counter()
    try:
        return await cache.get(question, space_ids)
    except Exception:  # noqa: BLE001
        logger.warning("cache.get() failed; treating as a cache miss", exc_info=True)
        return None
    finally:
        CACHE_LOOKUP_SECONDS.observe(time.perf_counter() - start)


async def _safe_cache_put(cache, question: str, space_ids: List[int], response: dict) -> None:
    try:
        await cache.put(question, space_ids, response)
    except Exception:  # noqa: BLE001
        # The answer was already computed and returned to the caller successfully — a failed cache
        # write must never turn a successful request into a failed one.
        logger.warning("cache.put() failed; response was still returned to the caller", exc_info=True)


async def _authorize_space_ids(request: Request, space_ids: List[int]) -> None:
    """Reject a request for a space the gateway-forwarded user isn't a member of.

    Unlike the cache helpers above, this must NOT degrade gracefully on failure — this is a
    permission boundary, not an optimization. If jira-backend (the membership source of truth) is
    unreachable, failing OPEN (silently permitting) would defeat the entire point of the check; the
    502 here fails CLOSED instead, the opposite tradeoff from the cache's "never break a request over
    an optimization" principle, deliberately.
    """
    user_id = request.headers.get("x-user-id")
    username = request.headers.get("x-username")
    checker = request.app.state.space_membership
    try:
        await checker.validate(user_id, username, space_ids)
    except SpaceMembershipError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - jira-backend unreachable etc.
        logger.error("space membership check failed", exc_info=True)
        raise HTTPException(status_code=502, detail="space membership check failed") from exc


async def _authorize_workflow_state(request: Request, values: dict) -> None:
    """Restrict a durable workflow to the authenticated user who created it.

    A thread UUID is an identifier, not an authorization credential. Start endpoints persist the
    gateway-derived caller identity in graph state; every later status/resume/history operation must
    compare the *current* caller with that owner before exposing or mutating the checkpoint. Direct
    internal callers without gateway headers remain supported for local eval scripts, matching
    `_authorize_space_ids`' trusted-internal convention.
    """
    user_id, _ = _caller_identity(request)
    if not user_id:
        return
    if str(values.get("user_id") or "") != user_id:
        raise HTTPException(status_code=403, detail="workflow is not owned by the authenticated user")
    await _authorize_space_ids(request, [values["space_id"]])
