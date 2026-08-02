"""Structured logging setup.

Emits one JSON object per log line in production (easy to ship to Loki/CloudWatch and correlate
across services by ``event_id`` / ``issue_key``), or a plain human-readable line locally.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter with structured extras.

    Any keyword passed via ``logger.info(msg, extra={"issue_key": ...})`` that isn't a standard
    LogRecord attribute is merged into the JSON object, so we can carry correlation ids.
    """

    _RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-5s %(name)s | %(message)s")
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # aiokafka/docling are chatty at DEBUG; keep them at WARNING unless we're debugging.
    for noisy in ("aiokafka", "docling", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
