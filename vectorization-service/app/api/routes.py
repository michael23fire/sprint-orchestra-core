"""HTTP surface: liveness/readiness, ingestion stats, and the raw retrieval API.

/healthz and /stats are the operational surface. /search is the **raw index query API**: this
service owns the index (both the vector and lexical sides), so it's the one place that knows how to
query it. It deliberately does NOT do query-time business logic — permission resolution beyond the
given space_ids, answer synthesis, agentic re-retrieval, citation formatting for a chat UI. That
composition belongs to the caller (the ai-service), which treats this endpoint as a tool: same
ingestion-vs-retrieval split as jira-backend's FTS (index owner exposes primitives; the caller
assembles them into a product feature).
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import date, datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.observability import EMBED_SECONDS, RERANK_SECONDS, RETRIEVAL_SECONDS, get_request_id

logger = logging.getLogger(__name__)
router = APIRouter()


def _blank_to_none(value: object) -> object:
    """Treat an empty string as "not provided" for an optional datetime filter.

    Caught live: a strict-JSON-schema tool-calling model (gpt-5.6-luna over /v1/responses) fills
    every declared property rather than omitting the ones it doesn't want to filter on, and its
    placeholder for "no value" on an optional string-typed field is `""`, not `null` — a real
    difference in how strict function-calling models express "unset" versus the local/Anthropic paths
    this endpoint was originally exercised against. `""` fails `datetime` validation outright, so
    without this the whole query 422s instead of just ignoring the (non-)filter.
    """
    return None if value == "" else value


@router.get("/healthz")
async def healthz(request: Request) -> dict:
    """Liveness + a quick DB ping. 200 with consumer/DB status; used by Docker/K8s probes."""
    consumer = request.app.state.consumer
    store = request.app.state.store
    db_ok = True
    try:
        await store.count()
    except Exception:  # noqa: BLE001
        db_ok = False
    return {
        "status": "ok" if (db_ok and consumer.stats.running) else "degraded",
        "consumer_running": consumer.stats.running,
        "db_ok": db_ok,
    }


@router.get("/stats")
async def stats(request: Request) -> dict:
    """Ingestion counters + current vector count, for dashboards and debugging."""
    consumer = request.app.state.consumer
    store = request.app.state.store
    s = consumer.stats
    try:
        vector_count = await store.count()
    except Exception:  # noqa: BLE001
        vector_count = None
    return {
        "topics": s.topics,
        "processed": s.processed,
        "skipped_poison": s.skipped_poison,
        "failed": s.failed,
        "last_error": s.last_error,
        "vector_count": vector_count,
    }


class EmbedRequest(BaseModel):
    text: str = Field(min_length=1)


class EmbedResponse(BaseModel):
    embedding: List[float]
    dim: int


@router.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest, request: Request) -> EmbedResponse:
    """Expose the raw embedding primitive this service already owns.

    Used by ai-service's semantic query cache (app/cache/semantic_cache.py) to embed incoming
    questions for similarity matching against previously-answered ones — deliberately reusing this
    service's existing embedder rather than duplicating embedding-provider code/credentials in
    ai-service. Same "index owner exposes primitives" split as /search.
    """
    embedder = request.app.state.embedder
    (vector,) = await embedder.embed([req.text])
    return EmbedResponse(embedding=vector, dim=len(vector))


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, description="Natural-language or keyword query.")
    space_ids: List[int] = Field(min_length=1, description="Caller's authorized spaces (permission scope).")
    limit: int = Field(default=5, ge=1, le=50)
    mode: Literal["hybrid", "vector", "lexical"] = "hybrid"


class SearchHitOut(BaseModel):
    id: str
    chunk_type: str
    issue_id: int
    issue_key: str
    space_id: int
    source_id: int
    content: str
    score: float
    retrievers: List[str]
    page_number: int | None = None
    provenance: dict = Field(default_factory=dict)


class SearchResponse(BaseModel):
    hits: List[SearchHitOut]
    mode: str
    reranked: bool = False


@router.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest, request: Request) -> SearchResponse:
    """Raw retrieval: embed the query, run vector / lexical / hybrid search, return ranked chunks.

    Callers (the ai-service's search_knowledge_base tool) get back chunk-level hits with enough
    metadata (issue_key, source_id, chunk_type) to build a citation, and ``retrievers`` to show which
    signal(s) found each result — useful for demoing/debugging hybrid fusion.

    When reranking is enabled (VEC_RERANK_ENABLED), first-stage retrieval fetches a wider candidate
    pool than ``req.limit`` so the cross-encoder has real room to reorder before truncating — see
    app/db/reranker.py.
    """
    store = request.app.state.store
    embedder = request.app.state.embedder
    reranker = request.app.state.reranker
    settings = request.app.state.settings

    rerank_active = settings.rerank_enabled
    fetch_limit = max(req.limit, settings.rerank_candidate_pool) if rerank_active else req.limit
    stage_ms: dict = {}

    def _stage(name: str):
        # One manual timer per stage: measured once, then both fed to the Prometheus histogram
        # (`.observe()`) and the structured log line below, so the metric and the log always agree
        # instead of measuring the same span twice with two separate clocks/mechanisms.
        return _StageTimer(name, stage_ms)

    try:
        if req.mode == "lexical":
            with _stage("retrieval") as t:
                hits = await store.search_lexical(req.query, req.space_ids, fetch_limit)
            RETRIEVAL_SECONDS.labels(req.mode).observe(t.seconds)
        else:
            with _stage("embed") as t:
                (embedding,) = await embedder.embed([req.query])
            EMBED_SECONDS.observe(t.seconds)

            with _stage("retrieval") as t:
                if req.mode == "vector":
                    hits = await store.search_vector(embedding, req.space_ids, fetch_limit)
                else:
                    hits = await store.search_hybrid(embedding, req.query, req.space_ids, fetch_limit)
            RETRIEVAL_SECONDS.labels(req.mode).observe(t.seconds)

        if rerank_active:
            with _stage("rerank") as t:
                hits = await reranker.rerank(req.query, hits, req.limit)
            RERANK_SECONDS.observe(t.seconds)
        else:
            hits = hits[: req.limit]
    except Exception as exc:  # noqa: BLE001 - surface as a clean 502, don't leak internals
        raise HTTPException(status_code=502, detail=f"search failed: {exc}") from exc

    logger.info(
        "search completed",
        extra={"request_id": get_request_id(), "mode": req.mode, "reranked": rerank_active,
               "hit_count": len(hits), "stage_ms": stage_ms},
    )
    return SearchResponse(
        hits=[SearchHitOut(**asdict(hit)) for hit in hits],
        mode=req.mode,
        reranked=rerank_active,
    )


class IssueQueryRequest(BaseModel):
    """Structured, non-semantic issue query — the counting/filtering/recency path.

    Deliberately NOT a natural-language query: the caller (ai-service's query_issues tool) has already
    translated the user's question into these exact filters, so this endpoint does plain SQL over the
    `issues` table, not embedding + similarity. That's what makes "how many bugs" answerable exactly
    instead of approximately.
    """

    space_ids: List[int] = Field(min_length=1, description="Caller's authorized spaces (permission scope).")
    issue_keys: Optional[List[str]] = Field(None, description="Restrict to these exact issue keys.")
    issue_types: Optional[List[str]] = Field(None, description="Filter to these issue types, e.g. ['bug'].")
    statuses: Optional[List[str]] = Field(None, description="Filter to these statuses, e.g. ['blocked'].")
    priorities: Optional[List[str]] = Field(None, description="Filter to these priorities, e.g. ['high'].")
    sprint_ids: Optional[List[int]] = Field(None, description="Filter to issues in these sprints, by id.")
    sprint_names: Optional[List[str]] = Field(None, description="Filter to issues in these sprints, by name.")
    created_after: Optional[datetime] = None
    created_before: Optional[datetime] = None
    updated_after: Optional[datetime] = None
    updated_before: Optional[datetime] = None
    # Constrained, not free text — this is what gets formatted into the ORDER BY, so it must be a
    # closed enum (see VectorStore.query_issues' injection note).
    order_by: Literal["created_at", "updated_at"] = "updated_at"
    order: Literal["asc", "desc"] = "desc"
    limit: int = Field(default=20, ge=1, le=200)

    _blank_dates = field_validator(
        "created_after", "created_before", "updated_after", "updated_before", mode="before"
    )(_blank_to_none)


class IssueRowOut(BaseModel):
    issue_id: int
    issue_key: str
    space_id: int
    issue_type: Optional[str]
    status: Optional[str]
    priority: Optional[str]
    sprint_id: Optional[int]
    sprint_name: Optional[str]
    title: Optional[str]
    parent_key: Optional[str]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


class IssueQueryResponse(BaseModel):
    total_count: int
    counts_by_type: dict
    counts_by_status: dict
    issues: List[IssueRowOut]


@router.post("/issues/query", response_model=IssueQueryResponse)
async def query_issues(req: IssueQueryRequest, request: Request) -> IssueQueryResponse:
    """Structured issue query: exact count + type/status breakdown + an ordered, limited sample.

    The deterministic counterpart to /search. /search answers "what content is relevant"; this
    answers "how many / which ones / most recent", which top-K similarity cannot do honestly. Same
    space-scoped permission boundary as /search — a caller only ever sees issues in the spaces it
    passes, and (as with /search) the ai-service loop injects those from the authenticated identity,
    never from the model.
    """
    store = request.app.state.store
    try:
        result = await store.query_issues(
            req.space_ids,
            issue_keys=req.issue_keys,
            issue_types=req.issue_types,
            statuses=req.statuses,
            priorities=req.priorities,
            sprint_ids=req.sprint_ids,
            sprint_names=req.sprint_names,
            created_after=req.created_after,
            created_before=req.created_before,
            updated_after=req.updated_after,
            updated_before=req.updated_before,
            order_by=req.order_by,
            descending=(req.order == "desc"),
            limit=req.limit,
        )
    except Exception as exc:  # noqa: BLE001 - same clean-502 contract as /search
        raise HTTPException(status_code=502, detail=f"issue query failed: {exc}") from exc

    logger.info(
        "issue query completed",
        extra={"request_id": get_request_id(), "total_count": result.total_count,
               "returned": len(result.rows)},
    )
    return IssueQueryResponse(
        total_count=result.total_count,
        counts_by_type=result.counts_by_type,
        counts_by_status=result.counts_by_status,
        issues=[IssueRowOut(**asdict(r)) for r in result.rows],
    )


class IssueHistoryRequest(BaseModel):
    """Query over the append-only change stream (migrations/005) — transitions, not current state."""

    space_ids: List[int] = Field(min_length=1, description="Caller's authorized spaces (permission scope).")
    issue_keys: Optional[List[str]] = Field(None, description="Restrict to these issues, e.g. ['ATC-77'].")
    fields: Optional[List[str]] = Field(None, description="Restrict to changes of these fields, e.g. ['status','description'].")
    event_types: Optional[List[str]] = Field(None, description="Restrict to these history kinds, e.g. ['field_change'].")
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    # First-class because it's the transition question people actually ask, and its SQL shape
    # (from='done' AND to<>'done') isn't expressible through the generic filters above.
    reopened_only: bool = False
    limit: int = Field(default=20, ge=1, le=200)

    _blank_dates = field_validator("since", "until", mode="before")(_blank_to_none)


class IssueChangeOut(BaseModel):
    issue_key: str
    event_type: str
    field_name: Optional[str]
    from_value: Optional[str]
    to_value: Optional[str]
    description: Optional[str]
    actor_name: Optional[str]
    changed_at: datetime


class IssueHistoryResponse(BaseModel):
    total_count: int
    changes: List[IssueChangeOut]


@router.post("/issues/history", response_model=IssueHistoryResponse)
async def issue_history(req: IssueHistoryRequest, request: Request) -> IssueHistoryResponse:
    """The transitions counterpart to /issues/query: who changed what, when, from → to.

    Answers the questions a latest-state snapshot destroys by design: "which issues were reopened"
    (status left 'done'), "what did I change in ATC-77 ten minutes ago" (field-level from/to values,
    including title/description edits). Same space-scoped permission boundary as every other read.
    """
    store = request.app.state.store
    try:
        result = await store.query_issue_changes(
            req.space_ids,
            issue_keys=req.issue_keys,
            fields=req.fields,
            event_types=req.event_types,
            since=req.since,
            until=req.until,
            reopened_only=req.reopened_only,
            limit=req.limit,
        )
    except Exception as exc:  # noqa: BLE001 - same clean-502 contract as /search
        raise HTTPException(status_code=502, detail=f"issue history query failed: {exc}") from exc

    logger.info(
        "issue history query completed",
        extra={"request_id": get_request_id(), "total_count": result.total_count,
               "returned": len(result.changes), "reopened_only": req.reopened_only},
    )
    return IssueHistoryResponse(
        total_count=result.total_count,
        changes=[
            IssueChangeOut(
                issue_key=c.issue_key, event_type=c.event_type, field_name=c.field_name,
                from_value=c.from_value, to_value=c.to_value, description=c.description,
                actor_name=c.actor_name, changed_at=c.changed_at,
            )
            for c in result.changes
        ],
    )


class IssueCommentsRequest(BaseModel):
    """Complete, unranked comment fetch for specific issues — see VectorStore.get_issue_comments."""

    space_ids: List[int] = Field(min_length=1, description="Caller's authorized spaces (permission scope).")
    issue_keys: List[str] = Field(min_length=1, description="Fetch every comment on these issues, e.g. ['ATC-30'].")
    limit: int = Field(default=200, ge=1, le=500)


class IssueCommentOut(BaseModel):
    issue_id: int
    issue_key: str
    source_id: int
    content: str


class IssueCommentsResponse(BaseModel):
    total_count: int
    comments: List[IssueCommentOut]


@router.post("/issues/comments", response_model=IssueCommentsResponse)
async def issue_comments(req: IssueCommentsRequest, request: Request) -> IssueCommentsResponse:
    """The deterministic fallback to /search: every comment on named issues, not a semantic top-K.

    Exists because a hybrid/vector search's top-K can bury the one comment that actually answers a
    question (a reopen reason, a decision) among many comments on the same issue — found live, see
    app/agent/crag_loop.py's get_issue_comments tool docstring. This is a plain exact-match SELECT
    against `chunks`, no embedding call, so it cannot miss a comment that's really there.
    """
    store = request.app.state.store
    try:
        result = await store.get_issue_comments(req.space_ids, req.issue_keys, limit=req.limit)
    except Exception as exc:  # noqa: BLE001 - same clean-502 contract as /search
        raise HTTPException(status_code=502, detail=f"issue comments query failed: {exc}") from exc

    logger.info(
        "issue comments fetch completed",
        extra={"request_id": get_request_id(), "total_count": result.total_count,
               "returned": len(result.comments), "issue_keys": req.issue_keys},
    )
    return IssueCommentsResponse(
        total_count=result.total_count,
        comments=[
            IssueCommentOut(issue_id=c.issue_id, issue_key=c.issue_key, source_id=c.source_id, content=c.content)
            for c in result.comments
        ],
    )


class IssueDetailsRequest(BaseModel):
    """Complete, unranked issue-body fetch for specific issues — see VectorStore.get_issue_details."""

    space_ids: List[int] = Field(min_length=1, description="Caller's authorized spaces (permission scope).")
    issue_keys: List[str] = Field(min_length=1, description="Fetch the title+description of these issues, e.g. ['ATC-43'].")
    limit: int = Field(default=200, ge=1, le=500)


class IssueDetailOut(BaseModel):
    issue_id: int
    issue_key: str
    source_id: int
    content: str


class IssueDetailsResponse(BaseModel):
    total_count: int
    details: List[IssueDetailOut]


@router.post("/issues/details", response_model=IssueDetailsResponse)
async def issue_details(req: IssueDetailsRequest, request: Request) -> IssueDetailsResponse:
    """The `chunk_type='issue'` counterpart to /issues/comments: an issue's own title+description,
    fetched by exact key, not a semantic top-K.

    Exists because an issue with many comments can have its own single title+description chunk rank
    *below* several of those comments — `ts_rank_cd` has no length normalization, so a short chunk
    that mentions its key once doesn't automatically out-rank comments that also mention it (found
    live, see docs/RAG_ACCURACY_CASE_STUDIES.md Case Study 11). This is a plain exact-match SELECT
    against `chunks`, no embedding call, so an issue's own body cannot be missed as long as the key
    is right — the same guarantee /issues/comments already gives for comments.
    """
    store = request.app.state.store
    try:
        result = await store.get_issue_details(req.space_ids, req.issue_keys, limit=req.limit)
    except Exception as exc:  # noqa: BLE001 - same clean-502 contract as /search
        raise HTTPException(status_code=502, detail=f"issue details query failed: {exc}") from exc

    logger.info(
        "issue details fetch completed",
        extra={"request_id": get_request_id(), "total_count": result.total_count,
               "returned": len(result.details), "issue_keys": req.issue_keys},
    )
    return IssueDetailsResponse(
        total_count=result.total_count,
        details=[
            IssueDetailOut(issue_id=d.issue_id, issue_key=d.issue_key, source_id=d.source_id, content=d.content)
            for d in result.details
        ],
    )


class IssueAttachmentsRequest(BaseModel):
    """Complete, unranked attachment-text fetch for specific issues — see
    VectorStore.get_issue_attachments."""

    space_ids: List[int] = Field(min_length=1, description="Caller's authorized spaces (permission scope).")
    issue_keys: List[str] = Field(min_length=1, description="Fetch every attachment chunk on these issues, e.g. ['ATC-46'].")
    limit: int = Field(default=200, ge=1, le=500)


class IssueAttachmentOut(BaseModel):
    issue_id: int
    issue_key: str
    source_id: int
    content: str
    page_number: int | None = None
    provenance: dict = Field(default_factory=dict)


class IssueAttachmentsResponse(BaseModel):
    total_count: int
    attachments: List[IssueAttachmentOut]


@router.post("/issues/attachments", response_model=IssueAttachmentsResponse)
async def issue_attachments(req: IssueAttachmentsRequest, request: Request) -> IssueAttachmentsResponse:
    """The `chunk_type='attachment'` counterpart to /issues/comments and /issues/details: every
    parsed-attachment chunk for named issues, fetched by exact key, not a semantic top-K.

    Exists because a semantic/hybrid query can't reliably be phrased to find a specific fact (an
    exact SKU, an ID) it doesn't already know the wording of — found live, "what SKU does ATC-46's
    attachment use" flipped between finding the answer and abstaining across otherwise-equivalent
    phrasings of the same question, even though the fact was always in the index (see
    docs/RAG_ACCURACY_CASE_STUDIES.md). This is a plain exact-match SELECT against `chunks`, no
    embedding call, so an issue's attachment content cannot be missed once its key is known.
    """
    store = request.app.state.store
    try:
        result = await store.get_issue_attachments(req.space_ids, req.issue_keys, limit=req.limit)
    except Exception as exc:  # noqa: BLE001 - same clean-502 contract as /search
        raise HTTPException(status_code=502, detail=f"issue attachments query failed: {exc}") from exc

    logger.info(
        "issue attachments fetch completed",
        extra={"request_id": get_request_id(), "total_count": result.total_count,
               "returned": len(result.attachments), "issue_keys": req.issue_keys},
    )
    return IssueAttachmentsResponse(
        total_count=result.total_count,
        attachments=[
            IssueAttachmentOut(
                issue_id=a.issue_id,
                issue_key=a.issue_key,
                source_id=a.source_id,
                content=a.content,
                page_number=a.page_number,
                provenance=a.provenance,
            )
            for a in result.attachments
        ],
    )


class SprintQueryRequest(BaseModel):
    """Structured, non-semantic sprint query — the counterpart to IssueQueryRequest, over `sprints`
    (migrations/006). Answers "how many sprints", "which is active", velocity/points questions."""

    space_ids: List[int] = Field(min_length=1, description="Caller's authorized spaces (permission scope).")
    statuses: Optional[List[str]] = Field(None, description="Filter to these statuses, e.g. ['active'].")
    order_by: Literal["start_date", "end_date"] = "start_date"
    order: Literal["asc", "desc"] = "desc"
    limit: int = Field(default=20, ge=1, le=200)


class SprintRowOut(BaseModel):
    sprint_id: int
    sprint_name: str
    space_id: int
    goal: Optional[str]
    start_date: Optional[date]
    end_date: Optional[date]
    status: Optional[str]
    initial_committed_points: Optional[int]
    initial_completed_points: Optional[int]
    final_scope_points: Optional[int]
    completed_points: Optional[int]
    initial_issue_count: Optional[int]
    completed_issue_count: Optional[int]
    final_issue_count: Optional[int]
    unestimated_issue_count: Optional[int]


class SprintQueryResponse(BaseModel):
    total_count: int
    counts_by_status: dict
    sprints: List[SprintRowOut]


@router.post("/sprints/query", response_model=SprintQueryResponse)
async def query_sprints(req: SprintQueryRequest, request: Request) -> SprintQueryResponse:
    """Structured sprint query: exact count + status breakdown + an ordered, limited sample.

    Same space-scoped permission boundary and "deterministic SQL, not semantic search" contract as
    /issues/query — see that endpoint's docstring for the full reasoning.
    """
    store = request.app.state.store
    try:
        result = await store.query_sprints(
            req.space_ids,
            statuses=req.statuses,
            order_by=req.order_by,
            descending=(req.order == "desc"),
            limit=req.limit,
        )
    except Exception as exc:  # noqa: BLE001 - same clean-502 contract as /search
        raise HTTPException(status_code=502, detail=f"sprint query failed: {exc}") from exc

    logger.info(
        "sprint query completed",
        extra={"request_id": get_request_id(), "total_count": result.total_count,
               "returned": len(result.rows)},
    )
    return SprintQueryResponse(
        total_count=result.total_count,
        counts_by_status=result.counts_by_status,
        sprints=[SprintRowOut(**asdict(r)) for r in result.rows],
    )


class _StageTimer:
    """Times one `with` block; records elapsed ms into `stage_ms[name]` and exposes `.seconds`."""

    def __init__(self, name: str, stage_ms: dict):
        self._name = name
        self._stage_ms = stage_ms
        self.seconds = 0.0

    def __enter__(self) -> "_StageTimer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc_info) -> None:
        self.seconds = time.perf_counter() - self._start
        self._stage_ms[self._name] = round(self.seconds * 1000, 2)
