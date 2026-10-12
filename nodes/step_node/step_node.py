from __future__ import annotations

from typing import Any

from pydaograph import CStatus, GNode

from ...params import WORKFLOW_PARAM_KEY, WorkflowParam
from ...tools import node_main_utility, node_subclass_implementation
from ...workflow_types import StepRunOutput
from .._shared import (
    _apply_node_descriptor_attributes,
    _build_node_descriptor_meta,
    _normalize_step_input,
    bootstrap_package_root,
)
ROOT_DIR = bootstrap_package_root(__file__)


class WorkflowStepNode(GNode):
    STEP_ID = ""
    TITLE = ""
    PROMPT = ""
    DEPENDENCIES: list[str] = []
    INPUT_REQUIRED = True
    DESCRIPTOR_PROMPT_FILE = str(ROOT_DIR / "nodes" / "step_node" / "descriptor_prompt.md")

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

        self._set_state("running")
        raw_input = _normalize_step_input(param.inputs.get(self.STEP_ID, ""))

        dependency_results = {
            dep: param.outputs[dep]
            for dep in self.DEPENDENCIES
            if dep in param.outputs
        }
        try:
            output = self.process_input(
                raw_input,
                dependency_results,
                param.state,
            )
            if not isinstance(output, StepRunOutput):
                return param.fail(self.STEP_ID, f"step {self.STEP_ID} failed: process_input must return StepRunOutput")
        except Exception as exc:  # pragma: no cover
            return param.fail(self.STEP_ID, f"step {self.STEP_ID} failed: {exc}")

        param.complete(self.STEP_ID, output, self.card_payload(output))
        return CStatus()

    def _set_state(self, state: str) -> None:
        self.get_workflow_param().set_state(self.STEP_ID, state)

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
    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        raise NotImplementedError()

    def clone(self):
        return self

    @classmethod
    def meta_node_kind(cls) -> str:
        return "WorkflowStepNode"

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