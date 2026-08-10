"""get_issue_attachments SQL construction — fake pool, no PostgreSQL required.

The chunk_type='attachment' counterpart to test_issue_comments.py/test_issue_details.py: complete,
unranked fetch of an issue's attachment chunk(s) by key (see VectorStore.get_issue_attachments
docstring). Exists because a semantic/hybrid search query can't reliably be phrased to find a specific
fact it doesn't already know the exact wording of (an exact SKU, an ID) — found live, this made
"what SKU does ATC-46's attachment use" flip between finding the answer and abstaining across
otherwise-equivalent phrasings, even though the fact was always in the index.
"""
from app.db.vector_store import VectorStore


class FakePool:
    def __init__(self, rows=None):
        self.calls = []
        self._rows = rows if rows is not None else []

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return len(self._rows)

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self._rows


async def test_filters_by_space_and_issue_key_with_no_ranking():
    pool = FakePool()
    store = VectorStore(pool)

    await store.get_issue_attachments([5000014], ["ATC-46"])

    for _, sql, args in pool.calls:
        assert "chunk_type = 'attachment'" in sql
        assert "issue_key = ANY($2::text[])" in sql
        assert "space_id = ANY($1::bigint[])" in sql
        assert args[0] == [5000014]
        assert args[1] == ["ATC-46"]
    # Ordered by issue then position — a stable read order, not a relevance ranking.
    fetch_sql = next(sql for kind, sql, _ in pool.calls if kind == "fetch")
    assert "page_number" in fetch_sql
    assert "ORDER BY issue_key, page_number NULLS FIRST, chunk_index" in fetch_sql


async def test_result_maps_every_row_and_reports_the_exact_count():
    rows = [
        {"id": "attachment:5000940#0", "issue_id": 5000935, "issue_key": "ATC-46", "source_id": 5000940,
         "content": "Inventory correction requirement... SKU A-104, order BETA-1043.", "chunk_index": 0,
         "page_number": 2, "provenance": {"source_type": "pdf", "page_number": 2}},
        {"id": "attachment:5000940#1", "issue_id": 5000935, "issue_key": "ATC-46", "source_id": 5000940,
         "content": "Acceptance criteria and exclusions...", "chunk_index": 1, "page_number": None,
         "provenance": {}},
    ]
    pool = FakePool(rows=rows)
    store = VectorStore(pool)

    result = await store.get_issue_attachments([5000014], ["ATC-46"])

    assert result.total_count == 2
    assert result.attachments[0].issue_key == "ATC-46"
    assert "A-104" in result.attachments[0].content
    assert result.attachments[0].page_number == 2
