"""Tests for app/sprint_recovery/notifications.py — the escalation handoff (structured log always,
webhook POST only if configured). Mocks httpx.AsyncClient directly via unittest.mock rather than
tests/test_space_membership.py's httpx.MockTransport pattern — there's exactly one outbound call to
verify here, not a request/response contract worth a fake transport for.
"""
from unittest.mock import AsyncMock, patch

from app.sprint_recovery.notifications import notify_escalation


async def test_notify_escalation_without_webhook_url_makes_no_http_call():
    with patch("app.sprint_recovery.notifications.httpx.AsyncClient") as mock_client_cls:
        await notify_escalation(None, "thread-1", 5000014, 7, "Sprint 7", "summary text")
    mock_client_cls.assert_not_called()


async def test_notify_escalation_with_webhook_url_posts_slack_shaped_payload():
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_response = AsyncMock()
    mock_response.raise_for_status = lambda: None
    mock_client.post.return_value = mock_response

    with patch("app.sprint_recovery.notifications.httpx.AsyncClient", return_value=mock_client):
        await notify_escalation(
            "https://hooks.example/webhook", "thread-1", 5000014, 7, "Sprint 7", "summary text",
        )

    mock_client.post.assert_awaited_once()
    args, kwargs = mock_client.post.call_args
    assert args[0] == "https://hooks.example/webhook"
    assert kwargs["json"]["text"].startswith('Sprint recovery escalated for "Sprint 7"')
    assert kwargs["json"]["summary"] == "summary text"
    assert kwargs["json"]["thread_id"] == "thread-1"


async def test_notify_escalation_webhook_failure_does_not_raise():
    """A down/misconfigured webhook must never break the actual status transition it's reacting to —
    see notify_escalation's own docstring on why this is called from outside the durable graph."""
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.post.side_effect = Exception("network down")

    with patch("app.sprint_recovery.notifications.httpx.AsyncClient", return_value=mock_client):
        await notify_escalation("https://hooks.example/webhook", "thread-1", 5000014, 7, "Sprint 7", "summary")
    # No exception propagated out — that's the assertion.
