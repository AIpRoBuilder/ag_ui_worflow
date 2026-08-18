from dataclasses import dataclass, field
from typing import Any, Callable
import uuid
from .tools import get_pipeline_id
from .workflow_types import StepRunInput, StepRunOutput


@dataclass(slots=True)
class WorkflowSession:
    thread_id: str
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    step_outputs: dict[str, StepRunOutput] = field(default_factory=dict)
    step_cards: dict[str, dict[str, Any]] = field(default_factory=dict)
    step_states: dict[str, str] = field(default_factory=dict)
    streamed_text_deltas: dict[str, list[str]] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    pending_inputs: dict[str, StepRunInput] = field(default_factory=dict)
    submit_callbacks: dict[str, Callable[[StepRunOutput], None]] = field(default_factory=dict)


_BOUND_SESSIONS: dict[str, WorkflowSession] = {}


def bind_workflow_session(session: WorkflowSession) -> None:
    if session.thread_id in _BOUND_SESSIONS:
        return
    else:
        _BOUND_SESSIONS[session.thread_id] = session


def unbind_workflow_session(thread_id: str) -> None:
    _BOUND_SESSIONS.pop(thread_id, None)


def get_bound_workflow_session(thread_id: str) -> WorkflowSession:
    session = _BOUND_SESSIONS.get(thread_id)
    if session is None:
        raise RuntimeError("Workflow session is not bound")
    return session


def get_node_workflow_session(owner: Any) -> WorkflowSession:
    thread_id = get_pipeline_id(owner)
    if not thread_id:
        raise RuntimeError("Workflow thread id is not attached to the pipeline")
    return get_bound_workflow_session(thread_id)