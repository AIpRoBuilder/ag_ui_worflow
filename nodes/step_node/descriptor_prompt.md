# Workflow Step Node Descriptor

## Structure
- Subclass `WorkflowStepNode` for interactive steps and automatic operations that produce a `StepRunOutput`.
- Define `STEP_ID`, `TITLE`, `PROMPT`, and any `DEPENDENCIES` at class level.
- Set `INPUT_REQUIRED = False` for an automatic operation that does not need direct user input.
- `INPUT_REQUIRED` is UI metadata only. The base node does not install wait callbacks.
- If a subclass needs to wait, define `wait_function(self) -> bool` inside that subclass and register the bound method with `self.setWaitFunc(self.wait_function)` after `super().__init__()`. Do not call the method when registering it.
- Implement `process_input(user_input, dependency_results, session_state)` as the node's main utility.
- Return a `StepRunOutput` whose `card` drives UI presentation and whose `derived` values feed later nodes.

## Function
- Collect normalized user input from the request's workflow param when required.
- Optionally consume upstream `dependency_results` keyed by step id.
- Persist the completed `StepRunOutput` into the request's workflow param so dependent steps can read it.

## Implementation Guide
- Keep `STEP_ID` stable because it is the key for step inputs, outputs, and workflow metadata.
- For JSON pipelines, map the node's JSON `name` to its class in `WorkflowPipelinePool(node_classes=..., pipeline_json=...)`. Match `name` to `STEP_ID` and ordinary output-producing `depends` to `DEPENDENCIES`. Native condition edges may differ: `DEPENDENCIES` must require only outputs that actually exist, not an unreported condition output or all alternative branch outputs. Each class or zero-argument factory must create a fresh node. Global node registration and registry `clone()` hooks are not supported; the explicit class map is required.
- Treat `user_input` as normalized text, a resolved file path, or an empty string for automatic operations.
- Validate required dependency data inside `process_input` and raise a clear error if prerequisites are missing.
- Build concise `card` fields for UI display and richer `derived` fields for machine-readable outputs.
- Use `session_state` for shared mutable workflow context instead of global module state.
- The native wait callback takes no arguments and returns `False` to stay paused or `True` to proceed. Keep it non-blocking; validation and input acquisition belong in this user-defined function.
- The base class provides `self.get_workflow_param()`. Call it inside `wait_function`, not during construction. Return `True` for nodes that cannot run because of missing dependency outputs, so failed branches do not hang.
- For `WorkflowEngine` to return an input request, use `param.set_state(step_id, "awaiting_input")` while waiting and `"running"` when released. Protect input reads/updates with `param.condition`. A callback that does not report a pause keeps the engine call open until completion.
- For event-based gates, reset events per run or store them in `param.state`; pooled nodes are reused across request IDs.

## Example
```python
from ag_ui_workflow import StepRunOutput, WorkflowStepNode


class SummarizeRequestNode(WorkflowStepNode):
    STEP_ID = "summarize_request"
    TITLE = "Summarize Request"
    PROMPT = "Describe what you want the workflow to do."

    def __init__(self):
        super().__init__()
        self.setWaitFunc(self.wait_function)

    def wait_function(self) -> bool:
        param = self.get_workflow_param()
        with param.condition:
            if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
                return True
            text = param.inputs.get(self.STEP_ID)
            ready = isinstance(text, str) and bool(text.strip())
            param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
            return ready

    def process_input(self, user_input, dependency_results, session_state):
        summary = user_input.strip()
        session_state["request_summary"] = summary
        return StepRunOutput(
            card={"title": self.TITLE, "summary": summary},
            derived={"requestSummary": summary},
        )
```

Automatic operations use the same hook:

```python
class MergeDraftNode(WorkflowStepNode):
    STEP_ID = "merge_draft"
    TITLE = "Merge Draft"
    DEPENDENCIES = ["collect_notes", "collect_constraints"]
    INPUT_REQUIRED = False

    def process_input(self, user_input, dependency_results, session_state):
        notes = dependency_results["collect_notes"].derived.get("notes", "")
        constraints = dependency_results["collect_constraints"].derived.get("constraints", "")
        merged = f"{notes}\n\nConstraints:\n{constraints}".strip()
        return StepRunOutput(
            card={"title": self.TITLE, "summary": merged},
            derived={"mergedDraft": merged},
        )
```