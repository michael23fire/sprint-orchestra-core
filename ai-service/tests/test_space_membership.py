"""SpaceMembershipChecker + _authorize_space_ids — closes a real gap: space_ids was previously
trusted unconditionally from the caller (see gateway/application.yml's own documented trust-boundary
comment). No real network calls — httpx.MockTransport stands in for jira-backend's
GET /api/spaces?userId=X (the same endpoint the frontend's own SpaceContext already calls).
"""
import httpx
import pytest

from app.api.routes import _authorize_space_ids, _authorize_workflow_state
from app.api.sprint_recovery_routes import DecisionRequest, submit_recovery_decision_endpoint
from app.auth.space_membership import (
    NoopSpaceMembershipChecker,
    SpaceMembershipChecker,
    SpaceMembershipError,
)


def _checker_with_spaces(user_spaces: dict[str, list[int]]) -> SpaceMembershipChecker:
    def handler(request: httpx.Request) -> httpx.Response:
        # Regression guard: found live, jira-backend's GatewayInternalAuthFilter.
        # requiresUserIdentity() rejects with 401 "Missing gateway user headers" unless BOTH
        # X-User-Id and X-Username are present, not just the shared X-Gateway-Internal trust secret.
        assert request.headers["x-gateway-internal"] == "test-token"
        assert request.headers["x-user-id"]
        assert request.headers["x-username"]
        user_id = request.url.params["userId"]
        spaces = user_spaces.get(user_id, [])
        return httpx.Response(200, json=[{"id": sid} for sid in spaces])

    checker = SpaceMembershipChecker("http://jira-backend.test", "test-token")
    checker._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://jira-backend.test",
        headers={"X-Gateway-Internal": "test-token"},
    )
    return checker


async def test_validate_passes_when_all_requested_spaces_are_authorized():
    checker = _checker_with_spaces({"42": [5000014, 5000020]})
    await checker.validate("42", "atlascart.maya", [5000014])  # must not raise


async def test_validate_rejects_a_space_the_user_is_not_a_member_of():
    checker = _checker_with_spaces({"42": [5000014]})
    with pytest.raises(SpaceMembershipError) as exc_info:
        await checker.validate("42", "atlascart.maya", [5000014, 9999999])
    assert exc_info.value.unauthorized_space_ids == [9999999]


async def test_validate_skips_the_check_entirely_when_user_id_is_absent():
    # This project's own eval/smoke-test scripts call ai-service directly on :8200, bypassing the
    # gateway entirely — treated as a trusted internal caller, same shape of decision jira-backend's
    # own GatewayInternalAuthFilter.requiresUserIdentity() already makes per-route.
    checker = _checker_with_spaces({})  # would reject everything if the check ran at all
    await checker.validate(None, None, [5000014])  # must not raise, must not call jira-backend


async def test_validate_caches_the_membership_lookup():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["userId"])
        return httpx.Response(200, json=[{"id": 5000014}])

    checker = SpaceMembershipChecker("http://jira-backend.test", "test-token", cache_ttl_seconds=60.0)
    checker._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://jira-backend.test",
    )

    await checker.validate("42", "atlascart.maya", [5000014])
    await checker.validate("42", "atlascart.maya", [5000014])

    assert len(calls) == 1  # second call served from cache, not a second HTTP round trip


async def test_noop_checker_always_permits():
    checker = NoopSpaceMembershipChecker()
    await checker.validate("42", "atlascart.maya", [5000014, 9999999])  # must not raise


class _FakeApp:
    def __init__(self, checker):
        self.state = type("State", (), {"space_membership": checker})()


class _FakeRequest:
    def __init__(self, checker, user_id=None, username=None):
        self.app = _FakeApp(checker)
        headers = {}
        if user_id:
            headers["x-user-id"] = user_id
        if username:
            headers["x-username"] = username
        self.headers = headers


async def test_authorize_space_ids_raises_403_for_an_unauthorized_space():
    from fastapi import HTTPException

    checker = _checker_with_spaces({"42": [5000014]})
    request = _FakeRequest(checker, user_id="42", username="atlascart.maya")

    with pytest.raises(HTTPException) as exc_info:
        await _authorize_space_ids(request, [9999999])
    assert exc_info.value.status_code == 403


async def test_authorize_space_ids_passes_for_an_authorized_space():
    checker = _checker_with_spaces({"42": [5000014]})
    request = _FakeRequest(checker, user_id="42", username="atlascart.maya")

    await _authorize_space_ids(request, [5000014])  # must not raise


async def test_authorize_space_ids_returns_502_not_a_raw_500_when_jira_backend_is_unreachable():
    from fastapi import HTTPException

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    checker = SpaceMembershipChecker("http://jira-backend.test", "test-token")
    checker._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://jira-backend.test",
    )
    request = _FakeRequest(checker, user_id="42", username="atlascart.maya")

    with pytest.raises(HTTPException) as exc_info:
        await _authorize_space_ids(request, [5000014])
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail == "space membership check failed"


async def test_workflow_state_allows_the_owner_after_rechecking_current_membership():
    checker = _checker_with_spaces({"42": [5000014]})
    request = _FakeRequest(checker, user_id="42", username="atlascart.maya")

    await _authorize_workflow_state(request, {"user_id": "42", "space_id": 5000014})


async def test_workflow_state_rejects_a_different_authenticated_user():
    from fastapi import HTTPException

    checker = _checker_with_spaces({"99": [5000014]})
    request = _FakeRequest(checker, user_id="99", username="mallory")

    with pytest.raises(HTTPException) as exc_info:
        await _authorize_workflow_state(request, {"user_id": "42", "space_id": 5000014})
    assert exc_info.value.status_code == 403


async def test_workflow_state_keeps_trusted_direct_internal_callers_working():
    checker = _checker_with_spaces({})
    request = _FakeRequest(checker)

    await _authorize_workflow_state(request, {"user_id": "", "space_id": 5000014})


async def test_recovery_decision_authorizes_owner_before_revealing_workflow_status():
    """A non-owner gets 403 even when the guessed thread is in a state that would otherwise be 409."""
    from fastapi import HTTPException

    class Graph:
        async def aget_state(self, config):
            return type("Snapshot", (), {"values": {
                "user_id": "42", "space_id": 5000014, "status": "failed",
            }})()

    checker = _checker_with_spaces({"99": [5000014]})
    request = _FakeRequest(checker, user_id="99", username="mallory")
    request.app.state.sprint_recovery_graph = Graph()

    with pytest.raises(HTTPException) as exc_info:
        await submit_recovery_decision_endpoint(
            "guessed-thread", DecisionRequest(decision="approve", plan_id="plan-1"), request
        )
    assert exc_info.value.status_code == 403
