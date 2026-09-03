from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from pydaograph import CStatus, GNode

from ..session import get_node_workflow_session
from ..tools import node_main_utility
from ..workflow_types import StepRunOutput
from ._shared import (
    _get_step_output_derived_keys,
    _normalize_step_input,
    _workflow_root_dir,
    extract_skill_commands,
    parse_skill_md,
)


class WorkflowSkillNode(GNode):
    """A workflow node backed by a skill defined in a skill.md file.

    Subclasses set ``SKILL_DIR`` to the directory containing ``skill.md`` (or
    ``SKILL_MD_PATH`` to point directly at the file). On instantiation the
    skill's ``## Installation`` block is executed so that the required packages
    are available. Subclasses must override :meth:`process_operation` to call the
    skill and return a :class:`StepRunOutput`.

    Parsed sections from the skill document are exposed as:
      - ``self.skill_description`` - text under ``## Description``
      - ``self.skill_install_commands`` - list of shell command strings
      - ``self.skill_using`` - text under ``## Using``
      - ``self.skill_examples`` - text under ``## Examples``
    """

    STEP_ID: str = ""
    TITLE: str = ""
    PROMPT: str = ""
    DEPENDENCIES: list[str] = []
    INPUT_REQUIRED: bool = False
    NODE_KIND: str = "skill"

    SKILL_DIR: str = ""
    SKILL_MD_PATH: str = ""

    INSTALL_TIMEOUT: int = 240

    def __init__(self) -> None:
        import threading

        super().__init__()
        self.setName(self.STEP_ID)
        self.setWaitForInput(False)
        self.setInputPrompt(self.PROMPT)
        self.setInputHandler(self._input_handler)

        skill_md_path = self._resolve_skill_md_path()
        skill_md_text = Path(skill_md_path).read_text(encoding="utf-8")
        sections = parse_skill_md(skill_md_text)

        self.skill_description: str = sections.get("Description", "")
        self.skill_install_commands: list[str] = extract_skill_commands(
            sections.get("Installation", "")
        )
        self.skill_using: str = sections.get("Using", "")
        self.skill_examples: str = sections.get("Examples", "")
        self._install_errors: list[str] = []

        self._install_thread: threading.Thread = threading.Thread(
            target=self._install_packages,
            daemon=True,
            name=f"skill-install-{self.STEP_ID}",
        )
        self._install_thread.start()

    def _input_handler(self, user_input: str) -> CStatus:
        session = get_node_workflow_session(self)
        session.pending_inputs[self.STEP_ID] = user_input
        return CStatus()

    def _resolve_skill_md_path(self) -> str:
        if self.SKILL_MD_PATH:
            path = Path(self.SKILL_MD_PATH)
            if not path.is_absolute():
                path = _workflow_root_dir() / path
            if not path.exists():
                raise FileNotFoundError(f"skill.md not found at {path}")
            return str(path)

        if self.SKILL_DIR:
            path = Path(self.SKILL_DIR)
            if not path.is_absolute():
                path = _workflow_root_dir() / path
            candidate = path / "skill.md"
            if not candidate.exists():
                raise FileNotFoundError(f"skill.md not found in {path}")
            return str(candidate)

        raise ValueError(
            f"{self.__class__.__name__} must set SKILL_DIR or SKILL_MD_PATH"
        )

    def _install_packages(self) -> None:
        import subprocess
        import warnings

        for cmd in self.skill_install_commands:
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    msg = (
                        f"WorkflowSkillNode installation command returned "
                        f"non-zero exit code {result.returncode}: {cmd!r}\n"
                        f"stderr: {result.stderr.strip()}"
                    )
                    self._install_errors.append(msg)
                    warnings.warn(msg, stacklevel=2)
            except Exception as exc:
                msg = f"WorkflowSkillNode installation command failed: {cmd!r}: {exc}"
                self._install_errors.append(msg)
                warnings.warn(msg, stacklevel=2)

    def _wait_for_installation(self) -> CStatus:
        self._install_thread.join(timeout=self.INSTALL_TIMEOUT)
        if self._install_thread.is_alive():
            return CStatus(
                1001,
                f"step {self.STEP_ID} failed: package installation timed out after {self.INSTALL_TIMEOUT}s",
            )
        if self._install_errors:
            return CStatus(
                1001,
                f"step {self.STEP_ID} failed: package installation errors: {'; '.join(self._install_errors)}",
            )
        return CStatus()

    def run(self) -> CStatus:
        session = get_node_workflow_session(self)
        self._set_state("running")
        raw_input = _normalize_step_input(session.pending_inputs.get(self.STEP_ID, ""))
        if self.INPUT_REQUIRED and not raw_input:
            self._set_state("awaiting_input")
            return CStatus(1003, f"input required for step {self.STEP_ID}")

        install_status = self._wait_for_installation()
        if install_status.isErr():
            self._set_state("failed")
            return install_status

        dependency_results = {
            dep: session.step_outputs[dep]
            for dep in self.DEPENDENCIES
            if dep in session.step_outputs
        }
        try:
            params = inspect.signature(self.process_operation).parameters.values()
            accepts_user_input = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params) or len(list(params)) >= 3
            if accepts_user_input:
                output = self.process_operation(
                    raw_input,
                    dependency_results,
                    session.state,
                )
            else:
                output = self.process_operation(
                    dependency_results,
                    session.state,
                )
            if not isinstance(output, StepRunOutput):
                self._set_state("failed")
                return CStatus(
                    1001,
                    f"step {self.STEP_ID} failed: process_operation must return StepRunOutput",
                )
        except Exception as exc:
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
    def process_operation(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        raise NotImplementedError(
            f"{self.__class__.__name__}.process_operation() must be implemented by the user.\n"
            f"Refer to self.skill_using and self.skill_examples for usage guidance.\n"
            f"skill_using:\n{self.skill_using}\n\nskill_examples:\n{self.skill_examples}"
        )

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