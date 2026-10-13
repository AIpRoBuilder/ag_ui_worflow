from __future__ import annotations

from abc import ABCMeta, abstractmethod
from typing import Any

from pydaograph import CStatus, GNode

from ...params import WORKFLOW_PARAM_KEY, WorkflowParam
from ...tools import node_main_utility, node_subclass_implementation
from ...workflow_types import StepRunOutput
from .._shared import (
    _apply_node_descriptor_attributes,
    _build_node_descriptor_meta,
    bootstrap_package_root,
)

ROOT_DIR = bootstrap_package_root(__file__)


class _FileNodeMeta(type(GNode), ABCMeta):
    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        # pybind11's native allocator bypasses the usual ABC instantiation check.
        if cls.__abstractmethods__:
            methods = ", ".join(sorted(cls.__abstractmethods__))
            raise TypeError(f"Can't instantiate abstract class {cls.__name__} with abstract methods {methods}")
        return super().__call__(*args, **kwargs)


class WorkflowFileNode(GNode, metaclass=_FileNodeMeta):
    """Abstract bytes-input node; subclasses own waiting, decoding, and storage."""

    INPUT_REQUIRED = True
    STEP_ID = ""
    TITLE = ""
    PROMPT = ""
    DEPENDENCIES: list[str] = []
    DESCRIPTOR_PROMPT_FILE = str(ROOT_DIR / "nodes" / "file_node" / "descriptor_prompt.md")

    def __init__(self) -> None:
        super().__init__()
        self.setName(self.STEP_ID)
        _apply_node_descriptor_attributes(self, self.DESCRIPTOR_PROMPT_FILE)

    def get_workflow_param(self) -> WorkflowParam:
        """Return this node's run data after its pipeline attaches the parameter."""
        param = self.getGParam(WORKFLOW_PARAM_KEY) if self.hasGParam(WORKFLOW_PARAM_KEY) else None
        if not isinstance(param, WorkflowParam):
            raise RuntimeError("Workflow param is not attached to the pipeline")
        return param

    def run(self) -> CStatus:
        param = self.get_workflow_param()
        if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
            return CStatus()

        param.set_state(self.STEP_ID, "running")
        file_bytes = param.inputs.get(self.STEP_ID)
        if not isinstance(file_bytes, (bytes, bytearray)):
            return param.fail(self.STEP_ID, f"step {self.STEP_ID} failed: file input must be bytes or bytearray")

        try:
            output = self.process_input(
                bytes(file_bytes),
                {dep: param.outputs[dep] for dep in self.DEPENDENCIES},
                param.state,
            )
            if not isinstance(output, StepRunOutput):
                return param.fail(self.STEP_ID, f"step {self.STEP_ID} failed: process_input must return StepRunOutput")
        except Exception as exc:
            return param.fail(self.STEP_ID, f"step {self.STEP_ID} failed: {exc}")

        param.complete(self.STEP_ID, output, self.card_payload(output))
        return CStatus()

    def card_payload(self, output: StepRunOutput) -> dict[str, Any]:
        return output.card

    def get_derived_keys(self) -> list[str]:
        try:
            output = self.get_workflow_param().outputs.get(self.STEP_ID)
        except RuntimeError:
            return []
        if output is None or not isinstance(output.derived, dict):
            return []
        return list(output.derived)

    @node_main_utility
    @node_subclass_implementation
    @abstractmethod
    def process_input(
        self,
        file_bytes: bytes,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        """Consume file bytes and return the subclass's card and derived data."""
        raise NotImplementedError()

    @classmethod
    def meta_node_kind(cls) -> str:
        return "WorkflowFileNode"

    @classmethod
    def step_meta(cls) -> dict[str, Any]:
        return {
            "id": cls.STEP_ID,
            "title": cls.TITLE,
            "prompt": cls.PROMPT,
            "dependencies": list(cls.DEPENDENCIES),
            "inputRequired": cls.INPUT_REQUIRED,
            "metaNodeKind": cls.meta_node_kind(),
            **_build_node_descriptor_meta(cls, cls.DESCRIPTOR_PROMPT_FILE),
        }