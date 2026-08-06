"""Idempotent execution of the 4 representative recovery-action types against jira-backend.

Deliberately 4, not every conceivable Jira mutation: `link_dependency`, `change_priority`,
`move_out_of_sprint`, `add_comment` — enough heterogeneity to make the idempotency ledger a real
per-action-type concern (unlike `app/planning/jira_commit_client.py`'s single homogeneous "create an
issue" action), without building a general-purpose action-allowlist framework for its own sake.

Same gateway-internal-token + X-User-Id/X-Username pattern established in
`app/planning/jira_commit_client.py` / `app/auth/space_membership.py`.
"""
from __future__ import annotations

from typing import Optional

import httpx

from app.sprint_recovery.schemas import RecoveryAction


class JiraActionError(Exception):
    """An action call to jira-backend failed. Callers must not guess at partial success."""


class JiraActionsClient:
    def __init__(self, jira_backend_url: str, internal_gateway_token: str, timeout: float = 10.0):
        self._client = httpx.AsyncClient(
            base_url=jira_backend_url.rstrip("/"),
            headers={"X-Gateway-Internal": internal_gateway_token},
            timeout=timeout,
        )

    def _headers(self, user_id: str, username: str) -> dict:
        return {"X-User-Id": user_id, "X-Username": username} if user_id else {}

    async def _resolve_issue_id(self, space_id: int, issue_key: str, user_id: str, username: str) -> int:
        resp = await self._client.get(
            f"/api/spaces/{space_id}/issues/{issue_key}", headers=self._headers(user_id, username),
        )
        resp.raise_for_status()
        return resp.json()["id"]

    async def execute(
        self, space_id: int, action: RecoveryAction, user_id: str, username: str,
    ) -> str:
        """Executes exactly one action, returns a short human-readable result summary. Raises
        `JiraActionError` on any failure — the caller (commit_one_node) never records a failed
        attempt in the idempotency ledger, so a resume/retry re-attempts the exact same action.
        """
        try:
            if action.action_type == "link_dependency":
                return await self._link_dependency(space_id, action, user_id, username)
            if action.action_type == "change_priority":
                return await self._change_priority(space_id, action, user_id, username)
            if action.action_type == "move_out_of_sprint":
                return await self._move_out_of_sprint(space_id, action, user_id, username)
            if action.action_type == "add_comment":
                return await self._add_comment(space_id, action, user_id, username)
            raise JiraActionError(f"unknown action_type: {action.action_type}")
        except httpx.HTTPError as exc:
            raise JiraActionError(f"{action.action_type} on {action.target_issue_key} failed: {exc}") from exc

    async def _link_dependency(self, space_id, action: RecoveryAction, user_id, username) -> str:
        if not action.depends_on_issue_key:
            raise JiraActionError("link_dependency requires depends_on_issue_key")
        issue_id = await self._resolve_issue_id(space_id, action.target_issue_key, user_id, username)
        resp = await self._client.post(
            f"/api/issues/{issue_id}/links",
            json={"relation": "is blocked by", "targetIssueKey": action.depends_on_issue_key},
            headers=self._headers(user_id, username),
        )
        resp.raise_for_status()
        return f"{action.target_issue_key} is blocked by {action.depends_on_issue_key}"

    async def _change_priority(self, space_id, action: RecoveryAction, user_id, username) -> str:
        if not action.new_priority:
            raise JiraActionError("change_priority requires new_priority")
        resp = await self._client.put(
            f"/api/spaces/{space_id}/issues/{action.target_issue_key}",
            json={"priority": action.new_priority},
            headers=self._headers(user_id, username),
        )
        resp.raise_for_status()
        return f"{action.target_issue_key} priority -> {action.new_priority}"

    async def _move_out_of_sprint(self, space_id, action: RecoveryAction, user_id, username) -> str:
        resp = await self._client.put(
            f"/api/spaces/{space_id}/issues/{action.target_issue_key}",
            json={"clearSprint": True},
            headers=self._headers(user_id, username),
        )
        resp.raise_for_status()
        return f"{action.target_issue_key} moved out of sprint"

    async def _add_comment(self, space_id, action: RecoveryAction, user_id, username) -> str:
        """**Found live, not hypothetically**: the first real run 500'd with "The given id must not be
        null" — `CommentService.create` (jira-backend) requires `CreateCommentRequest.authorId`
        directly, unlike issue create/update which derive the actor from headers. The original version
        of this method never set it. Fixed by resolving the caller's own numeric id — `user_id` here is
        the string form (`X-User-Id`), jira-backend's `authorId` wants the numeric `Long` id, same value.
        """
        if not action.comment_body:
            raise JiraActionError("add_comment requires comment_body")
        if not user_id:
            raise JiraActionError("add_comment requires a real user_id to attribute authorship to")
        issue_id = await self._resolve_issue_id(space_id, action.target_issue_key, user_id, username)
        resp = await self._client.post(
            f"/api/issues/{issue_id}/comments",
            json={"content": action.comment_body, "authorId": int(user_id)},
            headers=self._headers(user_id, username),
        )
        resp.raise_for_status()
        return f"comment added to {action.target_issue_key}"

    async def aclose(self) -> None:
        await self._client.aclose()
