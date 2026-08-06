"""Thin client for vectorization-service's raw retrieval API (POST /search).

The ai-service is a *caller* of that API, not an owner of the index — same ingestion-vs-retrieval
split discussed for FTS: the service that owns the data exposes primitives, the service that owns
the product feature (answering a question) composes them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import httpx

from app.observability import get_request_id


@dataclass(slots=True)
class RetrievedChunk:
    id: str
    chunk_type: str
    issue_id: int
    issue_key: str
    source_id: int
    content: str
    score: float
    retrievers: List[str]
    # 1-based source PDF page when parser provenance is available; null for non-page sources and
    # legacy chunks. Preserved end-to-end so UI citations can deep-link to the right PDF page.
    page_number: Optional[int] = None
    provenance: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class IssueQueryResult:
    """Result of a structured (non-semantic) issue query — see vectorization-service /issues/query.

    `total_count` and the breakdowns count every match; `issues` is only the ordered, limited sample.
    This is the exact-count/filter/recency path the agent uses instead of pretending semantic search
    can count or enumerate completely.
    """

    total_count: int
    counts_by_type: dict
    counts_by_status: dict
    issues: List[dict]


@dataclass(slots=True)
class IssueHistoryResult:
    """Result of a change-history query — see vectorization-service /issues/history. `changes` are
    raw dicts (issue_key, event_type, field_name, from_value, to_value, actor_name, changed_at)."""

    total_count: int
    changes: List[dict]


@dataclass(slots=True)
class IssueCommentsResult:
    """Result of a complete, unranked comment fetch for named issues — see vectorization-service
    /issues/comments. The deterministic fallback to search_knowledge_base's top-K when a specific
    detail (a reason, a decision) might be in a comment that didn't rank. `comments` are raw dicts
    (id, issue_id, issue_key, source_id, content)."""

    total_count: int
    comments: List[dict]


@dataclass(slots=True)
class IssueDetailsResult:
    """Result of a complete, unranked fetch of issues' own title+description — see
    vectorization-service's /issues/details. The `chunk_type='issue'` counterpart to
    IssueCommentsResult: the deterministic fallback to search_knowledge_base's top-K when an issue's
    own body (not its comments) is the thing that might not have ranked. `details` are raw dicts
    (issue_id, issue_key, source_id, content)."""

    total_count: int
    details: List[dict]


@dataclass(slots=True)
class IssueAttachmentsResult:
    """Result of a complete, unranked fetch of issues' attachment text — see vectorization-service's
    /issues/attachments. The `chunk_type='attachment'` counterpart to IssueCommentsResult/
    IssueDetailsResult: the deterministic fallback to search_knowledge_base when a specific fact
    inside an attachment (an exact SKU, an ID) can't reliably be found by a semantic/hybrid query that
    doesn't already know its wording. `attachments` are raw dicts (issue_id, issue_key, source_id,
    content)."""

    total_count: int
    attachments: List[dict]


@dataclass(slots=True)
class SprintQueryResult:
    """Result of a structured (non-semantic) sprint query — see vectorization-service
    /sprints/query. Counterpart to IssueQueryResult, over sprints (name/goal/dates/status/velocity)
    instead of issues."""

    total_count: int
    counts_by_status: dict
    sprints: List[dict]


@dataclass(slots=True)
class SearchResult:
    hits: List[RetrievedChunk]
    # Whether vectorization-service applied cross-encoder reranking (VEC_RERANK_ENABLED, a
    # server-side setting this client doesn't control and `mode` doesn't affect — see
    # vectorization-service/app/api/routes.py). Callers that need to interpret `score` numerically
    # MUST check this: when true, `score` is an unbounded cross-encoder logit (e.g. ms-marco-MiniLM
    # routinely scores a strong match ~5, a weak one ~1-2, negative for irrelevant pairs) — nothing
    # like the 0-1 range of raw cosine similarity returned when reranking is off. Found live: a
    # `min_score` filter calibrated against real cosine similarity (see app/search/service.py) does
    # nothing useful against this scale — it doesn't error, it just silently stops being a filter.
    reranked: bool


class RetrievalClient:
    def __init__(self, base_url: str):
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=30.0)

    async def search(
        self, query: str, space_ids: Sequence[int], limit: int, mode: str = "hybrid"
    ) -> List[RetrievedChunk]:
        """Used by CragAgent, which only ever consumes hits for citations/context — it doesn't apply
        an absolute score threshold, so the reranked-vs-cosine scale distinction doesn't matter to it.
        Callers that DO need to interpret `score` numerically should use `search_with_meta` instead.
        """
        result = await self.search_with_meta(query, space_ids, limit, mode)
        return result.hits

    async def search_with_meta(
        self, query: str, space_ids: Sequence[int], limit: int, mode: str = "hybrid"
    ) -> SearchResult:
        # Forwards this request's correlation id downstream so a log line in vectorization-service
        # can be tied back to the /ask call that triggered it — partial cross-service correlation
        # without full distributed tracing (see docs/ARCHITECTURE.md §6 for what that would still add).
        resp = await self._client.post(
            "/search",
            json={"query": query, "space_ids": list(space_ids), "limit": limit, "mode": mode},
            headers={"x-request-id": get_request_id()},
        )
        resp.raise_for_status()
        body = resp.json()
        hits = [
            RetrievedChunk(
                id=h["id"], chunk_type=h["chunk_type"], issue_id=h["issue_id"], issue_key=h["issue_key"],
                source_id=h["source_id"], content=h["content"], score=h["score"], retrievers=h["retrievers"],
                page_number=h.get("page_number"),
                provenance=h.get("provenance") or {},
            )
            for h in body["hits"]
        ]
        return SearchResult(hits=hits, reranked=body.get("reranked", False))

    async def query_issues(
        self, space_ids: Sequence[int], filters: dict
    ) -> IssueQueryResult:
        """Structured issue query against vectorization-service /issues/query (count/filter/recency).

        `space_ids` is injected by the agent loop from the authenticated caller — NEVER taken from the
        model — exactly like the search path's permission scoping. `filters` carries only the
        model-controllable fields (issue_types, statuses, date bounds, order_by/order, limit); the
        loop merges space_ids in here, so even a prompt-injected model can't widen its own scope.
        """
        payload = {"space_ids": list(space_ids), **filters}
        resp = await self._client.post(
            "/issues/query", json=payload, headers={"x-request-id": get_request_id()}
        )
        resp.raise_for_status()
        body = resp.json()
        return IssueQueryResult(
            total_count=body["total_count"],
            counts_by_type=body.get("counts_by_type", {}),
            counts_by_status=body.get("counts_by_status", {}),
            issues=body.get("issues", []),
        )

    async def query_issue_history(
        self, space_ids: Sequence[int], filters: dict
    ) -> IssueHistoryResult:
        """Change-history query against vectorization-service /issues/history. Same security shape as
        query_issues: space_ids injected here from the authenticated caller, never model-supplied."""
        payload = {"space_ids": list(space_ids), **filters}
        resp = await self._client.post(
            "/issues/history", json=payload, headers={"x-request-id": get_request_id()}
        )
        resp.raise_for_status()
        body = resp.json()
        return IssueHistoryResult(
            total_count=body["total_count"],
            changes=body.get("changes", []),
        )

    async def get_issue_comments(
        self, space_ids: Sequence[int], issue_keys: Sequence[str], limit: int = 200
    ) -> IssueCommentsResult:
        """Complete, unranked comment fetch against vectorization-service /issues/comments. Same
        security shape as query_issues: space_ids injected here from the authenticated caller, never
        model-supplied."""
        payload = {"space_ids": list(space_ids), "issue_keys": list(issue_keys), "limit": limit}
        resp = await self._client.post(
            "/issues/comments", json=payload, headers={"x-request-id": get_request_id()}
        )
        resp.raise_for_status()
        body = resp.json()
        return IssueCommentsResult(
            total_count=body["total_count"],
            comments=body.get("comments", []),
        )

    async def get_issue_details(
        self, space_ids: Sequence[int], issue_keys: Sequence[str], limit: int = 200
    ) -> IssueDetailsResult:
        """Complete, unranked issue-body fetch against vectorization-service /issues/details. Same
        security shape as query_issues: space_ids injected here from the authenticated caller, never
        model-supplied."""
        payload = {"space_ids": list(space_ids), "issue_keys": list(issue_keys), "limit": limit}
        resp = await self._client.post(
            "/issues/details", json=payload, headers={"x-request-id": get_request_id()}
        )
        resp.raise_for_status()
        body = resp.json()
        return IssueDetailsResult(
            total_count=body["total_count"],
            details=body.get("details", []),
        )

    async def get_issue_attachments(
        self, space_ids: Sequence[int], issue_keys: Sequence[str], limit: int = 200
    ) -> IssueAttachmentsResult:
        """Complete, unranked attachment-text fetch against vectorization-service /issues/attachments.
        Same security shape as query_issues: space_ids injected here from the authenticated caller,
        never model-supplied."""
        payload = {"space_ids": list(space_ids), "issue_keys": list(issue_keys), "limit": limit}
        resp = await self._client.post(
            "/issues/attachments", json=payload, headers={"x-request-id": get_request_id()}
        )
        resp.raise_for_status()
        body = resp.json()
        return IssueAttachmentsResult(
            total_count=body["total_count"],
            attachments=body.get("attachments", []),
        )

    async def query_sprints(
        self, space_ids: Sequence[int], filters: dict
    ) -> SprintQueryResult:
        """Structured sprint query against vectorization-service /sprints/query. Same security shape
        as query_issues: space_ids injected here from the authenticated caller, never model-supplied."""
        payload = {"space_ids": list(space_ids), **filters}
        resp = await self._client.post(
            "/sprints/query", json=payload, headers={"x-request-id": get_request_id()}
        )
        resp.raise_for_status()
        body = resp.json()
        return SprintQueryResult(
            total_count=body["total_count"],
            counts_by_status=body.get("counts_by_status", {}),
            sprints=body.get("sprints", []),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
