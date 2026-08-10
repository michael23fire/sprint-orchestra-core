"""Publishes an approved AI epic plan to jira-backend.

The durable graph calls this client once per side effect: create/reuse each selected sprint, create
the real epic, create each child issue in its chosen sprint, then create each planned dependency
link. Keeping the client operations small lets the graph checkpoint progress between operations.

Same gateway-internal-token + X-User-Id/X-Username header pattern `app/auth/space_membership.py`
already established for calling jira-backend from ai-service — see that module's docstring for why
both headers are required (`GatewayInternalAuthFilter.requiresUserIdentity()`).

**Correction from this module's first version**: it previously claimed jira-backend has no epic
concept and tagged every issue with a synthetic label instead. That was wrong — checked against the
wrong evidence (`IssueDraft.issue_type`'s `Literal["story","task","bug"]` constraint, which is
ai-service's own planning-schema restriction on what the *LLM* may propose, not a jira-backend
capability limit). `sprint-orchestra-studio`'s existing `PlanEpicModal.tsx` already creates a real
`issueType: "epic"` issue and links children via `parentId` — `Issue.issueType` is a free-text column
that already accepts "epic" as a value in practice. Fixed to match: this client now creates one real
epic issue and parent-links every child to it, the same shape the existing frontend's own (now being
replaced) client-side commit loop already used.
"""
from __future__ import annotations

from typing import Optional

import httpx

from app.planning.schemas import EpicDraft, IssueDraft


class JiraCommitError(Exception):
    """A Jira publishing call failed or returned an unusable response.

    The graph does not record the step as completed and exposes an explicit resume path. As with any
    cross-service request, a transport failure after the remote commit is ambiguous; the UI therefore
    describes checkpointed resume rather than claiming a distributed exactly-once transaction.
    """


class JiraCommitClient:
    def __init__(self, jira_backend_url: str, internal_gateway_token: str, timeout: float = 10.0):
        self._client = httpx.AsyncClient(
            base_url=jira_backend_url.rstrip("/"),
            headers={"X-Gateway-Internal": internal_gateway_token},
            timeout=timeout,
        )

    def _headers(self, user_id: str, username: str) -> dict:
        # Only attach identity headers when a real one was forwarded (see routes.py's
        # `_caller_identity`) — sending a fabricated placeholder to jira-backend would misattribute
        # the created issue's reporter instead of leaving it anonymous/system-attributed, and mirrors
        # the same "empty means trusted direct caller, not a fake identity" rule the RBAC recheck uses.
        return {"X-User-Id": user_id, "X-Username": username} if user_id else {}

    async def create_epic_issue(
        self, space_id: int, epic: EpicDraft, user_id: str, username: str,
    ) -> tuple[int, str]:
        """Creates the real epic-type issue every rollout issue will be `parentId`-linked to. Returns
        `(issue_id, issue_key)` — the id (not just the key) because `parentId` on the child-issue
        create call is jira-backend's internal id, not the human-facing key.
        """
        body = {"title": epic.title, "description": epic.description, "issueType": "epic"}
        try:
            resp = await self._client.post(
                f"/api/spaces/{space_id}/issues", json=body, headers=self._headers(user_id, username),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise JiraCommitError(f"failed creating epic issue '{epic.title}': {exc}") from exc
        data = resp.json()
        issue_id, issue_key = data.get("id"), data.get("issueKey")
        if not issue_id or not issue_key:
            raise JiraCommitError(f"jira-backend response for epic '{epic.title}' missing id/issueKey: {data}")
        return issue_id, issue_key

    async def create_sprint(
        self, space_id: int, name: str, user_id: str, username: str,
    ) -> int:
        try:
            resp = await self._client.post(
                f"/api/spaces/{space_id}/sprints",
                json={"name": name},
                headers=self._headers(user_id, username),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise JiraCommitError(f"failed creating sprint '{name}': {exc}") from exc
        sprint_id = resp.json().get("id")
        if not sprint_id:
            raise JiraCommitError(f"jira-backend response for sprint '{name}' missing id")
        return int(sprint_id)

    async def create_issue(
        self,
        space_id: int,
        issue: IssueDraft,
        parent_id: int,
        sprint_id: Optional[int],
        user_id: str,
        username: str,
    ) -> tuple[int, str]:
        """Creates one issue under `parent_id`, returning its internal id and issue key.

        The id is retained by the graph so dependency links can be created after all issues exist.
        Raises `JiraCommitError` on any failure — callers must not guess at partial success.
        """
        description = issue.description.strip()
        if issue.estimate_rationale:
            description = f"{description}\n\nEstimate rationale: {issue.estimate_rationale}".strip()
        body = {
            "title": issue.title,
            "description": description,
            "issueType": issue.issue_type,
            "storyPoints": issue.estimate_story_points,
            "labels": issue.labels,
            "parentId": parent_id,
            "sprintId": sprint_id,
        }
        try:
            resp = await self._client.post(
                f"/api/spaces/{space_id}/issues", json=body, headers=self._headers(user_id, username),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise JiraCommitError(f"failed creating issue '{issue.title}': {exc}") from exc
        data = resp.json()
        issue_id, issue_key = data.get("id"), data.get("issueKey")
        if not issue_id or not issue_key:
            raise JiraCommitError(f"jira-backend response for '{issue.title}' missing id/issueKey: {data}")
        return int(issue_id), issue_key

    async def create_issue_link(
        self,
        source_issue_id: int,
        target_issue_key: str,
        user_id: str,
        username: str,
    ) -> int:
        try:
            resp = await self._client.post(
                f"/api/issues/{source_issue_id}/links",
                json={"relation": "is blocked by", "targetIssueKey": target_issue_key},
                headers=self._headers(user_id, username),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise JiraCommitError(
                f"failed linking issue id {source_issue_id} to '{target_issue_key}': {exc}"
            ) from exc
        link_id = resp.json().get("id")
        if not link_id:
            raise JiraCommitError(
                f"jira-backend link response for issue id {source_issue_id} missing id"
            )
        return int(link_id)

    async def aclose(self) -> None:
        await self._client.aclose()
