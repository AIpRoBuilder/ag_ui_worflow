from __future__ import annotations

from typing import Any

from pydaograph import CStatus, GNode

from ...session import get_node_workflow_session
from ...tools import node_main_utility, node_subclass_implementation
from ...workflow_types import StepRunOutput
from .._shared import (
    _apply_node_descriptor_attributes,
    _build_node_descriptor_meta,
    _get_step_output_derived_keys,
    _normalize_step_input,
)


class WorkflowStepNode(GNode):
    STEP_ID = ""
    TITLE = ""
    PROMPT = ""
    DEPENDENCIES: list[str] = []
    INPUT_REQUIRED = True
    NODE_KIND = "input"
    DESCRIPTOR_PROMPT_FILE = "descriptor_prompt.md"

    def __init__(self) -> None:
        super().__init__()
        self.setName(self.STEP_ID)
        self.setWaitForInput(False)
        self.setInputPrompt(self.PROMPT)
        self.setInputHandler(self._input_handler)
        _apply_node_descriptor_attributes(self, self.DESCRIPTOR_PROMPT_FILE)

    def _input_handler(self, user_input: str) -> CStatus:
        session = get_node_workflow_session(self)
        session.pending_inputs[self.STEP_ID] = user_input
        return CStatus()

    def run(self) -> CStatus:
        session = get_node_workflow_session(self)
        self._set_state("running")
        raw_input = _normalize_step_input(session.pending_inputs.get(self.STEP_ID, ""))
        if not raw_input:
            self._set_state("awaiting_input")
            return CStatus(1003, f"input required for step {self.STEP_ID}")

        dependency_results = {
            dep: session.step_outputs[dep]
            for dep in self.DEPENDENCIES
            if dep in session.step_outputs
        }
        try:
            output = self.process_input(
                raw_input,
                dependency_results,
                session.state,
            )
            if not isinstance(output, StepRunOutput):
                self._set_state("failed")
                return CStatus(1001, f"step {self.STEP_ID} failed: process_input must return StepRunOutput")
        except Exception as exc:  # pragma: no cover
            self._set_state("failed")
            return CStatus(1001, f"step {self.STEP_ID} failed: {exc}")

        session.step_outputs[self.STEP_ID] = output
        session.step_cards[self.STEP_ID] = self.card_payload(output)
        self._set_state("completed")
        callback = session.submit_callbacks.pop(self.STEP_ID, None)
        if callback is not None:
            callback(output)
        session.pending_inputs.pop(self.STEP_ID, None)
        return CStatus()

    def _set_state(self, state: str) -> None:
        session = get_node_workflow_session(self)
        session.step_states[self.STEP_ID] = state

    def card_payload(self, output: StepRunOutput) -> dict[str, Any]:
        return output.card

    def get_derived_keys(self) -> list[str]:
        return _get_step_output_derived_keys(self, self.STEP_ID)

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
            "nodeKind": cls.NODE_KIND,
            "metaNodeKind": cls.meta_node_kind(),
            **_build_node_descriptor_meta(cls, cls.DESCRIPTOR_PROMPT_FILE),
        }