from __future__ import annotations

import json
from typing import Any


def event_to_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(event, dict):
        return event
    raise TypeError(f"Unsupported event type for serialization: {type(event)!r}")


def to_sse_payload(event: Any) -> str:
    return f"data: {json.dumps(event_to_dict(event), ensure_ascii=False)}\n\n"
