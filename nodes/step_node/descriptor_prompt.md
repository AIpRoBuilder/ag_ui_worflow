# Workflow Step Node Descriptor

## Structure
- Subclass `WorkflowStepNode` for steps that require direct user input before producing a `StepRunOutput`.
- Define `STEP_ID`, `TITLE`, `PROMPT`, and any `DEPENDENCIES` or `SERVICES` at class level.
- Implement `process_input(user_input, dependency_results, session_state)` as the node's main utility.
- Return a `StepRunOutput` whose `card` drives UI presentation and whose `derived` values feed later nodes.

## Function
- Collect normalized user input from the workflow session.
- Optionally consume upstream `dependency_results` keyed by step id.
- Persist the completed `StepRunOutput` into the workflow session state.

## Implementation Guide
- Keep `STEP_ID` stable because it is used for session state keys and workflow metadata.
- Treat `user_input` as already normalized text or a resolved file path from `_normalize_step_input`.
- Validate required dependency data inside `process_input` and raise a clear error if prerequisites are missing.
- Build concise `card` fields for UI display and richer `derived` fields for machine-readable outputs.
- Use `session_state` for shared mutable workflow context instead of global module state.

## Example
```python
from ag_ui_workflow import StepRunOutput, WorkflowStepNode


class SummarizeRequestNode(WorkflowStepNode):
    STEP_ID = "summarize_request"
    TITLE = "Summarize Request"
    PROMPT = "Describe what you want the workflow to do."

    def process_input(self, user_input, dependency_results, session_state):
        summary = user_input.strip()
        session_state["request_summary"] = summary
        return StepRunOutput(
            card={"title": self.TITLE, "summary": summary},
            derived={"requestSummary": summary},
        )
```