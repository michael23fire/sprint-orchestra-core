"""The "actually notify someone" half of a workflow reaching status="escalated" — without this,
escalation was a passive UI badge nobody would see unless they happened to reopen the modal. Every
deployment gets a structured WARNING log line and a Prometheus counter increment unconditionally;
an outbound webhook call is additive and off by default (`AI_SPRINT_RECOVERY_ESCALATION_WEBHOOK_URL`),
same "real capability, gated by config, not fake by default" shape `app/tracing.py`'s Phoenix
integration and `kafka_trigger.py`'s Kafka consumer already use in this codebase.

Deliberately called from the API/kafka-trigger layer, never from inside `reevaluate_node` itself: a
graph node only has the state dict to work with, not `Settings` (the webhook URL) or the compiled
graph (needed for `build_escalation_summary`'s checkpoint-history walk) — and a durable-execution node
is the wrong place for a side effect that shouldn't be replayed if the node ever re-runs. This module
reacts to "status is now escalated" from the outside, the same shape
`sprint_recovery_routes.py`'s `_register_if_waiting` already uses for "status is now
waiting_reevaluation".
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.observability import SPRINT_RECOVERY_ESCALATIONS_TOTAL

logger = logging.getLogger(__name__)


async def notify_escalation(
    webhook_url: Optional[str], thread_id: str, space_id: int, sprint_id: int, sprint_name: str, summary: str,
) -> None:
    SPRINT_RECOVERY_ESCALATIONS_TOTAL.labels(str(space_id)).inc()
    logger.warning(
        "sprint recovery escalated — automated remediation exhausted, human intervention needed",
        extra={
            "thread_id": thread_id, "space_id": space_id, "sprint_id": sprint_id,
            "sprint_name": sprint_name, "summary": summary,
        },
    )
    if not webhook_url:
        return
    # "text" is a real Slack incoming-webhook's expected top-level field — pointing this at a real
    # Slack webhook URL works with zero payload translation, not just a hypothetical shape.
    payload = {
        "text": f"Sprint recovery escalated for \"{sprint_name}\" (thread {thread_id[:8]}): {summary}",
        "thread_id": thread_id, "space_id": space_id, "sprint_id": sprint_id, "sprint_name": sprint_name,
        "summary": summary,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(webhook_url, json=payload)
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - a failed notification must never break the actual status transition
        logger.warning(
            "sprint recovery escalation webhook call failed", extra={"thread_id": thread_id, "error": str(exc)},
        )
