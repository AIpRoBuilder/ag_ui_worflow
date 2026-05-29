from __future__ import annotations

from pathlib import Path
import uuid
from typing import Any, Callable, Iterator, Generator
from pydaograph import CStatus, GPipeline
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
from .types import StepRunOutput
from .session import WorkflowSession, bind_workflow_session, unbind_workflow_session
from .streaming import to_sse_payload
from .condition import WorkflowConditionNode
from .nodes import WorkflowImageNode, WorkflowOperationNode, WorkflowStepNode


class WorkflowEngine:
    def __init__(
        self,
        *,
        pipeline_json_path: str,
        steps_meta: list[dict[str, Any]],
        thread_id: str,
    ) -> None:
        self.pipeline_json_path = str(Path(pipeline_json_path).resolve())
        self.steps_meta = self._normalize_steps_meta(steps_meta)
        self._step_map = {str(step["id"]).strip(): step for step in self.steps_meta if str(step.get("id", "")).strip()}
        self.thread_id = thread_id

        self.pipeline: GPipeline | None = None
        self.session = WorkflowSession(thread_id=thread_id)
        self._sync_steps_meta_to_session_state()
        self._build_pipeline()

    def _sync_steps_meta_to_session_state(self) -> None:
        self.session.state["_workflow_step_meta_map"] = {
            str(step.get("id", "")).strip(): dict(step)
            for step in self.steps_meta
            if str(step.get("id", "")).strip()
        }

    def _normalize_services(self, step: dict[str, Any]) -> list[dict[str, str]]:
        services_raw = step.get("services") or []
        if not isinstance(services_raw, list):
            return []

        normalized: list[dict[str, str]] = []
        for item in services_raw:
            if not isinstance(item, dict):
                continue
            service_name = str(item.get("service_name", "")).strip()
            use_desc = str(item.get("use_desc", "")).strip()
            if not service_name:
                continue
            normalized.append(
                {
                    "service_name": service_name,
                    "use_desc": use_desc,
                }
            )
        return normalized

    def _normalize_steps_meta(self, steps_meta: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized_steps: list[dict[str, Any]] = []
        for raw_step in steps_meta:
            if not isinstance(raw_step, dict):
                continue

            step = dict(raw_step)
            step_id = str(step.get("id", "")).strip()
            if not step_id:
                continue

            dependencies_raw = step.get("dependencies") or []
            if isinstance(dependencies_raw, (str, bytes)):
                dependencies_raw = [dependencies_raw]
            if not isinstance(dependencies_raw, list):
                dependencies_raw = []

            step["id"] = step_id
            step["dependencies"] = [str(dep).strip() for dep in dependencies_raw if str(dep).strip()]
            step["services"] = self._normalize_services(step)
            normalized_steps.append(step)

        return normalized_steps

    def _build_pipeline(self) -> None:
        self.pipeline = GPipeline()
        status = self.pipeline.buildFromJson(self.pipeline_json_path)
        if status.isErr():
            raise RuntimeError(f"buildFromJson failed: {status.getInfo()}")

        status = self.pipeline.init()
        if status.isErr():
            raise RuntimeError(f"pipeline.init failed: {status.getInfo()}")

    def reset_session(self) -> WorkflowSession:
        if self.pipeline is not None:
            self.pipeline.destroy()
        self.session = WorkflowSession(thread_id=self.thread_id)
        self._sync_steps_meta_to_session_state()
        self._build_pipeline()
        return self.session

    def get_step_meta(self, step_id: str) -> dict[str, Any]:
        return self._step_map[step_id]

    def _terminal_step_ids(self) -> set[str]:
        parents = {
            str(dep).strip()
            for step in self.steps_meta
            for dep in (step.get("dependencies") or [])
            if str(dep).strip()
        }
        return {
            str(step.get("id", "")).strip()
            for step in self.steps_meta
            if str(step.get("id", "")).strip() and str(step.get("id", "")).strip() not in parents
        }

    def _step_requires_user_input(self, step: dict[str, Any]) -> bool:
        ext_data = step.get("extData") or step.get("ext_data") or {}
        ext_type = ""
        ext_type = str(ext_data.get("type", "")).strip().lower() if isinstance(ext_data, dict) else str(ext_data).strip().lower()

        node_kind = str(step.get("nodeKind", "")).strip().lower()
        input_required = bool(step.get("inputRequired", True))

        if node_kind == "operation":
            return False
        if node_kind == "image" or ext_type == "image":
            return False
        if input_required is False:
            return False
        return True

    def _is_step_unlocked(self, step: dict[str, Any]) -> bool:
        dependencies = step.get("dependencies") or []
        if not isinstance(dependencies, list):
            return False
        return all(str(dep).strip() in self.session.step_outputs for dep in dependencies)

    def _next_auto_runnable_step(self) -> dict[str, Any] | None:
        for step in self.steps_meta:
            step_id = str(step.get("id", "")).strip()
            if not step_id:
                continue
            if step_id in self.session.step_outputs:
                continue
            if self._step_requires_user_input(step):
                continue
            if not self._is_step_unlocked(step):
                continue
            return step
        return None

    def run_step(
        self,
        step_id: str,
        user_input: Any,
        callback: Callable[[StepRunOutput], None] | None = None,
        *,
        preserve_run_id: bool = False,
    ) -> CStatus:
        if self.pipeline is None:
            return CStatus(1006, "pipeline is not initialized")

        if not preserve_run_id:
            self.session.run_id = str(uuid.uuid4())
        self.session.pending_inputs[step_id] = user_input
        if callback is not None:
            self.session.submit_callbacks[step_id] = callback

        bind_workflow_session(self.session)
        try:
            if step_id in self.session.step_outputs:
                output = self.session.step_outputs.get(step_id)
                callback = self.session.submit_callbacks.pop(step_id, None)
                if callback is not None and output is not None:
                    callback(output)
                self.session.pending_inputs.pop(step_id, None)
                return CStatus()

            max_iterations = max(1, len(self.steps_meta) * 2)
            for _ in range(max_iterations):
                before_count = len(self.session.step_outputs)

                status = self.pipeline.proceed()
                if step_id in self.session.step_outputs:
                    return CStatus()
                if status.isErr():
                    return status

                status = self.pipeline.run()
                if step_id in self.session.step_outputs:
                    return CStatus()
                if status.isErr():
                    return status

                after_count = len(self.session.step_outputs)
                if after_count == before_count:
                    break

            completed = ", ".join(self.session.step_outputs.keys()) or "none"
            return CStatus(
                1007,
                f"requested step {step_id} did not execute in current pipeline cycle; completed steps: {completed}",
            )
        finally:
            unbind_workflow_session()

    def run_all_steps(
        self,
        step_inputs: dict[str, Any] | None = None,
        callbacks: dict[str, Callable[[StepRunOutput], None]] | None = None,
    ) -> CStatus:
        if self.pipeline is None:
            return CStatus(1006, "pipeline is not initialized")

        inputs = step_inputs or {}
        submit_callbacks = callbacks or {}

        self.session.run_id = str(uuid.uuid4())

        for step in self.steps_meta:
            step_id = str(step.get("id", "")).strip()
            if not step_id:
                continue

            if step_id in self.session.step_outputs:
                continue

            has_input = step_id in inputs
            step_input = inputs.get(step_id)
            if self._step_requires_user_input(step) and not has_input:
                return CStatus(1008, f"input required for step {step_id}")

            status = self.run_step(
                step_id,
                step_input,
                callback=submit_callbacks.get(step_id),
                preserve_run_id=True,
            )
            if status.isErr():
                return status

        return CStatus()

    def _run_step_events(self, step_id: str, user_input: Any) -> Iterator[str]:
        terminal_ids = self._terminal_step_ids()
        yield to_sse_payload(self.start_event(self.session))
        last_step_id = ""
        last_output: StepRunOutput | None = None

        def _execute_step_events(
            step: dict[str, Any],
            step_input: Any,
            *,
            preserve_id: bool,
        ) -> Generator[str, None, tuple[bool, str, StepRunOutput | None]]:
            captured_output: dict[str, Any] = {}

            def _on_submit(output: StepRunOutput) -> None:
                captured_output["value"] = output

            sid = str(step.get("id", "")).strip()
            yield to_sse_payload(self.step_started_event(step_name=sid))

            status = self.run_step(sid, step_input, callback=_on_submit, preserve_run_id=preserve_id)
            if status.isErr():
                yield to_sse_payload(self.error_event(message=status.getInfo(), code=str(status.getCode())))
                return False, sid, None

            output = captured_output.get("value") or self.session.step_outputs.get(sid)
            if output is None:
                card_payload = self.session.step_cards.get(sid)
                step_state = str(self.session.step_states.get(sid, "")).strip().lower()
                if card_payload is not None or step_state in {"completed", "done", "finished", "success", "succeeded"}:
                    summary = ""
                    if isinstance(card_payload, dict):
                        summary = str(card_payload.get("summary", "")).strip() or str(card_payload.get("label", "")).strip()
                    if not summary:
                        summary = f"{sid} completed"
                    synthesized = StepRunOutput(
                        summary=summary,
                        card=card_payload if isinstance(card_payload, dict) else {},
                        derived={},
                    )
                    self.session.step_outputs[sid] = synthesized
                    output = synthesized
            if output is None:
                yield to_sse_payload(
                    self.error_event(
                        message=f"step output missing after proceed/run for {sid}",
                        code="missing_step_output",
                    )
                )
                return False, sid, None

            streamed_deltas = self.session.streamed_text_deltas.pop(sid, None)
            for event in self.message_events(content=output.summary, deltas=streamed_deltas):
                yield to_sse_payload(event)

            yield to_sse_payload(
                self.step_card_event(
                    step=step,
                    output=output,
                    unlocked=True,
                    is_final=(sid in terminal_ids),
                )
            )

            yield to_sse_payload(self.step_finished_event(step_name=sid))
            return True, sid, output

        first_step = self.get_step_meta(step_id)
        first_result = yield from _execute_step_events(first_step, user_input, preserve_id=False)
        ok, last_step_id, last_output = first_result
        if not ok:
            yield to_sse_payload(self.finish_event(self.session, result={"ok": False, "stepId": last_step_id}))
            return

        while True:
            auto_step = self._next_auto_runnable_step()
            if auto_step is None:
                break

            auto_result = yield from _execute_step_events(auto_step, None, preserve_id=True)
            ok, last_step_id, last_output = auto_result
            if not ok:
                yield to_sse_payload(self.finish_event(self.session, result={"ok": False, "stepId": last_step_id}))
                return

        result: dict[str, Any] = {
            "ok": True,
            "stepId": last_step_id,
            "isFinal": last_step_id in terminal_ids,
            "completedSteps": list(self.session.step_outputs.keys()),
        }
        if result["isFinal"]:
            result["final"] = last_output.derived if last_output is not None else {}

        yield to_sse_payload(self.finish_event(self.session, result=result))

    def _run_all_steps_events(self, step_inputs: dict[str, Any] | None = None) -> Iterator[str]:
        terminal_ids = self._terminal_step_ids()
        inputs = step_inputs or {}
        self.session.run_id = str(uuid.uuid4())
        yield to_sse_payload(self.start_event(self.session))
        last_step_id = ""
        last_output: StepRunOutput | None = None

        def _execute_step_events(
            step: dict[str, Any],
            step_input: Any,
        ) -> Generator[str, None, tuple[bool, str, StepRunOutput | None]]:
            captured_output: dict[str, Any] = {}

            def _on_submit(output: StepRunOutput) -> None:
                captured_output["value"] = output

            sid = str(step.get("id", "")).strip()
            yield to_sse_payload(self.step_started_event(step_name=sid))

            status = self.run_step(sid, step_input, callback=_on_submit, preserve_run_id=True)
            if status.isErr():
                yield to_sse_payload(self.error_event(message=status.getInfo(), code=str(status.getCode())))
                return False, sid, None

            output = captured_output.get("value") or self.session.step_outputs.get(sid)
            if output is None:
                card_payload = self.session.step_cards.get(sid)
                step_state = str(self.session.step_states.get(sid, "")).strip().lower()
                if card_payload is not None or step_state in {"completed", "done", "finished", "success", "succeeded"}:
                    summary = ""
                    if isinstance(card_payload, dict):
                        summary = str(card_payload.get("summary", "")).strip() or str(card_payload.get("label", "")).strip()
                    if not summary:
                        summary = f"{sid} completed"
                    synthesized = StepRunOutput(
                        summary=summary,
                        card=card_payload if isinstance(card_payload, dict) else {},
                        derived={},
                    )
                    self.session.step_outputs[sid] = synthesized
                    output = synthesized
            if output is None:
                yield to_sse_payload(
                    self.error_event(
                        message=f"step output missing after proceed/run for {sid}",
                        code="missing_step_output",
                    )
                )
                return False, sid, None

            streamed_deltas = self.session.streamed_text_deltas.pop(sid, None)
            for event in self.message_events(content=output.summary, deltas=streamed_deltas):
                yield to_sse_payload(event)

            yield to_sse_payload(
                self.step_card_event(
                    step=step,
                    output=output,
                    unlocked=True,
                    is_final=(sid in terminal_ids),
                )
            )

            yield to_sse_payload(self.step_finished_event(step_name=sid))
            return True, sid, output

        for step in self.steps_meta:
            step_id = str(step.get("id", "")).strip()
            if not step_id:
                continue

            if step_id in self.session.step_outputs:
                continue

            has_input = step_id in inputs
            step_input = inputs.get(step_id)
            if self._step_requires_user_input(step) and not has_input:
                yield to_sse_payload(
                    self.error_event(
                        message=f"input required for step {step_id}",
                        code="1008",
                    )
                )
                yield to_sse_payload(self.finish_event(self.session, result={"ok": False, "stepId": step_id}))
                return

            step_result = yield from _execute_step_events(step, step_input)
            ok, last_step_id, last_output = step_result
            if not ok:
                yield to_sse_payload(self.finish_event(self.session, result={"ok": False, "stepId": last_step_id}))
                return

        result: dict[str, Any] = {
            "ok": True,
            "stepId": last_step_id,
            "isFinal": bool(last_step_id) and last_step_id in terminal_ids,
            "completedSteps": list(self.session.step_outputs.keys()),
        }
        if result["isFinal"]:
            result["final"] = last_output.derived if last_output is not None else {}

        yield to_sse_payload(self.finish_event(self.session, result=result))

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
        events: list[Any] = [
            TextMessageStartEvent(messageId=message_id, role=role),
        ]
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
        unlocked: bool,
        is_final: bool,
    ) -> CustomEvent:
        step_id = step["id"]
        title = step.get("title", "")
        prompt = step.get("prompt", "")
        card_payload = self.session.step_cards.get(step_id)
        if card_payload is None:
            raise RuntimeError(f"step card payload missing for step {step_id}")
        step_state = self.session.step_states.get(step_id, "completed")

        event_payload = {
            "stepId": step_id,
            "title": title,
            "prompt": prompt,
            "state": step_state,
            "summary": output.summary,
            "card": card_payload,
            "derived": output.derived,
            "unlocked": unlocked,
            "isFinal": is_final,
        }
        return CustomEvent(name="step_card", value=event_payload)

