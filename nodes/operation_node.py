from __future__ import annotations

from typing import Any

from pydaograph import CStatus, GNode

from ..session import get_node_workflow_session
from ..types import StepRunOutput
from ._shared import _get_step_output_derived_keys, _resolve_service_usages_for_step


class WorkflowOperationNode(GNode):
    INPUT_REQUIRED = False
    STEP_ID = ""
    TITLE = ""
    PROMPT = ""
    DEPENDENCIES: list[str] = []
    SERVICES: list[dict[str, str]] = []
    NODE_KIND = "operation"

    def __init__(self) -> None:
        super().__init__()
        self.setName(self.STEP_ID)
        self.setWaitForInput(False)

    def run(self) -> CStatus:
        session = get_node_workflow_session(self)
        self._set_state("running")
        dependency_results = {
            dep: session.step_outputs[dep]
            for dep in self.DEPENDENCIES
            if dep in session.step_outputs
        }
        try:
            self.use_service(session.state)
            output = self.process_operation(
                dependency_results,
                session.state,
            )
            if not isinstance(output, StepRunOutput):
                self._set_state("failed")
                return CStatus(1001, f"step {self.STEP_ID} failed: process_operation must return StepRunOutput")
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

    def use_service(self, session_state: dict[str, Any]) -> list[dict[str, Any]]:
        meta_map = session_state.get("_workflow_step_meta_map")
        if not isinstance(meta_map, dict):
            meta_map = {}
            session_state["_workflow_step_meta_map"] = meta_map
        if self.STEP_ID not in meta_map:
            meta_map[self.STEP_ID] = self.step_meta()

        usages = _resolve_service_usages_for_step(self.STEP_ID, session_state)
        if usages:
            service_usage_map = session_state.setdefault("service_usage", {})
            if isinstance(service_usage_map, dict):
                service_usage_map[self.STEP_ID] = usages
        return usages

    def clone(self):
        return self

    @classmethod
    def step_meta(cls) -> dict[str, Any]:
        return {
            "id": cls.STEP_ID,
            "title": cls.TITLE,
            "prompt": cls.PROMPT,
            "dependencies": list(cls.DEPENDENCIES),
            "services": list(cls.SERVICES),
            "inputRequired": cls.INPUT_REQUIRED,
            "nodeKind": cls.NODE_KIND,
        }

    def process_operation(
        self,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        raise NotImplementedError()