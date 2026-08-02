"""Fetch attachment binaries from object storage (MinIO locally, S3 in cloud).

The Kafka attachment message carries only a locator (``s3://bucket/key``) — not the bytes — because
binaries are large. This service pulls them with its own credentials, exactly the pattern the
tracking message's ``storageUri`` field was designed for.
"""
from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Optional
from urllib.parse import urlparse

from app.config import Settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _s3_client(endpoint: str, access_key: str, secret_key: str, region: str):
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )


def _parse_uri(storage_uri: Optional[str], bucket: Optional[str], key: Optional[str]):
    if storage_uri and storage_uri.startswith("s3://"):
        parsed = urlparse(storage_uri)
        return parsed.netloc, parsed.path.lstrip("/")
    if bucket and key:
        return bucket, key
    return None, None


def _fetch_sync(settings: Settings, bucket: str, key: str) -> bytes:
    client = _s3_client(
        settings.s3_endpoint, settings.s3_access_key, settings.s3_secret_key, settings.s3_region
    )
    obj = client.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read()


async def fetch_attachment(
    settings: Settings,
    storage_uri: Optional[str],
    bucket: Optional[str],
    key: Optional[str],
) -> Optional[bytes]:
    """Return the binary, or ``None`` if it can't be located/fetched (logged, non-fatal)."""
    resolved_bucket, resolved_key = _parse_uri(storage_uri, bucket, key)
    if not resolved_bucket or not resolved_key:
        logger.warning("attachment has no resolvable storage location", extra={"uri": storage_uri})
        return None
    try:
        return await asyncio.to_thread(_fetch_sync, settings, resolved_bucket, resolved_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "attachment fetch failed",
            extra={"bucket": resolved_bucket, "key": resolved_key, "error": str(exc)},
        )
        return None
