from __future__ import annotations

from pathlib import Path
import uuid
from typing import Any, Callable, Iterator, Generator
from pydaograph import CStatus, GPipeline
from .events import WorkflowEventFactory
from .tools import set_pipeline_id
from .workflow_types import StepRunInput, StepRunOutput, step_output_text
from .session import WorkflowSession, bind_workflow_session, unbind_workflow_session
from .streaming import to_sse_payload


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
        self._events = WorkflowEventFactory()
        self._sync_steps_meta_to_session_state()
        self._build_pipeline()

    def _sync_steps_meta_to_session_state(self) -> None:
        self.session.state["_workflow_step_meta_map"] = {
            str(step.get("id", "")).strip(): dict(step)
            for step in self.steps_meta
            if str(step.get("id", "")).strip()
        }

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
            normalized_steps.append(step)

        return normalized_steps

    def _build_pipeline(self) -> None:
        self.pipeline = GPipeline()
        status = self.pipeline.buildFromJson(self.pipeline_json_path)
        if status.isErr():
            raise RuntimeError(f"buildFromJson failed: {status.getInfo()}")

        set_pipeline_id(self.pipeline, self.thread_id)

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
        node_kind = str(step.get("nodeKind", "")).strip().lower()
        input_required = bool(step.get("inputRequired", True))

        if node_kind == "operation":
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
        user_input: StepRunInput,
        *,
        callback: Callable[[StepRunOutput], None] | None = None,
    ) -> CStatus:
        if self.pipeline is None:
            return CStatus(1006, "pipeline is not initialized")

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
            unbind_workflow_session(self.session.thread_id)

    def run_all_steps(
        self,
        step_inputs: dict[str, StepRunInput] | None = None,
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
            )
            if status.isErr():
                return status

        return CStatus()

    def _resolve_step_output(self, step_id: str) -> StepRunOutput | None:
        output = self.session.step_outputs.get(step_id)
        if output is not None:
            return output

        card_payload = self.session.step_cards.get(step_id)
        step_state = str(self.session.step_states.get(step_id, "")).strip().lower()
        if card_payload is None and step_state not in {"completed", "done", "finished", "success", "succeeded"}:
            return None

        synthesized = StepRunOutput(
            card=card_payload if isinstance(card_payload, dict) else {},
            derived={},
        )
        self.session.step_outputs[step_id] = synthesized
        return synthesized

    def _execute_step_events(
        self,
        step: dict[str, Any],
        step_input: StepRunInput,
        terminal_ids: set[str],
    ) -> Generator[str, None, tuple[bool, str, StepRunOutput | None]]:
        captured_output: dict[str, StepRunOutput] = {}

        def _on_submit(output: StepRunOutput) -> None:
            captured_output["value"] = output

        step_id = str(step.get("id", "")).strip()
        yield to_sse_payload(self._events.step_started_event(step_name=step_id))

        status = self.run_step(step_id, step_input, callback=_on_submit)
        if status.isErr():
            yield to_sse_payload(self._events.error_event(message=status.getInfo(), code=str(status.getCode())))
            return False, step_id, None

        output = captured_output.get("value") or self._resolve_step_output(step_id)
        if output is None:
            yield to_sse_payload(
                self._events.error_event(
                    message=f"step output missing after proceed/run for {step_id}",
                    code="missing_step_output",
                )
            )
            return False, step_id, None

        streamed_deltas = self.session.streamed_text_deltas.pop(step_id, None)
        for event in self._events.message_events(content=step_output_text(output), deltas=streamed_deltas):
            yield to_sse_payload(event)

        yield to_sse_payload(
            self._events.step_card_event(
                session=self.session,
                step=step,
                output=output,
                unlocked=True,
                is_final=(step_id in terminal_ids),
            )
        )
        yield to_sse_payload(self._events.step_finished_event(step_name=step_id))
        return True, step_id, output

    def _run_step_events(self, step_id: str, user_input: StepRunInput) -> Iterator[str]:
        terminal_ids = self._terminal_step_ids()
        yield to_sse_payload(self._events.start_event(self.session))
        last_step_id = ""
        last_output: StepRunOutput | None = None

        first_step = self.get_step_meta(step_id)
        first_result = yield from self._execute_step_events(
            first_step,
            user_input,
            terminal_ids,
        )
        ok, last_step_id, last_output = first_result
        if not ok:
            yield to_sse_payload(self._events.finish_event(self.session, result={"ok": False, "stepId": last_step_id}))
            return

        while True:
            auto_step = self._next_auto_runnable_step()
            if auto_step is None:
                break

            auto_result = yield from self._execute_step_events(
                auto_step,
                None,
                terminal_ids,
            )
            ok, last_step_id, last_output = auto_result
            if not ok:
                yield to_sse_payload(self._events.finish_event(self.session, result={"ok": False, "stepId": last_step_id}))
                return

        result: dict[str, Any] = {
            "ok": True,
            "stepId": last_step_id,
            "isFinal": last_step_id in terminal_ids,
            "completedSteps": list(self.session.step_outputs.keys()),
        }
        if result["isFinal"]:
            result["final"] = last_output.derived if last_output is not None else {}

        yield to_sse_payload(self._events.finish_event(self.session, result=result))

    def _run_all_steps_events(self, step_inputs: dict[str, StepRunInput] | None = None) -> Iterator[str]:
        terminal_ids = self._terminal_step_ids()
        inputs = step_inputs or {}
        self.session.run_id = str(uuid.uuid4())
        yield to_sse_payload(self._events.start_event(self.session))
        last_step_id = ""
        last_output: StepRunOutput | None = None

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
                    self._events.error_event(
                        message=f"input required for step {step_id}",
                        code="1008",
                    )
                )
                yield to_sse_payload(self._events.finish_event(self.session, result={"ok": False, "stepId": step_id}))
                return

            step_result = yield from self._execute_step_events(
                step,
                step_input,
                terminal_ids,
            )
            ok, last_step_id, last_output = step_result
            if not ok:
                yield to_sse_payload(self._events.finish_event(self.session, result={"ok": False, "stepId": last_step_id}))
                return

        result: dict[str, Any] = {
            "ok": True,
            "stepId": last_step_id,
            "isFinal": bool(last_step_id) and last_step_id in terminal_ids,
            "completedSteps": list(self.session.step_outputs.keys()),
        }
        if result["isFinal"]:
            result["final"] = last_output.derived if last_output is not None else {}

        yield to_sse_payload(self._events.finish_event(self.session, result=result))

