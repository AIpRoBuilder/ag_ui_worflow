from __future__ import annotations

from typing import Any

from pydaograph import GCondition, CStatus

from .session import get_bound_workflow_session
from .types import StepRunOutput


def _safe_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


class WorkflowConditionNode(GCondition):
    """A workflow condition node that routes pipeline execution to one of N branches.

    Subclasses declare:
    - ``STEP_ID``       – unique node identifier matching the graph JSON
    - ``TITLE``         – human-readable label
    - ``PROMPT``        – prompt shown to the user when ``INPUT_REQUIRED`` is True
    - ``DEPENDENCIES``  – upstream step IDs whose outputs are passed to :meth:`evaluate`
    - ``BRANCHES``      – ordered list of branch names (corresponding to the elements
                          added via :meth:`pydaograph.GCondition.addGElements`)
    - ``INPUT_REQUIRED``– set to ``True`` when user input must be provided before
                          :meth:`evaluate` is called (default ``False``)

    The only method subclasses *must* override is :meth:`evaluate`, which should
    return either an integer branch index (0-based into ``BRANCHES``) or a branch
    name string.

    When ``INPUT_REQUIRED`` is ``True``, the node waits for a pending input value
    (keyed by ``STEP_ID`` in ``session.pending_inputs``).  The resolved string is
    passed to :meth:`evaluate` as the ``user_input`` argument.

    Example (no input)::

        class RouteByQuality(WorkflowConditionNode):
            STEP_ID = "RouteByQuality"
            TITLE = "Route by quality score"
            DEPENDENCIES = ["ScoreStep"]
            BRANCHES = ["HighQualityPath", "LowQualityPath"]

            def evaluate(self, dependency_results, session_state, user_input=""):
                score = dependency_results["ScoreStep"].derived.get("score", 0)
                return 0 if score >= 0.8 else 1

    Example (with user input)::

        class UserChoiceRoute(WorkflowConditionNode):
            STEP_ID = "UserChoiceRoute"
            TITLE = "Route by user choice"
            PROMPT = "Enter 'a' for path A or 'b' for path B"
            INPUT_REQUIRED = True
            BRANCHES = ["PathA", "PathB"]

            def evaluate(self, dependency_results, session_state, user_input=""):
                return 0 if user_input.strip().lower() == "a" else 1
    """

    STEP_ID: str = ""
    TITLE: str = ""
    PROMPT: str = ""
    DEPENDENCIES: list[str] = []
    # Ordered list of branch node IDs / names. Index i corresponds to branch i
    # in the GCondition element list built by the graph pipeline.
    BRANCHES: list[str] = []
    INPUT_REQUIRED: bool = False
    NODE_KIND: str = "condition"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        super().__init__()
        self.setName(self.STEP_ID)
        self.setWaitForInput(False)
        self.setInputPrompt(self.PROMPT)
        self.setInputHandler(self._input_handler)

    def _input_handler(self, user_input: str) -> CStatus:
        try:
            session = get_bound_workflow_session()
        except RuntimeError:
            return CStatus()
        session.pending_inputs[self.STEP_ID] = user_input
        return CStatus()

    # ------------------------------------------------------------------
    # GCondition interface
    # ------------------------------------------------------------------

    def choose(self) -> int:
        """Select which branch index to follow.

        Invoked by the pydaograph pipeline.  Delegates to :meth:`evaluate` and
        converts a branch name string to the corresponding integer index when
        necessary.  Falls back to branch 0 on any error to avoid blocking the
        pipeline.

        When ``INPUT_REQUIRED`` is ``True`` and no pending input is present the
        node records an ``"awaiting_input"`` state and returns ``0`` so the
        pipeline can continue; the engine is responsible for re-running the step
        once input arrives (same contract as other input-capable node types).
        """
        try:
            session = get_bound_workflow_session()
        except RuntimeError:
            return 0

        # Resolve optional user input.
        raw_input = _safe_string(session.pending_inputs.get(self.STEP_ID, ""))
        if self.INPUT_REQUIRED and not raw_input:
            self._set_state("awaiting_input")
            return 0

        self._set_state("evaluating")
        dependency_results: dict[str, StepRunOutput] = {
            dep: session.step_outputs[dep]
            for dep in self.DEPENDENCIES
            if dep in session.step_outputs
        }

        try:
            branch = self.evaluate(dependency_results, session.state, raw_input)
        except Exception as exc:  # pragma: no cover
            self._set_state("failed")
            session.step_states[self.STEP_ID + "_error"] = str(exc)
            return 0

        # Resolve a string branch name to its index.
        if isinstance(branch, str):
            try:
                branch = self.BRANCHES.index(branch)
            except ValueError:
                branch = 0

        chosen = max(0, int(branch))
        # Record which branch was taken in the shared session state so downstream
        # nodes and the engine streaming layer can inspect it.
        session.step_states[self.STEP_ID] = f"branch:{chosen}"
        session.step_states[self.STEP_ID + "_branch"] = chosen
        # Consume the input so re-runs start fresh.
        session.pending_inputs.pop(self.STEP_ID, None)
        return chosen

    # ------------------------------------------------------------------
    # Subclass hook
    # ------------------------------------------------------------------

    def evaluate(
        self,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
        user_input: str = "",
    ) -> int | str:
        """Return the branch to follow.

        Args:
            dependency_results: Outputs from upstream steps keyed by step ID.
            session_state: Mutable shared workflow session state dict.
            user_input: Normalised string input provided by the user for this
                step, or an empty string when ``INPUT_REQUIRED`` is ``False``.

        Returns:
            An integer branch index (0-based into ``BRANCHES``) **or** a
            branch name string present in ``BRANCHES``.  Values outside the
            valid range are clamped to 0.

        Subclasses must override this method.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__}.evaluate() must be implemented by the user.\n"
            f"Return an integer index or a branch name from BRANCHES={self.BRANCHES!r}."
        )

    # ------------------------------------------------------------------
    # Helpers shared with other node types
    # ------------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        try:
            session = get_bound_workflow_session()
        except RuntimeError:
            return
        session.step_states[self.STEP_ID] = state

    def clone(self):  # required by pydaograph
        return self

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @classmethod
    def step_meta(cls) -> dict[str, Any]:
        """Return metadata dict compatible with WorkflowEngine's steps_meta list."""
        return {
            "id": cls.STEP_ID,
            "title": cls.TITLE,
            "prompt": cls.PROMPT,
            "dependencies": list(cls.DEPENDENCIES),
            "branches": list(cls.BRANCHES),
            "inputRequired": cls.INPUT_REQUIRED,
            "nodeKind": cls.NODE_KIND,
        }
