from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class StepRunOutput:
    summary: str
    card: dict[str, Any] = field(default_factory=dict)
    derived: dict[str, Any] = field(default_factory=dict)


class WorkflowStepDefinition(Protocol):
    id: str
    title: str
    prompt: str
    dependencies: list[str]
    services: list[dict[str, str]]
    inputRequired: bool
    nodeKind: str


class WorkflowConditionDefinition(Protocol):
    id: str
    title: str
    prompt: str
    dependencies: list[str]
    branches: list[str]
    inputRequired: bool
    nodeKind: str
