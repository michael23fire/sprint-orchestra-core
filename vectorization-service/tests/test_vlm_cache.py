"""Durable cache behaviour for paid VLM calls, including in-process retry coalescing."""
import asyncio

from app.ingest.vlm_cache import CachedVlmImageDescriber


class _Store:
    def __init__(self):
        self.rows: dict[str, str] = {}
        self.puts = []

    async def get_vlm_result(self, key):
        return self.rows.get(key)

    async def put_vlm_result(self, key, namespace, mime_type, content):
        self.puts.append((key, namespace, mime_type, content))
        self.rows.setdefault(key, content)


class _Vlm:
    def __init__(self, result="extracted text"):
        self.result = result
        self.calls = 0

    async def describe(self, image_data, mime_type):
        self.calls += 1
        await asyncio.sleep(0)
        return self.result

    async def aclose(self):
        return None


async def test_completed_vlm_result_is_reused_after_a_new_wrapper_simulates_restart():
    store = _Store()
    first_delegate = _Vlm("SKU: W-7734")
    first = CachedVlmImageDescriber(first_delegate, store, "prompt-v1:model-a")

    assert await first.describe(b"same image", "image/png") == "SKU: W-7734"
    assert first_delegate.calls == 1
    assert len(store.puts) == 1

    resumed_delegate = _Vlm("should not be called")
    resumed = CachedVlmImageDescriber(resumed_delegate, store, "prompt-v1:model-a")
    assert await resumed.describe(b"same image", "image/png") == "SKU: W-7734"
    assert resumed_delegate.calls == 0


async def test_same_image_concurrent_retries_share_one_paid_vlm_call():
    store = _Store()
    delegate = _Vlm("recovered text")
    describer = CachedVlmImageDescriber(delegate, store, "prompt-v1:model-a")

    results = await asyncio.gather(
        describer.describe(b"same image", "image/png"),
        describer.describe(b"same image", "image/png"),
    )

    assert results == ["recovered text", "recovered text"]
    assert delegate.calls == 1
    assert len(store.puts) == 1


async def test_model_or_prompt_namespace_change_does_not_reuse_old_result():
    store = _Store()
    first = CachedVlmImageDescriber(_Vlm("old extraction"), store, "prompt-v1:model-a")
    second_delegate = _Vlm("new extraction")
    second = CachedVlmImageDescriber(second_delegate, store, "prompt-v2:model-a")

    await first.describe(b"same image", "image/png")
    assert await second.describe(b"same image", "image/png") == "new extraction"
    assert second_delegate.calls == 1
