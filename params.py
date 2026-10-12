from __future__ import annotations

from threading import Condition
from typing import Any, Iterable

from pydaograph import CStatus, GParam

from .workflow_types import StepRunInput, StepRunOutput


WORKFLOW_PARAM_KEY = "workflow.request"


class WorkflowParam(GParam):
    """Per-pipeline data channel between the engine and the nodes of one run.

    One instance is registered on every pooled pipeline and cleared when the pipeline goes
    back to the pool. Nodes own their input callbacks; this object only stores inputs,
    outputs and synchronized run state.
    """

    def __init__(self) -> None:
        super().__init__()
        self.condition = Condition()
        self.clear()

    def clear(self) -> None:
        self.request_id = ""
        self.inputs: dict[str, StepRunInput] = {}
        self.outputs: dict[str, StepRunOutput] = {}
        self.cards: dict[str, dict[str, Any]] = {}
        self.states: dict[str, str] = {}
        self.state: dict[str, Any] = {}
        self.streamed_text_deltas: dict[str, list[str]] = {}
        self.completed: list[str] = []
        self.failed_step = ""
        self.failure: CStatus | None = None
        self.finished = False
        self.run_status = CStatus()

    @property
    def awaiting(self) -> list[str]:
        """Pauses reported by user-defined wait functions, in step insertion order."""
        return [step_id for step_id, state in self.states.items() if state == "awaiting_input"]

    def can_run(self, step_id: str, dependencies: Iterable[str]) -> bool:
        """True when the step is not finished yet and all its dependencies are."""
        return step_id not in self.outputs and all(dep in self.outputs for dep in dependencies)

    def set_state(self, step_id: str, state: str) -> None:
        """Report a step's state; wait functions use awaiting_input/running."""
        with self.condition:
            self.states[step_id] = state
            self.condition.notify_all()

    def complete(
        self,
        step_id: str,
        output: StepRunOutput,
        card: dict[str, Any],
        state: str = "completed",
    ) -> None:
        with self.condition:
            self.outputs[step_id] = output
            self.cards[step_id] = card
            self.states[step_id] = state
            self.completed.append(step_id)
            self.condition.notify_all()

    def fail(self, step_id: str, message: str, code: int = 1001) -> CStatus:
        with self.condition:
            self.states[step_id] = "failed"
            self.failed_step = step_id
            self.failure = CStatus(code, message)
            self.condition.notify_all()
            return self.failure

    def provide_input(self, step_id: str, value: StepRunInput) -> None:
        """Store input; only the subclass's wait function decides whether to proceed."""
        with self.condition:
            if step_id in self.outputs:
                return
            self.inputs[step_id] = value
            if self.states.get(step_id) == "awaiting_input":
                self.states[step_id] = "running"  # wait for the callback to accept or ask again
            self.condition.notify_all()

    def is_settled(self) -> bool:
        """True when the run ended or every step that can still move waits for input.

        Call with ``condition`` held.
        """
        return self.finished or self.failure is not None or bool(
            self.awaiting and not any(state in ("running", "evaluating") for state in self.states.values())
        )

    def finish(self, status: CStatus) -> None:
        with self.condition:
            self.run_status = status
            self.finished = True
            self.condition.notify_all()
