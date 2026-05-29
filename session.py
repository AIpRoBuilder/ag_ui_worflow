from dataclasses import dataclass, field
from typing import Any, Callable
import uuid
from .types import StepRunOutput


@dataclass(slots=True)
class WorkflowSession:
    thread_id: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    step_outputs: dict[str, StepRunOutput] = field(default_factory=dict)
    step_cards: dict[str, dict[str, Any]] = field(default_factory=dict)
    step_states: dict[str, str] = field(default_factory=dict)
    streamed_text_deltas: dict[str, list[str]] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    pending_inputs: dict[str, Any] = field(default_factory=dict)
    submit_callbacks: dict[str, Callable[[StepRunOutput], None]] = field(default_factory=dict)


_BOUND_SESSION: WorkflowSession | None = None


def bind_workflow_session(session: WorkflowSession) -> None:
    global _BOUND_SESSION
    _BOUND_SESSION = session


def unbind_workflow_session() -> None:
    global _BOUND_SESSION
    _BOUND_SESSION = None


def get_bound_workflow_session() -> WorkflowSession:
    session = _BOUND_SESSION
    if session is None:
        raise RuntimeError("Workflow session is not bound")
    return session