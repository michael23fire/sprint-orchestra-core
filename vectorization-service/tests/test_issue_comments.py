"""get_issue_comments SQL construction — fake pool, no PostgreSQL required.

The deterministic fallback to /search: complete, unranked comment fetch for named issues (see
VectorStore.get_issue_comments docstring). These tests only check the query shape/params, the same
level test_structured_issue_query.py already tests query_issues at.
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

    await store.get_issue_comments([5000014], ["ATC-30", "ATC-68"])

    for _, sql, args in pool.calls:
        assert "chunk_type = 'comment'" in sql
        assert "issue_key = ANY($2::text[])" in sql
        assert "space_id = ANY($1::bigint[])" in sql
        assert args[0] == [5000014]
        assert args[1] == ["ATC-30", "ATC-68"]
    # Ordered by issue then position — a stable read order, not a relevance ranking.
    fetch_sql = next(sql for kind, sql, _ in pool.calls if kind == "fetch")
    assert "ORDER BY issue_key, chunk_index" in fetch_sql


async def test_result_maps_every_row_and_reports_the_exact_count():
    rows = [
        {"id": "comment:1", "issue_id": 30, "issue_key": "ATC-30", "source_id": 1,
         "content": "Reopening this because...", "chunk_index": 0},
        {"id": "comment:2", "issue_id": 30, "issue_key": "ATC-30", "source_id": 2,
         "content": "Verification result PASS", "chunk_index": 1},
    ]
    pool = FakePool(rows=rows)
    store = VectorStore(pool)

    result = await store.get_issue_comments([5000014], ["ATC-30"])

    assert result.total_count == 2
    assert [c.content for c in result.comments] == ["Reopening this because...", "Verification result PASS"]
    assert result.comments[0].issue_key == "ATC-30"
