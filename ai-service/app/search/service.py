"""Collapses vectorization-service's chunk-level retrieval hits down to issue-level results.

Both consumers of this — the "find related issues" search mode and duplicate-issue detection — want
"is there already an issue about this," not a passage to cite in a sentence, so several chunk hits for
the same issue should become one result (its best-scoring chunk), not a duplicate row per chunk.

Callers of this function MUST pass chunks retrieved in `mode="vector"` (raw cosine similarity, a real
0-1 confidence — see `vectorization-service/app/db/vector_store.py::search_vector`), not
`mode="hybrid"` (Reciprocal Rank Fusion — see `app/db/rrf.py` — a rank-combination score with no
absolute meaning, typically maxing around 0.03). `min_score` below is only a meaningful filter against
the former; applying it to an RRF score would silently drop everything.

`attachment` chunks are excluded by default — found live, not assumed: OCR'd screenshot chunks in real
ingested data are frequently near-templated boilerplate (e.g. this project's own AtlasCart/M3 demo
data, where ~28.5% of one space's chunks are attachment OCR text sharing heavy phrase overlap like
"SYNTHETIC STAGING SCREENSHOT" across dozens of unrelated issues). Measured directly against that
corpus: attachment chunks average 0.54 cosine similarity to *each other*, vs 0.47 for issue chunks and
0.44 for comment chunks — they cluster together regardless of the underlying issue's real topic, which
is exactly wrong for "is this a duplicate of an existing issue." Citations in /ask (a different,
richer answer with its own judgment) are unaffected — this exclusion is scoped to this dedupe path
only.

`min_score` exists because chunk-type exclusion alone doesn't guarantee everything left is actually
relevant — for a query with no real match in the corpus, dedupe would otherwise still hand back
whatever ranked highest among low-relevance candidates, presented with the same confidence as a real
hit. `DEFAULT_MIN_SCORE` (0.7) is a starting heuristic informed by a small manual sample against the
AtlasCart/M3 corpus (genuinely related-but-distinct issues clustered ~0.75-0.85; same-domain-but-
unrelated pairs clustered ~0.64-0.66) — not a rigorously calibrated number. Properly calibrating it
needs a labeled "these issues are duplicates of each other" query set and the same Hit@k/MRR
methodology `vectorization-service/eval/` already uses for retrieval quality — that doesn't exist yet
for this task.

`sprint` chunks are excluded for a structural reason, not a quality one: unlike every other chunk_type,
a sprint chunk's `issue_id`/`issue_key` are the sprint's own id/name, not a real issue (see Chunk's
docstring in vectorization-service/app/models.py) — those ids are drawn from a different sequence than
real issue ids and can coincidentally collide with one. Grouping/labeling a sprint-goal hit as if it
were "issue #N" here would be actively misleading for a feature whose whole contract is "is there
already an ISSUE about this." (The main /ask path is unaffected: its citations render `chunk_type`
explicitly, so a sprint hit reads as what it is.)

IMPORTANT — two incompatible score scales exist and callers must not mix them up: the number above
(0.7) is calibrated for raw cosine similarity (`mode="vector"`, reranking off), a real 0-1 value. When
vectorization-service reranks results with a cross-encoder (`VEC_RERANK_ENABLED`, a server-side
setting `mode` does not control), `score` becomes an *unbounded* cross-encoder logit instead — found
live against a real deployment with reranking on: a genuine "shipping quotes" match scored 5.39/1.71,
an unrelated issue on the same page scored -4.6, and a query with literally no matching content in the
corpus scored every candidate around -11.4. Applying 0.7 there wouldn't error, it would just silently
stop filtering anything (positive scores among genuine matches vastly exceed it, negative ones don't
even register as "below" in a meaningful way relative to that scale). `RERANKED_MIN_SCORE` (0.0) is
the calibrated-for-that-scale equivalent — "the model itself judged this relevant" — based on the
same live observation: real matches scored clearly positive, the no-real-match case scored uniformly,
deeply negative, with a wide margin either way. Also not rigorously calibrated (two data points, not
a labeled eval), but grounded in an actual production score distribution rather than a guess.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, List, Optional

from app.agent.retrieval_tool import RetrievedChunk

DEFAULT_EXCLUDED_CHUNK_TYPES: FrozenSet[str] = frozenset({"attachment", "sprint"})
DEFAULT_MIN_SCORE = 0.7
RERANKED_MIN_SCORE = 0.0


def dedupe_by_issue(
    chunks: List[RetrievedChunk],
    limit: int,
    exclude_chunk_types: Optional[FrozenSet[str]] = DEFAULT_EXCLUDED_CHUNK_TYPES,
    min_score: Optional[float] = DEFAULT_MIN_SCORE,
) -> List[RetrievedChunk]:
    best_by_issue: Dict[int, RetrievedChunk] = {}
    for chunk in chunks:
        if exclude_chunk_types and chunk.chunk_type in exclude_chunk_types:
            continue
        if min_score is not None and chunk.score < min_score:
            continue
        current = best_by_issue.get(chunk.issue_id)
        if current is None or chunk.score > current.score:
            best_by_issue[chunk.issue_id] = chunk
    ranked = sorted(best_by_issue.values(), key=lambda c: c.score, reverse=True)
    return ranked[:limit]
