"""Tests for AI-assisted task creation (app/drafting/) — a fake instructor-shaped client, no network.

Covers the two things that actually matter here: (1) a successful call returns the validated
TaskDraft untouched, and (2) any failure — model down, validation retries exhausted, whatever —
degrades to a safe, always-valid fallback instead of raising, since this is a user-facing "help me
create a task" call that must never block someone from creating *something*.
"""
from app.drafting.prompts import render_task_draft_prompt
from app.drafting.schemas import TaskDraft
from app.drafting.service import draft_task


class _FakeCompletions:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeInstructorClient:
    def __init__(self, result):
        self.completions = _FakeCompletions(result)
        self.chat = _FakeChat(self.completions)


async def test_draft_task_returns_validated_draft_on_success():
    expected = TaskDraft(
        title="Add rate limiting to login endpoint", issue_type="bug",
        labels=["security"], estimate_story_points=3, dependencies=["gateway auth work"],
    )
    client = FakeInstructorClient(expected)

    result = await draft_task(client, "fake-model", "someone was hammering the login endpoint")

    assert result.draft == expected
    assert result.degraded is False
    assert result.error is None
    assert client.completions.calls[0]["response_model"] is TaskDraft


async def test_draft_task_degrades_safely_when_the_model_call_fails():
    client = FakeInstructorClient(RuntimeError("model unreachable"))

    result = await draft_task(client, "fake-model", "some task\nmore detail nobody will read")

    assert result.degraded is True
    assert result.draft.title == "some task"  # first line of the raw description, not invented
    assert result.draft.labels == []
    assert result.draft.estimate_story_points is None
    assert result.error == "model unreachable"


async def test_draft_task_fallback_handles_blank_description():
    client = FakeInstructorClient(RuntimeError("boom"))

    result = await draft_task(client, "fake-model", "   ")

    assert result.draft.title == "Untitled task"


async def test_draft_task_passes_existing_labels_into_the_rendered_system_prompt():
    client = FakeInstructorClient(TaskDraft(title="x"))

    await draft_task(client, "fake-model", "desc", existing_labels=["urgent-fix"])

    system_message = client.completions.calls[0]["messages"][0]["content"]
    assert "urgent-fix" in system_message


def test_render_without_existing_labels_omits_the_reuse_instruction():
    prompt = render_task_draft_prompt()
    assert "Prefer reusing" not in prompt


def test_render_with_existing_labels_includes_them_joined():
    prompt = render_task_draft_prompt(["backend", "urgent"])
    assert "backend, urgent" in prompt
