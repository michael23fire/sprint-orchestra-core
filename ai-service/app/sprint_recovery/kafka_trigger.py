"""Event-driven re-evaluation: a second `aiokafka` consumer group on the *same*
`jira.content.ingestion` topic `vectorization-service` already consumes for RAG ingestion (see
`vectorization-service/app/kafka/consumer.py`). Kafka consumer groups are independent — a second
group_id on the same topic gets its own full copy of every message and does not compete with or
duplicate vectorization-service's own consumption. This graph does not invent new event
infrastructure, it taps into infrastructure that already exists and is already proven reliable.

**Known limitation, stated plainly, same shape as the rollout workflow's own "you can't rediscover a
pending workflow without its thread_id" gap**: `_waiting_by_sprint` is an in-process dict, not a
persisted registry. A workflow paused at `wait_for_reevaluation` is only findable by this consumer for
as long as the *same* ai-service process that started it is still running — an ai-service restart
loses the (sprint_id -> thread_ids) index, even though the underlying Postgres checkpoint itself is
completely intact and resumable via the manual "re-check now" trigger regardless. A real production
version would persist this index (the same kind of lightweight registry table already named as a real,
not-yet-built gap for the rollout workflow) — not built here, for the same reason: it's an
availability/discoverability concern, not a data-safety one.

**Found live, a cross-tenant version of the same class of bug**: `_handle` used to loop over *every*
sprint in `_waiting_by_sprint`, regardless of which space (tenant) the incoming event's issue actually
belongs to — an `IssueContentChangedEvent` for space A's issue would resume space B's workflows waiting
on an unrelated sprint. Fixed by recording `sprint_id -> space_id` alongside the existing waiting-thread
index and skipping any waiting sprint whose space doesn't match the event's `spaceId`.

**Found live, a second gap in the same area — the event envelope does carry `sprintId`
(`IssueIngestionMessage.sprintId`, jira-backend), an earlier version of this docstring wrongly claimed
it didn't**: matching on `sprintId == this workflow's sprint_id` is the right primary check, since
`reevaluate_node` recomputes risk for the *whole* sprint from `sprint_id` (any issue's change could
matter, not just ones already flagged). But one real case breaks a sprintId-only match: when this
workflow's own committed `move_out_of_sprint` action is what changed the issue, the resulting event's
`sprintId` is now `null` (the issue just left the sprint) — the single most important self-triggered
case would fail to match its own sprint. Closed with a fallback: also match if the event's `issueKey` is
one this workflow is actually tracking (its risk-flagged issues or its approved plan's target/dependency
issues, recorded at registration time — see `register_waiting`). An event matching neither is genuinely
irrelevant and is skipped before ever calling `trigger_reevaluation`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, Optional, Set

from aiokafka import AIOKafkaConsumer

from app.observability import SPRINT_RECOVERY_INDEX_CATCH_UP_TIMEOUTS_TOTAL

logger = logging.getLogger(__name__)


class SprintRecoveryKafkaTrigger:
    def __init__(
        self, bootstrap_servers: str, topic: str, group_id: str, compiled_graph,
        escalation_webhook_url: Optional[str] = None,
    ):
        self._bootstrap_servers = bootstrap_servers
        self._topic = topic
        self._group_id = group_id
        self._graph = compiled_graph
        # Only for the escalation-handoff notification below — a real event-triggered reevaluation can
        # land on status="escalated" exactly the same way a manual "re-check now" click can, and that
        # deserves the same handoff, not a silently-different behavior depending on which path resumed
        # the workflow.
        self._escalation_webhook_url = escalation_webhook_url
        self._consumer: Optional[AIOKafkaConsumer] = None
        self._task: Optional[asyncio.Task] = None
        # sprint_id -> set of thread_ids currently paused at wait_for_reevaluation for that sprint.
        # Registered by the API layer right before pausing (see routes.py's decision endpoint) and
        # cleared once a trigger fires — see this module's docstring for the known persistence gap.
        self._waiting_by_sprint: Dict[int, Set[str]] = {}
        # sprint_id -> space_id, so an event can be checked against the tenant it actually belongs to
        # before triggering anything waiting on a different tenant's sprint — see docstring.
        self._space_by_sprint: Dict[int, int] = {}
        # thread_id -> issue keys this specific thread cares about (its risk-flagged issues + its
        # approved plan's target/dependency issues) — the fallback match for an event whose sprintId
        # doesn't match this thread's sprint, see docstring.
        self._tracked_issue_keys_by_thread: Dict[str, Set[str]] = {}
        self.events_processed = 0
        self.workflows_triggered = 0

    def register_waiting(
        self, space_id: int, sprint_id: int, thread_id: str, tracked_issue_keys: Optional[Set[str]] = None,
    ) -> None:
        self._waiting_by_sprint.setdefault(sprint_id, set()).add(thread_id)
        self._space_by_sprint[sprint_id] = space_id
        self._tracked_issue_keys_by_thread[thread_id] = set(tracked_issue_keys or ())

    def _unregister(self, sprint_id: int, thread_id: str) -> None:
        self._waiting_by_sprint.get(sprint_id, set()).discard(thread_id)
        self._tracked_issue_keys_by_thread.pop(thread_id, None)

    async def start(self) -> None:
        self._consumer = AIOKafkaConsumer(
            self._topic, bootstrap_servers=self._bootstrap_servers, group_id=self._group_id,
            enable_auto_commit=True, auto_offset_reset="latest",
            value_deserializer=lambda b: b,
        )
        await self._consumer.start()
        self._task = asyncio.create_task(self._run(), name="sprint-recovery-kafka-trigger")
        logger.info("sprint-recovery kafka trigger started", extra={"topic": self._topic, "group": self._group_id})

    async def _run(self) -> None:
        assert self._consumer is not None
        async for record in self._consumer:
            try:
                await self._handle(record.value)
            except Exception:  # noqa: BLE001 - one bad event must not kill the consumer loop
                logger.exception("sprint-recovery kafka trigger failed handling a record")

    async def _handle(self, raw: bytes) -> None:
        self.events_processed += 1
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return  # not JSON we understand — same "poison message, skip" tolerance the ingestion consumer uses
        issue_key = payload.get("issueKey") or payload.get("issue_key")
        space_id = payload.get("spaceId") or payload.get("space_id")
        event_sprint_id = payload.get("sprintId") or payload.get("sprint_id")
        logger.info(
            "sprint-recovery kafka trigger received event",
            extra={
                "issue_key": issue_key, "space_id": space_id, "sprint_id": event_sprint_id,
                "waiting_sprints": list(self._waiting_by_sprint.keys()),
            },
        )
        if not issue_key or space_id is None:
            return

        from app.sprint_recovery.graph import build_escalation_summary, flatten_escalation_summary, trigger_reevaluation
        from app.sprint_recovery.notifications import notify_escalation

        # Which sprints does this event's issue actually belong to, among the ones something is
        # waiting on? Same-space is necessary but not sufficient — within a space, an event is only
        # relevant to a waiting thread if either the issue is currently in that thread's sprint, or (the
        # move_out_of_sprint self-trigger case, see docstring) the issue is one this thread is actually
        # tracking, regardless of its current sprintId.
        for sprint_id, thread_ids in list(self._waiting_by_sprint.items()):
            if self._space_by_sprint.get(sprint_id) != space_id:
                continue
            same_sprint = event_sprint_id is not None and event_sprint_id == sprint_id
            for thread_id in list(thread_ids):
                if not same_sprint and issue_key not in self._tracked_issue_keys_by_thread.get(thread_id, ()):
                    continue
                try:
                    result = await trigger_reevaluation(
                        self._graph, thread_id, source="kafka_event", detail=f"{issue_key} changed",
                    )
                    if result.get("index_catch_up_timed_out"):
                        SPRINT_RECOVERY_INDEX_CATCH_UP_TIMEOUTS_TOTAL.labels(str(space_id)).inc()
                        logger.warning(
                            "sprint recovery reevaluation (kafka-triggered) timed out waiting for the "
                            "read model to catch up — reading anyway",
                            extra={"space_id": space_id, "sprint_id": sprint_id, "thread_id": thread_id},
                        )
                    if result.get("status") == "escalated":
                        summary = await build_escalation_summary(self._graph, thread_id)
                        await notify_escalation(
                            self._escalation_webhook_url, thread_id, result["space_id"], sprint_id,
                            result["sprint_name"], flatten_escalation_summary(summary),
                        )
                except Exception:  # noqa: BLE001 - one thread's failure must not stop other threads/future events
                    logger.exception("failed triggering reevaluation for thread %s", thread_id)
                    continue
                self.workflows_triggered += 1
                # This specific `wait_for_reevaluation` pause is consumed either way (a future
                # escalation round re-registers via a fresh register_waiting call once it reaches that
                # node again) — nothing here can double-trigger the same pause a second time.
                self._unregister(sprint_id, thread_id)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._consumer:
            await self._consumer.stop()
