"""Durable, content-addressed cache around paid VLM extraction calls.

Kafka ingestion is at-least-once.  A crash after the model answered but before the attachment chunks
or Kafka offset were committed must not bill the same image again on resume.  The cache therefore
persists successful non-empty VLM outputs before the caller continues with embedding/storage.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Protocol

from app.ingest.vlm_describer import VlmImageDescriber

logger = logging.getLogger(__name__)


class VlmResultStore(Protocol):
    async def get_vlm_result(self, cache_key: str) -> str | None: ...

    async def put_vlm_result(self, cache_key: str, namespace: str, mime_type: str, content: str) -> None: ...


class CachedVlmImageDescriber:
    """Adds persistent cache lookup plus single-flight protection to a real VLM describer.

    The DB row provides crash/restart durability; keyed asyncio locks avoid duplicate calls when two
    coroutines in the same service instance receive the same attachment during a short retry window.
    Cache-store outages never block ingestion: the underlying model result remains usable, but a
    warning makes the lost idempotency visible.
    """

    def __init__(self, delegate: VlmImageDescriber, store: VlmResultStore, namespace: str):
        self._delegate = delegate
        self._store = store
        self._namespace = namespace
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    def _cache_key(self, image_data: bytes, mime_type: str) -> str:
        digest = hashlib.sha256()
        digest.update(self._namespace.encode())
        digest.update(b"\0")
        digest.update(mime_type.encode())
        digest.update(b"\0")
        digest.update(image_data)
        return digest.hexdigest()

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())

    async def _cached(self, key: str) -> str | None:
        try:
            return await self._store.get_vlm_result(key)
        except Exception as exc:  # noqa: BLE001 - a cache outage must not drop a source document
            logger.warning("VLM result cache read failed", extra={"error": str(exc)})
            return None

    async def describe(self, image_data: bytes, mime_type: str) -> str:
        key = self._cache_key(image_data, mime_type)
        cached = await self._cached(key)
        if cached is not None:
            logger.info("VLM result cache hit", extra={"mime_type": mime_type})
            return cached

        lock = await self._lock_for(key)
        async with lock:
            # A coroutine may have completed the paid call while this one was waiting.
            cached = await self._cached(key)
            if cached is not None:
                logger.info("VLM result cache hit after single-flight wait", extra={"mime_type": mime_type})
                return cached

            result = await self._delegate.describe(image_data, mime_type)
            if result.strip():
                try:
                    await self._store.put_vlm_result(key, self._namespace, mime_type, result)
                except Exception as exc:  # noqa: BLE001 - return useful extraction even if cache persistence fails
                    logger.warning("VLM result cache write failed", extra={"error": str(exc)})
            return result

    async def aclose(self) -> None:
        await self._delegate.aclose()
