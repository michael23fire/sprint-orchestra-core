"""Structured issue-query SQL construction regressions — fake pool, no PostgreSQL required."""

from app.db.vector_store import VectorStore


class FakePool:
    def __init__(self):
        self.calls = []
        self.fetch_responses = [[], [], []]

    async def fetchval(self, sql, *args):
        self.calls.append(("fetchval", sql, args))
        return 0

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self.fetch_responses.pop(0)


async def test_issue_keys_and_statuses_share_the_same_parameterized_where_clause():
    pool = FakePool()
    store = VectorStore(pool)

    await store.query_issues(
        [5000014],
        issue_keys=["ATC-34", "ATC-55"],
        statuses=["blocked"],
    )

    count_call = pool.calls[0]
    assert "issue_key = ANY($2::text[])" in count_call[1]
    assert "status = ANY($3::text[])" in count_call[1]
    assert count_call[2] == ([5000014], ["ATC-34", "ATC-55"], ["blocked"])

    # Count, breakdowns, and row fetch must use the identical filters; otherwise the UI could receive
    # a total that disagrees with its rows/summaries.
    for _, sql, args in pool.calls[:-1]:
        assert "issue_key = ANY($2::text[])" in sql
        assert "status = ANY($3::text[])" in sql
        assert args == count_call[2]
    assert pool.calls[-1][2][:-1] == count_call[2]


async def test_priorities_filter_shares_the_same_parameterized_where_clause():
    pool = FakePool()
    store = VectorStore(pool)

    await store.query_issues([5000014], priorities=["high"])

    count_call = pool.calls[0]
    assert "priority = ANY($2::text[])" in count_call[1]
    assert count_call[2] == ([5000014], ["high"])
    for _, sql, args in pool.calls[:-1]:
        assert "priority = ANY($2::text[])" in sql
        assert args == count_call[2]
    assert pool.calls[-1][2][:-1] == count_call[2]
