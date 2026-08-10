"""get_issue_details SQL construction — fake pool, no PostgreSQL required.

The chunk_type='issue' counterpart to test_issue_comments.py: complete, unranked fetch of an issue's
own title+description chunk(s) by key (see VectorStore.get_issue_details docstring). Exists because an
issue with many comments can have its own single body chunk rank below several of those comments in
both lexical and vector search.
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

    await store.get_issue_details([5000014], ["ATC-43"])

    for _, sql, args in pool.calls:
        assert "chunk_type = 'issue'" in sql
        assert "issue_key = ANY($2::text[])" in sql
        assert "space_id = ANY($1::bigint[])" in sql
        assert args[0] == [5000014]
        assert args[1] == ["ATC-43"]
    # Ordered by issue then position — a stable read order, not a relevance ranking.
    fetch_sql = next(sql for kind, sql, _ in pool.calls if kind == "fetch")
    assert "ORDER BY issue_key, chunk_index" in fetch_sql


async def test_result_maps_every_row_and_reports_the_exact_count():
    rows = [
        {"id": "issue:5000932", "issue_id": 5000932, "issue_key": "ATC-43", "source_id": 5000932,
         "content": "Checkout can create two orders from a double click. Context: during the "
                    "private beta, a slow checkout allowed two Place order clicks.", "chunk_index": 0},
    ]
    pool = FakePool(rows=rows)
    store = VectorStore(pool)

    result = await store.get_issue_details([5000014], ["ATC-43"])

    assert result.total_count == 1
    assert result.details[0].issue_key == "ATC-43"
    assert "double click" in result.details[0].content
