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

from .session import WorkflowSession
from .workflow_types import StepRunOutput


class WorkflowEventFactory:
    def start_event(self, session: WorkflowSession) -> RunStartedEvent:
        return RunStartedEvent(threadId=session.thread_id, runId=session.run_id)

    def finish_event(self, session: WorkflowSession, result: Any | None = None) -> RunFinishedEvent:
        return RunFinishedEvent(threadId=session.thread_id, runId=session.run_id, result=result)

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
        session: WorkflowSession,
        step: dict[str, Any],
        output: StepRunOutput,
        unlocked: bool,
        is_final: bool,
    ) -> CustomEvent:
        step_id = step["id"]

        event_payload = {
            "stepId": step_id,
            "title": step.get("title", ""),
            "prompt": step.get("prompt", ""),
            "state": session.step_states.get(step_id, "completed"),
            "card": output.card,
            "unlocked": unlocked,
            "isFinal": is_final,
        }
        return CustomEvent(name="step_card", value=event_payload)