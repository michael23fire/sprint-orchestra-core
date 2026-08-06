"""Backfill query regressions — no source or vector database required."""

from scripts.backfill_jira_backend import _fetch_issues, _fetch_sprints


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


async def test_issue_backfill_fetches_all_structured_fields_and_scopes_spaces():
    # Regression test: _fetch_issues used to select only id/key/space_id/title/description/updated_at/
    # sprint fields — omitting issue_type, status, created_at, updated_at, and parent_key entirely.
    # IssueIngestionMessage defaults every missing field to None, and upsert_issue's ON CONFLICT
    # unconditionally overwrites with whatever the message carries — so every full backfill run
    # silently NULLed out already-correct issue_type/status/created_at/updated_at/parent_key for every
    # issue already in the vector store. Found live: query_issues(issue_types=['bug']) returned 0 for
    # a space with 11 real bugs, right after running --include-attachments (see
    # docs/RAG_ACCURACY_CASE_STUDIES.md).
    pool = FakePool()

    await _fetch_issues(pool, [5000014])

    assert "FROM issues" in pool.query
    assert "i.issue_type" in pool.query
    assert "i.status" in pool.query
    assert "i.priority" in pool.query
    assert "i.created_at" in pool.query
    assert "i.updated_at" in pool.query
    assert "parent_key" in pool.query
    assert "space_id = ANY($1::bigint[])" in pool.query
    assert pool.args == ([5000014],)
