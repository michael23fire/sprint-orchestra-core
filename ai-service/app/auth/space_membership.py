"""Validates that a caller's requested space_ids are actually within their real space membership.

Previously, `space_ids` on /ask, /ask/stream, and /search was trusted unconditionally from the
request body — nothing stopped a caller from asking about a space they aren't a member of (see
gateway/application.yml's own documented trust-boundary comment). The gateway already validates the
JWT at the edge and forwards the real authenticated user's id as X-User-Id (see
PropagateUserHeadersGatewayFilter.java); jira-backend already exposes a membership-scoped space list
(GET /api/spaces?userId=X, the same endpoint the frontend's own SpaceContext calls). This wires those
two already-existing pieces together instead of building new membership infrastructure.

Scope: only enforced when X-User-Id is present on the request. A direct caller with no X-User-Id
(this project's own eval/smoke-test scripts, which all call ai-service directly on :8200 and never go
through the gateway) is treated as a trusted internal caller and skipped — the same shape of decision
jira-backend's own GatewayInternalAuthFilter.requiresUserIdentity() already makes per-route. Real
end-user traffic always goes through the gateway and always carries X-User-Id, so this covers the
actual attack surface (a browser client forging space_ids) without breaking direct internal tooling.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx


class SpaceMembershipError(Exception):
    """Raised when a caller requests a space_id they are not a member of."""

    def __init__(self, user_id: str, unauthorized_space_ids: list[int]):
        self.user_id = user_id
        self.unauthorized_space_ids = unauthorized_space_ids
        super().__init__(
            f"user {user_id} is not a member of space(s) {unauthorized_space_ids}"
        )


class SpaceMembershipChecker:
    def __init__(self, jira_backend_url: str, internal_gateway_token: str, cache_ttl_seconds: float = 60.0):
        self._client = httpx.AsyncClient(
            base_url=jira_backend_url.rstrip("/"),
            headers={"X-Gateway-Internal": internal_gateway_token},
            timeout=5.0,
        )
        self._cache_ttl = cache_ttl_seconds
        # user_id -> (cached_at, authorized_space_ids). Small in-process TTL cache: a user's space
        # membership changes rarely, and /ask is latency-sensitive — worth avoiding a jira-backend
        # round trip on every single request for a fact that's almost always unchanged since the last
        # check a few seconds ago.
        self._cache: dict[str, tuple[float, set[int]]] = {}

    async def _authorized_space_ids(self, user_id: str, username: str) -> set[int]:
        now = time.monotonic()
        cached = self._cache.get(user_id)
        if cached is not None and now - cached[0] < self._cache_ttl:
            return cached[1]
        # jira-backend's GatewayInternalAuthFilter.requiresUserIdentity() requires BOTH X-User-Id and
        # X-Username to authenticate a /api/ request (see that filter's doFilterInternal) — found live,
        # sending only X-Gateway-Internal got a 401 "Missing gateway user headers" even though the
        # shared trust secret was correct. Both headers are exactly what the gateway itself already
        # forwards to ai-service (see PropagateUserHeadersGatewayFilter.java), so this just passes
        # them onward one more hop.
        resp = await self._client.get(
            "/api/spaces", params={"userId": user_id},
            headers={"X-User-Id": user_id, "X-Username": username},
        )
        resp.raise_for_status()
        ids = {space["id"] for space in resp.json()}
        self._cache[user_id] = (now, ids)
        return ids

    async def validate(
        self, user_id: Optional[str], username: Optional[str], requested_space_ids: list[int]
    ) -> None:
        """Raises SpaceMembershipError if user_id is present and any requested space isn't theirs.

        No-op (including no jira-backend call) when user_id is None — see module docstring for why.
        """
        if not user_id:
            return
        authorized = await self._authorized_space_ids(user_id, username or "")
        unauthorized = [s for s in requested_space_ids if s not in authorized]
        if unauthorized:
            raise SpaceMembershipError(user_id, unauthorized)

    async def aclose(self) -> None:
        await self._client.aclose()


class NoopSpaceMembershipChecker:
    """Used when space_membership_check_enabled=false — always permits, never calls jira-backend."""

    async def validate(
        self, user_id: Optional[str], username: Optional[str], requested_space_ids: list[int]
    ) -> None:
        return None

    async def aclose(self) -> None:
        return None


def build_space_membership_checker(settings):
    if not settings.space_membership_check_enabled:
        return NoopSpaceMembershipChecker()
    return SpaceMembershipChecker(
        settings.jira_backend_url,
        settings.internal_gateway_token,
        settings.space_membership_cache_ttl_seconds,
    )
