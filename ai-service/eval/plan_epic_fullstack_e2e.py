"""Full-stack destructive E2E test for the integrated durable Plan Epic workflow.

This is deliberately not part of the hermetic pytest suite: it requires the running gateway,
jira-backend, ai-service, core PostgreSQL/checkpoint store, and a real configured LLM. It exercises
the same HTTP boundary as the React UI:

    login -> durable AI generation -> stateless AI refine -> human edited approval ->
    server-side sprint/epic/issue/dependency publishing -> read-back verification -> cleanup

Every created title contains a unique run marker. Cleanup runs in ``finally`` even after a failed
assertion and only targets ids/keys returned by this run or exact marker-bearing names. The Epic
delete cascades through its children and links in jira-backend; the two test sprints and the exact
LangGraph thread checkpoints are then removed separately. Kafka-driven deletion from the replicated
vector/search index is also awaited and verified, with an exact-key purge only as a cleanup fallback.

Run from ``ai-service/`` after ``../scripts/dev_up.sh status`` is healthy:

    .venv/bin/python -m eval.plan_epic_fullstack_e2e

Optional overrides:

    .venv/bin/python -m eval.plan_epic_fullstack_e2e --space-id 5000018
    E2E_USERNAME=alice E2E_PASSWORD=123 .venv/bin/python -m eval.plan_epic_fullstack_e2e

Data is deleted by default. ``--keep-data`` exists only for deliberate debugging.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx


DEFAULT_CHECKPOINT_DSN = "postgresql://poc:poc123@localhost:5432/pocdb"
DEFAULT_VECTOR_DSN = "postgresql://vec:vec123@localhost:5433/vecdb"


class E2EFailure(AssertionError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise E2EFailure(message)


@dataclass
class CreatedResources:
    thread_id: str | None = None
    epic_key: str | None = None
    child_keys: list[str] = field(default_factory=list)
    sprint_ids: list[int] = field(default_factory=list)
    exact_sprint_names: list[str] = field(default_factory=list)


class GatewayClient:
    def __init__(self, base_url: str, timeout_seconds: float):
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_seconds)
        self._headers: dict[str, str] = {}

    def close(self) -> None:
        self._client.close()

    def set_token(self, token: str) -> None:
        self._headers = {"Authorization": f"Bearer {token}"}

    def request_json(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        response = self._client.request(method, path, json=json, headers=self._headers)
        if response.status_code not in expected:
            raise E2EFailure(
                f"{method} {path} returned HTTP {response.status_code}: {response.text[:1000]}"
            )
        if not response.content:
            return None
        return response.json()

    def delete(self, path: str, expected: tuple[int, ...] = (204,)) -> None:
        self.request_json("DELETE", path, expected=expected)

    def post_sse_result(self, path: str, payload: dict[str, Any]) -> tuple[Any, list[str]]:
        """Consume the UI's SSE boundary and return the terminal result plus progress labels."""
        event_name: str | None = None
        data_lines: list[str] = []
        stage_labels: list[str] = []
        result: Any | None = None

        def consume_event() -> None:
            nonlocal event_name, data_lines, result
            if event_name is None and not data_lines:
                return
            raw_data = "\n".join(data_lines)
            try:
                parsed = json.loads(raw_data) if raw_data else None
            except json.JSONDecodeError as exc:
                raise E2EFailure(
                    f"invalid SSE JSON for event {event_name!r}: {raw_data[:1000]}"
                ) from exc
            if event_name == "stage":
                label = parsed.get("label") if isinstance(parsed, dict) else None
                require(bool(label), f"stage SSE event had no label: {parsed!r}")
                stage_labels.append(str(label))
            elif event_name == "result":
                result = parsed
            elif event_name == "error":
                raise E2EFailure(f"Plan Epic SSE returned an error event: {parsed!r}")
            event_name = None
            data_lines = []

        with self._client.stream("POST", path, json=payload, headers=self._headers) as response:
            if response.status_code != 200:
                response.read()
                raise E2EFailure(
                    f"POST {path} returned HTTP {response.status_code}: {response.text[:1000]}"
                )
            require(
                "text/event-stream" in response.headers.get("content-type", ""),
                f"POST {path} did not return event-stream content",
            )
            for line in response.iter_lines():
                if line == "":
                    consume_event()
                elif line.startswith("event:"):
                    event_name = line.removeprefix("event:").strip()
                elif line.startswith("data:"):
                    data_lines.append(line.removeprefix("data:").lstrip())
            consume_event()

        require(result is not None, "Plan Epic SSE stream ended without a result event")
        return result, stage_labels


def _check_http(name: str, url: str, expected_status: int = 200) -> None:
    try:
        response = httpx.get(url, timeout=5.0)
    except Exception as exc:  # noqa: BLE001 - report all preflight connection failures uniformly
        raise E2EFailure(f"preflight: {name} is not reachable at {url}: {exc}") from exc
    require(
        response.status_code == expected_status,
        f"preflight: {name} returned HTTP {response.status_code} at {url}",
    )


def _check_tcp(name: str, host: str, port: int) -> None:
    try:
        with socket.create_connection((host, port), timeout=3.0):
            return
    except OSError as exc:
        raise E2EFailure(f"preflight: {name} is not listening on {host}:{port}: {exc}") from exc


def preflight(args: argparse.Namespace) -> None:
    print("[1/9] Preflight: required full-stack services")
    _check_http("gateway", f"{args.gateway_url.rstrip('/')}/api/auth/config")
    _check_http("ai-service", f"{args.ai_url.rstrip('/')}/healthz")
    _check_http("frontend", args.frontend_url.rstrip("/") + "/")
    _check_http("LLM server", f"{args.llm_url.rstrip('/')}/v1/models")
    _check_tcp("core PostgreSQL", "127.0.0.1", 5432)
    _check_tcp("pgvector PostgreSQL", "127.0.0.1", 5433)
    # Redis/Kafka/vectorization do not participate in Plan Epic generation/publishing directly, but
    # checking the standard dev stack here matches the purpose of this full-stack runner and catches
    # a half-started environment before it mutates Jira data.
    _check_tcp("Redis", "127.0.0.1", 6379)
    _check_tcp("Kafka", "127.0.0.1", 9092)
    _check_http("vectorization-service", f"{args.vector_url.rstrip('/')}/healthz")
    print("      all required services are reachable")


def login_and_choose_space(
    api: GatewayClient, args: argparse.Namespace
) -> tuple[dict[str, Any], dict[str, Any]]:
    print("[2/9] Authenticate through the gateway and select an authorized space")
    auth = api.request_json(
        "POST",
        "/api/auth/token",
        json={"username": args.username, "password": args.password},
    )
    api.set_token(auth["accessToken"])
    user = auth["user"]
    spaces = api.request_json("GET", f"/api/spaces?userId={user['id']}")
    require(bool(spaces), f"user {args.username!r} has no visible spaces")
    if args.space_id is None:
        preferred = next((s for s in spaces if int(s["id"]) == 5000018), None)
        space = preferred or spaces[0]
    else:
        space = next((s for s in spaces if int(s["id"]) == args.space_id), None)
        require(space is not None, f"user {args.username!r} cannot access space {args.space_id}")
    print(f"      user={user['username']} ({user['id']}), space={space['key']} ({space['id']})")
    return user, space


def deterministic_edited_plan(marker: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    epic = {
        "title": f"[{marker}] Durable order export",
        "description": "E2E-edited epic covering API, background execution, UI, and QA.",
        "goals": ["Publish a verified two-sprint plan through the durable workflow"],
    }
    issues = [
        {
            "tempId": "1",
            "title": f"[{marker}] Define export API",
            "description": "Define the permissioned order export contract.",
            "issueType": "story",
            "labels": [],
            "estimateStoryPoints": 3,
            "estimateRationale": "A bounded API and authorization change.",
            "dependsOn": [],
        },
        {
            "tempId": "2",
            "title": f"[{marker}] Implement export job",
            "description": "Generate CSV exports asynchronously and store the result.",
            "issueType": "task",
            "labels": [],
            "estimateStoryPoints": 5,
            "estimateRationale": "Background execution and object storage integration.",
            "dependsOn": ["1"],
        },
        {
            "tempId": "3",
            "title": f"[{marker}] Build export progress UI",
            "description": "Show export progress and expose the completed download.",
            "issueType": "story",
            "labels": [],
            "estimateStoryPoints": 3,
            "estimateRationale": "A focused UI flow using the new API.",
            "dependsOn": ["1"],
        },
        {
            "tempId": "4",
            "title": f"[{marker}] Verify export workflow",
            "description": "Add integration coverage for permissions, export, and download.",
            "issueType": "task",
            "labels": [],
            "estimateStoryPoints": 3,
            "estimateRationale": "Cross-layer verification after backend and UI completion.",
            "dependsOn": ["2", "3"],
        },
    ]
    return epic, issues


def verify_dependency(issue: dict[str, Any], expected_key: str) -> None:
    matches = [
        link for link in issue.get("linkedIssues") or []
        if link.get("relation") == "is blocked by" and link.get("linkedIssueKey") == expected_key
    ]
    require(bool(matches), f"{issue['issueKey']} is missing dependency on {expected_key}")


def execute_flow(
    api: GatewayClient,
    args: argparse.Namespace,
    resources: CreatedResources,
    marker: str,
) -> None:
    user, space = login_and_choose_space(api, args)
    space_id = int(space["id"])

    print("[3/9] Create the existing-sprint fixture used by the final human edit")
    existing_sprint_name = f"[{marker}] Existing target sprint"
    resources.exact_sprint_names.append(existing_sprint_name)
    existing_sprint = api.request_json(
        "POST", f"/api/spaces/{space_id}/sprints", json={"name": existing_sprint_name}
    )
    existing_sprint_id = int(existing_sprint["id"])
    resources.sprint_ids.append(existing_sprint_id)

    issues_before_start = api.request_json("GET", f"/api/spaces/{space_id}/issues")
    sprints_before_start = api.request_json("GET", f"/api/spaces/{space_id}/sprints")
    issue_keys_before_start = {row["issueKey"] for row in issues_before_start}
    sprint_ids_before_start = {int(row["id"]) for row in sprints_before_start}

    print("[4/9] Start durable AI planning and verify the approval pause")
    started, stage_labels = api.post_sse_result(
        "/api/ai/plan-epic/rollout/stream",
        {
            "proposal": (
                "Build a customer order CSV export with permission checks, an asynchronous export "
                "job, object storage download, progress UI, auditability, and automated tests."
            ),
            "spaceId": space_id,
            "existingLabels": [],
            "sprintCapacityPoints": 8,
            "targetSprintCount": 2,
        },
    )
    require(bool(stage_labels), "Plan Epic SSE returned no progress stage events")
    resources.thread_id = started["threadId"]
    require(started["status"] == "pending_approval", f"unexpected start status: {started['status']}")
    require(not started.get("degraded"), f"AI generation degraded: {started.get('error')}")
    generated_plan = started.get("plan") or {}
    require(generated_plan.get("epic") is not None, "AI generation returned no epic")
    require(bool(generated_plan.get("issues")), "AI generation returned no issues")
    discovered = api.request_json(
        "GET", f"/api/ai/plan-epic/rollout/active?space_id={space_id}"
    )
    require(discovered is not None, "active Plan Epic lookup did not find the pending workflow")
    require(discovered["threadId"] == resources.thread_id, "active lookup returned the wrong workflow")
    require(discovered["status"] == "pending_approval", "active lookup did not restore the approval pause")

    # Prove the durable pause has no product side effects before approval. Compare the complete sets,
    # not only marker-bearing titles: a buggy unmarked write must fail this assertion too.
    issues_during_pause = api.request_json("GET", f"/api/spaces/{space_id}/issues")
    sprints_during_pause = api.request_json("GET", f"/api/spaces/{space_id}/sprints")
    require(
        {row["issueKey"] for row in issues_during_pause} == issue_keys_before_start,
        "Plan Epic wrote Jira issues before approval",
    )
    require(
        {int(row["id"]) for row in sprints_during_pause} == sprint_ids_before_start,
        "Plan Epic wrote Jira sprints before approval",
    )

    print("[5/9] Exercise AI refine without writing Jira data")
    refined = api.request_json(
        "POST",
        "/api/ai/plan-epic/refine",
        json={
            "epic": generated_plan["epic"],
            "issues": generated_plan["issues"],
            "instruction": "Add explicit security review and end-to-end verification coverage.",
            "existingLabels": [],
            "sprintCapacityPoints": 8,
            "targetSprintCount": 2,
        },
    )
    require(not refined.get("degraded"), "AI refine degraded")
    require(bool(refined.get("issues")), "AI refine returned no issues")
    issues_after_refine = api.request_json("GET", f"/api/spaces/{space_id}/issues")
    sprints_after_refine = api.request_json("GET", f"/api/spaces/{space_id}/sprints")
    require(
        {row["issueKey"] for row in issues_after_refine} == issue_keys_before_start,
        "Plan Epic refine wrote Jira issues before approval",
    )
    require(
        {int(row["id"]) for row in sprints_after_refine} == sprint_ids_before_start,
        "Plan Epic refine wrote Jira sprints before approval",
    )

    print("[6/9] Submit the deterministic human-edited plan and publish it durably")
    epic, edited_issues = deterministic_edited_plan(marker)
    new_sprint_name = f"[{marker}] New workflow sprint"
    resources.exact_sprint_names.append(new_sprint_name)
    decision = api.request_json(
        "POST",
        f"/api/ai/plan-epic/rollout/{resources.thread_id}/decision",
        json={
            "decision": "edit",
            "epic": epic,
            "issues": edited_issues,
            "sprintTargets": [
                {
                    "sprintIndex": 0,
                    "issueTempIds": ["1", "2"],
                    "mode": "existing",
                    "sprintId": existing_sprint_id,
                    "sprintName": None,
                },
                {
                    "sprintIndex": 1,
                    "issueTempIds": ["3", "4"],
                    "mode": "new",
                    "sprintId": None,
                    "sprintName": new_sprint_name,
                },
            ],
        },
    )
    require(decision["status"] == "committed", f"publish ended as {decision['status']}: {decision.get('error')}")
    resources.epic_key = decision.get("epicIssueKey")
    committed = decision.get("committedIssueKeys") or {}
    resources.child_keys = list(committed.values())
    require(resources.epic_key is not None, "committed workflow returned no epic key")
    require(len(committed) == 4, f"expected 4 committed issues, got {committed}")

    print("[7/9] Read back Jira data and verify structure, sprint placement, and dependencies")
    sprints = api.request_json("GET", f"/api/spaces/{space_id}/sprints")
    new_matches = [s for s in sprints if s.get("name") == new_sprint_name]
    require(len(new_matches) == 1, f"expected one new workflow sprint, found {len(new_matches)}")
    new_sprint_id = int(new_matches[0]["id"])
    resources.sprint_ids.append(new_sprint_id)

    epic_row = api.request_json("GET", f"/api/spaces/{space_id}/issues/{resources.epic_key}")
    require(epic_row["title"] == epic["title"], "persisted epic title does not match human edit")
    require(epic_row["issueType"] == "epic", "persisted parent is not an epic")
    require(set(epic_row.get("childKeys") or []) == set(resources.child_keys), "epic children mismatch")

    by_temp_id: dict[str, dict[str, Any]] = {}
    for temp_id, issue_key in committed.items():
        row = api.request_json("GET", f"/api/spaces/{space_id}/issues/{issue_key}")
        by_temp_id[temp_id] = row
        expected = next(i for i in edited_issues if i["tempId"] == temp_id)
        require(row["title"] == expected["title"], f"title mismatch for {issue_key}")
        require(row["parentKey"] == resources.epic_key, f"parent mismatch for {issue_key}")
        require(row["reporterId"] == user["id"], f"reporter mismatch for {issue_key}")
        require(
            f"Estimate rationale: {expected['estimateRationale']}" in (row.get("description") or ""),
            f"estimate rationale missing from {issue_key}",
        )
        expected_sprint_id = existing_sprint_id if temp_id in {"1", "2"} else new_sprint_id
        require(int(row["sprintId"]) == expected_sprint_id, f"sprint mismatch for {issue_key}")

    verify_dependency(by_temp_id["2"], committed["1"])
    verify_dependency(by_temp_id["3"], committed["1"])
    verify_dependency(by_temp_id["4"], committed["2"])
    verify_dependency(by_temp_id["4"], committed["3"])

    all_issues = api.request_json("GET", f"/api/spaces/{space_id}/issues")
    marked = [i for i in all_issues if marker in (i.get("title") or "")]
    require(len(marked) == 5, f"expected exactly 5 marker-bearing Jira issues, found {len(marked)}")
    print(
        f"      verified epic={resources.epic_key}, children={resources.child_keys}, "
        f"sprints={resources.sprint_ids}"
    )

    print("[8/9] Verify durable workflow status is readable through the gateway")
    status = api.request_json("GET", f"/api/ai/plan-epic/rollout/{resources.thread_id}")
    require(status["status"] == "committed", f"status read returned {status['status']}")
    require(status.get("committedIssueKeys") == committed, "status committed ledger mismatch")
    active_after_commit = api.request_json(
        "GET", f"/api/ai/plan-epic/rollout/active?space_id={space_id}"
    )
    require(active_after_commit is None, "completed Plan Epic workflow was incorrectly auto-restored")


def _delete_checkpoint_thread(dsn: str, thread_id: str) -> None:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - the ai-service venv always installs psycopg
        raise E2EFailure("psycopg is required to clean LangGraph checkpoints") from exc

    with psycopg.connect(dsn, autocommit=True) as conn:
        for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
            conn.execute(
                f"DELETE FROM planning_workflows.{table} WHERE thread_id = %s",  # noqa: S608 - fixed identifiers
                (thread_id,),
            )
        for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
            remaining = conn.execute(
                f"SELECT COUNT(*) FROM planning_workflows.{table} WHERE thread_id = %s",  # noqa: S608
                (thread_id,),
            ).fetchone()[0]
            require(remaining == 0, f"checkpoint cleanup left {remaining} rows in {table}")
        conn.execute(
            "DELETE FROM planning_workflows.plan_epic_active_threads WHERE thread_id = %s",
            (thread_id,),
        )
        remaining_active = conn.execute(
            "SELECT COUNT(*) FROM planning_workflows.plan_epic_active_threads WHERE thread_id = %s",
            (thread_id,),
        ).fetchone()[0]
        require(remaining_active == 0, "checkpoint cleanup left an active-thread index row")


def _vector_rows_remaining(
    dsn: str, issue_keys: list[str], sprint_ids: list[int]
) -> dict[str, int]:
    import psycopg

    counts: dict[str, int] = {}
    with psycopg.connect(dsn) as conn:
        if issue_keys:
            for table in ("chunks", "issues", "issue_changes"):
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE issue_key = ANY(%s)",  # noqa: S608
                    (issue_keys,),
                ).fetchone()[0]
        if sprint_ids:
            counts["sprint_chunks"] = conn.execute(
                "SELECT COUNT(*) FROM chunks "
                "WHERE chunk_type = 'sprint' AND source_id = ANY(%s)",
                (sprint_ids,),
            ).fetchone()[0]
            counts["sprints"] = conn.execute(
                "SELECT COUNT(*) FROM sprints WHERE sprint_id = ANY(%s)",
                (sprint_ids,),
            ).fetchone()[0]
    return counts


def _wait_for_vector_cleanup(
    dsn: str,
    issue_keys: list[str],
    sprint_ids: list[int],
    timeout_seconds: float = 20.0,
) -> None:
    """Wait for Kafka delete events to purge this run's replicated vector/search data."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        counts = _vector_rows_remaining(dsn, issue_keys, sprint_ids)
        if not any(counts.values()):
            return
        if time.monotonic() >= deadline:
            raise E2EFailure(f"vector cleanup did not converge through Kafka: {counts}")
        time.sleep(0.25)


def _force_delete_vector_rows(dsn: str, issue_keys: list[str], sprint_ids: list[int]) -> None:
    """Exact-key fallback so a failed Kafka cleanup assertion never leaves E2E residue behind."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        if issue_keys:
            for table in ("chunks", "issue_changes", "issues"):
                conn.execute(
                    f"DELETE FROM {table} WHERE issue_key = ANY(%s)",  # noqa: S608
                    (issue_keys,),
                )
        if sprint_ids:
            conn.execute(
                "DELETE FROM chunks WHERE chunk_type = 'sprint' AND source_id = ANY(%s)",
                (sprint_ids,),
            )
            conn.execute("DELETE FROM sprints WHERE sprint_id = ANY(%s)", (sprint_ids,))
    remaining = _vector_rows_remaining(dsn, issue_keys, sprint_ids)
    require(not any(remaining.values()), f"forced vector cleanup left rows behind: {remaining}")


def cleanup(
    api: GatewayClient,
    args: argparse.Namespace,
    resources: CreatedResources,
    marker: str,
    space_id: int | None,
) -> list[str]:
    if args.keep_data:
        print("[9/9] Cleanup skipped because --keep-data was explicitly set")
        return []

    print("[9/9] Cleanup all Jira and LangGraph data created by this run")
    errors: list[str] = []
    if space_id is not None:
        # Recover keys/ids by exact marker if an HTTP response was lost after the server committed.
        try:
            current_issues = api.request_json("GET", f"/api/spaces/{space_id}/issues")
            marked_epics = [
                row for row in current_issues
                if row.get("issueType") == "epic" and row.get("title") == f"[{marker}] Durable order export"
            ]
            if resources.epic_key is None and len(marked_epics) == 1:
                resources.epic_key = marked_epics[0]["issueKey"]
            discovered_children = [
                row["issueKey"] for row in current_issues
                if row.get("issueType") != "epic" and marker in (row.get("title") or "")
            ]
            resources.child_keys = sorted(set(resources.child_keys) | set(discovered_children))
        except Exception as exc:  # noqa: BLE001 - cleanup continues through independent targets
            errors.append(f"could not discover marker-bearing issues: {exc}")

        if resources.epic_key:
            try:
                api.delete(f"/api/spaces/{space_id}/issues/{resources.epic_key}")
            except Exception as exc:  # noqa: BLE001
                # A previous partial cleanup may already have removed it; verify before reporting.
                try:
                    api.request_json("GET", f"/api/spaces/{space_id}/issues/{resources.epic_key}")
                except Exception:
                    pass
                else:
                    errors.append(f"failed deleting epic {resources.epic_key}: {exc}")

        try:
            current_sprints = api.request_json("GET", f"/api/spaces/{space_id}/sprints")
            exact_names = set(resources.exact_sprint_names)
            discovered_ids = {
                int(s["id"]) for s in current_sprints if s.get("name") in exact_names
            }
            resources.sprint_ids = sorted(set(resources.sprint_ids) | discovered_ids)
            for sprint_id in sorted(resources.sprint_ids, reverse=True):
                try:
                    api.delete(f"/api/spaces/{space_id}/sprints/{sprint_id}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"failed deleting sprint {sprint_id}: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"could not discover/delete test sprints: {exc}")

    if resources.thread_id:
        try:
            _delete_checkpoint_thread(args.checkpoint_db_url, resources.thread_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"failed deleting checkpoint thread {resources.thread_id}: {exc}")

    issue_keys = sorted(
        set(resources.child_keys) | ({resources.epic_key} if resources.epic_key else set())
    )
    sprint_ids = sorted(set(resources.sprint_ids))
    if issue_keys or sprint_ids:
        try:
            _wait_for_vector_cleanup(args.vector_db_url, issue_keys, sprint_ids)
        except Exception as exc:  # noqa: BLE001
            try:
                _force_delete_vector_rows(args.vector_db_url, issue_keys, sprint_ids)
            except Exception as force_exc:  # noqa: BLE001
                errors.append(f"vector cleanup failed: {exc}; exact-key fallback failed: {force_exc}")
            else:
                errors.append(f"Kafka vector cleanup failed (exact-key fallback succeeded): {exc}")

    if space_id is not None:
        try:
            remaining_issues = api.request_json("GET", f"/api/spaces/{space_id}/issues")
            leftovers = [i["issueKey"] for i in remaining_issues if marker in (i.get("title") or "")]
            require(not leftovers, f"marker-bearing issues remain after cleanup: {leftovers}")
            remaining_sprints = api.request_json("GET", f"/api/spaces/{space_id}/sprints")
            sprint_leftovers = [
                s["id"] for s in remaining_sprints if s.get("name") in set(resources.exact_sprint_names)
            ]
            require(not sprint_leftovers, f"test sprints remain after cleanup: {sprint_leftovers}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"post-cleanup verification failed: {exc}")

    if errors:
        print("      cleanup FAILED")
        for error in errors:
            print(f"      - {error}")
    else:
        print(
            "      cleanup verified: zero Jira, vector/search-index, and thread-checkpoint residue"
        )
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", default=os.getenv("E2E_GATEWAY_URL", "http://localhost:8080"))
    parser.add_argument("--ai-url", default=os.getenv("E2E_AI_URL", "http://localhost:8200"))
    parser.add_argument("--vector-url", default=os.getenv("E2E_VECTOR_URL", "http://localhost:8100"))
    parser.add_argument("--frontend-url", default=os.getenv("E2E_FRONTEND_URL", "http://localhost:5173"))
    parser.add_argument("--llm-url", default=os.getenv("E2E_LLM_URL", "http://localhost:1234"))
    parser.add_argument("--username", default=os.getenv("E2E_USERNAME", "alice"))
    parser.add_argument("--password", default=os.getenv("E2E_PASSWORD", "123"))
    parser.add_argument("--space-id", type=int, default=None)
    parser.add_argument("--timeout-seconds", type=float, default=240.0)
    parser.add_argument(
        "--checkpoint-db-url",
        default=os.getenv("AI_EPIC_ROLLOUT_CHECKPOINT_DB_URL", DEFAULT_CHECKPOINT_DSN),
    )
    parser.add_argument(
        "--vector-db-url",
        default=os.getenv("E2E_VECTOR_DB_URL", DEFAULT_VECTOR_DSN),
    )
    parser.add_argument("--keep-data", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    marker = f"PLAN-E2E-{uuid.uuid4().hex[:8].upper()}"
    resources = CreatedResources()
    api = GatewayClient(args.gateway_url, args.timeout_seconds)
    test_error: BaseException | None = None
    cleanup_errors: list[str] = []
    selected_space_id: int | None = args.space_id

    print(f"Plan Epic full-stack E2E run marker: {marker}")
    try:
        preflight(args)
        # Capture the selected space for cleanup even if a later step fails. The helper performs its
        # own login; this lightweight pre-read mirrors its deterministic selection rule.
        auth = api.request_json(
            "POST", "/api/auth/token", json={"username": args.username, "password": args.password}
        )
        api.set_token(auth["accessToken"])
        visible = api.request_json("GET", f"/api/spaces?userId={auth['user']['id']}")
        if selected_space_id is None and visible:
            preferred = next((s for s in visible if int(s["id"]) == 5000018), None)
            selected_space_id = int((preferred or visible[0])["id"])
        execute_flow(api, args, resources, marker)
    except BaseException as exc:  # includes Ctrl-C so cleanup still runs
        test_error = exc
    finally:
        cleanup_errors = cleanup(api, args, resources, marker, selected_space_id)
        api.close()

    if cleanup_errors:
        summary = "; ".join(cleanup_errors)
        if test_error is not None:
            raise E2EFailure(f"test failed with {test_error!r}; cleanup also failed: {summary}") from test_error
        raise E2EFailure(f"test assertions passed but cleanup failed: {summary}")
    if test_error is not None:
        raise test_error.with_traceback(test_error.__traceback__)

    print("PASS: integrated Plan Epic workflow and automatic cleanup both succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
