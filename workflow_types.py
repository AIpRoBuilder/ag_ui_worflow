from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

from ag_ui.core import InputContent, UserMessage


UserInput = str | UserMessage | list[InputContent]
StepRunInput = UserInput | bytes | bytearray | dict[str, Any] | list[Any] | None


class RequiredStepMeta(TypedDict):
    """Keys every engine step entry must provide."""

    id: str
    dependencies: list[str]


class StepMeta(RequiredStepMeta, total=False):
    """Step metadata exchanged with ``WorkflowEngine`` and AG-UI events.

    ``id`` and ``dependencies`` are required. The remaining keys are optional:
    ``title``/``prompt`` are used by step cards and input-request events;
    descriptor fields are carried through for UI consumers but are not read by
    the engine. Minimal metadata such as ``{"id": ..., "dependencies": []}`` is
    valid.
    """

    title: str
    prompt: str
    inputRequired: bool
    metaNodeKind: str
    repeatable: bool
    branches: list[str]

    # Optional descriptor payload produced by ``WorkflowStepNode.step_meta()``.
    metaDescription: str
    structure: str
    function: str
    implementationGuide: str
    example: str


class StepMetaProvider(Protocol):
    """Structural contract for node classes that expose step metadata."""

    @classmethod
    def step_meta(cls) -> StepMeta: ...


@dataclass(slots=True)
class StepRunOutput:
    card: dict[str, Any] = field(default_factory=dict)
    derived: dict[str, Any] = field(default_factory=dict)


def step_output_text(output: StepRunOutput) -> str:
    card = output.card if isinstance(output.card, dict) else {}
    derived = output.derived if isinstance(output.derived, dict) else {}

    for source in (card, derived):
        for key in ("summary", "label", "response", "message", "text"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    file_count = card.get("fileCount")
    if isinstance(file_count, int):
        return f"Saved {file_count} file(s)."

    service_name = card.get("service") or derived.get("service_name")
    service_status = card.get("status")
    service_pid = card.get("pid") or derived.get("pid")
    if service_name and service_status:
        pid_suffix = f" (pid={service_pid})" if service_pid is not None else ""
        return f"Service {service_name} is {service_status}{pid_suffix}."

    if card:
        return json.dumps(card, ensure_ascii=False)
    if derived:
        return json.dumps(derived, ensure_ascii=False)
    return ""


class WorkflowStepDefinition(Protocol):
    id: str
    title: str
    prompt: str
    dependencies: list[str]
    inputRequired: bool


class WorkflowConditionDefinition(Protocol):
    id: str
    title: str
    prompt: str
    dependencies: list[str]
    branches: list[str]
    inputRequired: bool