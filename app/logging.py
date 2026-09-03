from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|cookie|password|session|csrf|api[_-]?key|token)\s*[=:]\s*([^\s,;]+)"
)


def redact(value: object) -> str:
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    return _SECRET_PATTERN.sub(r"\1=[REDACTED]", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        body: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in (
            "request_id",
            "job_id",
            "tool_call_id",
            "service",
            "scan_id",
            "relative_path",
            "reason",
        ):
            value = getattr(record, key, None)
            if value is not None:
                body[key] = redact(value)[:300]
        if record.exc_info:
            body["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.name = "music-agent-json"
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    # Replace only our own handler. Test capture handlers and embedding
    # applications may legitimately attach independent root handlers.
    root.handlers[:] = [
        existing for existing in root.handlers if existing.name != "music-agent-json"
    ]
    root.addHandler(handler)
    root.setLevel(level.upper())
