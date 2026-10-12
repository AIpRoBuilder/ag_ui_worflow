from __future__ import annotations

import uuid
from typing import Any

from ag_ui.core import (
    CustomEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)

from .workflow_types import StepRunOutput


class WorkflowEventFactory:
    def start_event(self, thread_id: str, request_id: str) -> RunStartedEvent:
        return RunStartedEvent(threadId=thread_id, runId=request_id)

    def finish_event(self, thread_id: str, request_id: str, result: Any | None = None) -> RunFinishedEvent:
        return RunFinishedEvent(threadId=thread_id, runId=request_id, result=result)

    def error_event(self, message: str, code: str | None = None) -> RunErrorEvent:
        return RunErrorEvent(message=message, code=code)

    def step_started_event(self, step_name: str) -> StepStartedEvent:
        return StepStartedEvent(stepName=step_name)

    def step_finished_event(self, step_name: str) -> StepFinishedEvent:
        return StepFinishedEvent(stepName=step_name)

    def message_events(
        self,
        content: str,
        role: str = "assistant",
        deltas: list[str] | None = None,
    ) -> list[Any]:
        message_id = str(uuid.uuid4())
        content_parts = deltas if deltas else [content]
        events: list[Any] = [TextMessageStartEvent(messageId=message_id, role=role)]
        for part in content_parts:
            if part:
                events.append(TextMessageContentEvent(messageId=message_id, delta=part))
        events.append(TextMessageEndEvent(messageId=message_id))
        return events

    def step_card_event(
        self,
        *,
        step: dict[str, Any],
        output: StepRunOutput,
        state: str,
        unlocked: bool,
        is_final: bool,
    ) -> CustomEvent:
        step_id = step["id"]

        event_payload = {
            "stepId": step_id,
            "title": step.get("title", ""),
            "prompt": step.get("prompt", ""),
            "state": state,
            "card": output.card,
            "unlocked": unlocked,
            "isFinal": is_final,
        }
        return CustomEvent(name="step_card", value=event_payload)

    def input_required_event(self, *, step: dict[str, Any]) -> CustomEvent:
        return CustomEvent(
            name="input_required",
            value={
                "stepId": step["id"],
                "title": step.get("title", ""),
                "prompt": step.get("prompt", ""),
            },
        )