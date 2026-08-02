"""Backfill query regressions — no source or vector database required."""

from scripts.backfill_jira_backend import _fetch_sprints


class FakePool:
    def __init__(self):
        self.query = None
        self.args = None

    async def fetch(self, query, *args):
        self.query = query
        self.args = args
        return []


async def test_sprint_backfill_fetches_all_structured_fields_and_scopes_spaces():
    pool = FakePool()

    await _fetch_sprints(pool, [5000014])

    assert "FROM sprints" in pool.query
    assert "initial_committed_points" in pool.query
    assert "unestimated_issue_count" in pool.query
    assert "space_id = ANY($1::bigint[])" in pool.query
    assert pool.args == ([5000014],)
