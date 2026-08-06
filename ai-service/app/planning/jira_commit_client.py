"""Writes an approved epic-rollout plan to jira-backend: one real epic-type issue, then one issue per
node step, each `parentId`-linked to it.

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

import time
from typing import List, Optional

import httpx

from app.planning.schemas import EpicDraft, IssueDraft


class JiraCommitError(Exception):
    """A create-issue call to jira-backend failed. Callers should leave the rollout's `committed`
    ledger (and `epic_issue_id`) exactly as they were before the call — a failed create was never
    persisted server-side, so there is nothing to record, and a resume will simply retry.
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

    async def create_issue(
        self,
        space_id: int,
        issue: IssueDraft,
        parent_id: int,
        user_id: str,
        username: str,
    ) -> str:
        """Creates exactly one issue under `parent_id` (the epic), returns its real `issueKey`.
        Raises `JiraCommitError` on any failure — callers must not guess at partial success.
        """
        body = {
            "title": issue.title,
            "description": issue.description,
            "issueType": issue.issue_type,
            "storyPoints": issue.estimate_story_points,
            "labels": issue.labels,
            "parentId": parent_id,
        }
        try:
            resp = await self._client.post(
                f"/api/spaces/{space_id}/issues", json=body, headers=self._headers(user_id, username),
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise JiraCommitError(f"failed creating issue '{issue.title}': {exc}") from exc
        data = resp.json()
        issue_key = data.get("issueKey")
        if not issue_key:
            raise JiraCommitError(f"jira-backend response for '{issue.title}' had no issueKey: {data}")
        return issue_key

    async def aclose(self) -> None:
        await self._client.aclose()
