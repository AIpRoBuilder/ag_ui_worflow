from __future__ import annotations

from pathlib import Path
from typing import Any

from pydaograph import CStatus, GNode

from ..services import workflow_service_registry
from ..session import get_node_workflow_session
from ..types import StepRunOutput
from ._shared import _get_step_output_derived_keys


class WorkflowServiceNode(GNode):
    DEFAULT_WORKDIR = str(Path.cwd())
    INPUT_REQUIRED = False
    STEP_ID = ""
    TITLE = ""
    PROMPT = ""
    DEPENDENCIES: list[str] = []
    NODE_KIND = "service"

    SANDBOX_DOMAIN_ENV = "SANDBOX_DOMAIN"
    SANDBOX_IMAGE_ENV = "SANDBOX_IMAGE"

    DEFAULT_SANDBOX_DOMAIN = "localhost:8120"
    DEFAULT_SANDBOX_IMAGE = "ubuntu:22.04"

    DEFAULT_SANDBOX_TIMEOUT_SECONDS = 600
    DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
    DEFAULT_KILL_ON_EXIT = True

    def __init__(self) -> None:
        super().__init__()
        self.setName(self.STEP_ID)
        self.setWaitForInput(False)
        self._installed: bool = False
        self._service_running: bool = False
        self._pid: int | None = None

    def run(self) -> CStatus:
        session = get_node_workflow_session(self)
        self._set_state("running")
        dependency_results = {
            dep: session.step_outputs[dep]
            for dep in self.DEPENDENCIES
            if dep in session.step_outputs
        }
        try:
            output = self.process_operation(
                dependency_results,
                session.state,
            )
            if not isinstance(output, StepRunOutput):
                self._set_state("failed")
                return CStatus(1001, f"step {self.STEP_ID} failed: process_operation must return StepRunOutput")
        except Exception as exc:  # pragma: no cover
            self._set_state("failed")
            workflow_service_registry.mark_failed(self.STEP_ID, str(exc))
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

    def clone(self):
        return self

    @classmethod
    def step_meta(cls) -> dict[str, Any]:
        return {
            "id": cls.STEP_ID,
            "title": cls.TITLE,
            "prompt": cls.PROMPT,
            "dependencies": list(cls.DEPENDENCIES),
            "inputRequired": cls.INPUT_REQUIRED,
            "nodeKind": cls.NODE_KIND,
        }

    def install_environment(
        self,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> bool:
        raise NotImplementedError(
            f"{self.__class__.__name__}.install_environment() must be implemented.\n"
            "Install packages, write configs, or pull container images here."
        )

    def start_service(
        self,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> int:
        raise NotImplementedError(
            f"{self.__class__.__name__}.start_service() must be implemented.\n"
            "Launch the background service and return its PID here."
        )

    def process_operation(
        self,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        if not self._installed:
            installed = self.install_environment(dependency_results, session_state)
            if not isinstance(installed, bool):
                raise TypeError(
                    f"install_environment must return bool, got {type(installed).__name__}"
                )
            if not installed:
                raise RuntimeError(
                    f"{self.__class__.__name__}.install_environment() returned False - installation failed."
                )
            self._installed = True

        if not self._service_running or self._pid is None:
            pid = self.start_service(dependency_results, session_state)
            if not isinstance(pid, int):
                raise TypeError(
                    f"start_service must return int (PID), got {type(pid).__name__}"
                )
            if pid <= 0:
                raise RuntimeError(
                    f"{self.__class__.__name__}.start_service() returned {pid} - service failed to start."
                )
            self._pid = pid
            self._service_running = True

        workflow_service_registry.register_service(
            self.STEP_ID,
            node_class=self.__class__.__name__,
            metadata={
                "title": self.TITLE,
                "node_kind": self.NODE_KIND,
                "dependencies": list(self.DEPENDENCIES),
            },
        )
        workflow_service_registry.update_service_status(
            self.STEP_ID,
            status="running",
            is_running=True,
            pid=self._pid,
            installed=self._installed,
            last_error="",
        )

        return StepRunOutput(
            card={
                "service": self.STEP_ID,
                "status": "running",
                "pid": self._pid,
                "installed": self._installed,
            },
            derived={
                "service_name": self.STEP_ID,
                "service_running": True,
                "pid": self._pid,
                "installed": self._installed,
            },
        )