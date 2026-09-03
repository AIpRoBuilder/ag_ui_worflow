# Workflow Operation Node Descriptor

## Structure
- Subclass `WorkflowOperationNode` for steps that run automatically from dependency outputs and shared session state.
- Define `STEP_ID`, `TITLE`, optional `PROMPT`, `DEPENDENCIES`.
- Implement `process_operation(dependency_results, session_state)` as the node's main utility.
- Return a `StepRunOutput` describing the operation result.

## Function
- Execute pure workflow logic after upstream dependencies are available.
- Consume structured results from prior steps without waiting for new user input.
- Publish a card payload and derived data for downstream orchestration.

## Implementation Guide
- Keep `process_operation` deterministic with respect to `dependency_results` and `session_state` whenever possible.
- Validate that required dependency keys exist before reading nested fields from upstream outputs.
- Prefer storing reusable machine-facing values in `derived` and lightweight presentation values in `card`.
- Set `INPUT_REQUIRED = True` only if a specialized subclass changes the input behavior intentionally.

## Example
```python
from ag_ui_workflow import StepRunOutput, WorkflowOperationNode


class MergeDraftNode(WorkflowOperationNode):
    STEP_ID = "merge_draft"
    TITLE = "Merge Draft"
    DEPENDENCIES = ["collect_notes", "collect_constraints"]

    def process_operation(self, dependency_results, session_state):
        notes = dependency_results["collect_notes"].derived.get("notes", "")
        constraints = dependency_results["collect_constraints"].derived.get("constraints", "")
        merged = f"{notes}\n\nConstraints:\n{constraints}".strip()
        return StepRunOutput(
            card={"title": self.TITLE, "summary": merged},
            derived={"mergedDraft": merged},
        )
```