# Workflow File Node Descriptor

## Structure
- `WorkflowFileNode` is abstract. Implement `process_input(file_bytes, dependency_results, session_state)` to receive a file as immutable `bytes` and return a `StepRunOutput`.
- Define `STEP_ID`, `TITLE`, `PROMPT`, and `DEPENDENCIES` on the subclass. Neither the base nor a subclass without `process_input` can be instantiated.
- The base node does not wait automatically; `INPUT_REQUIRED` is UI metadata only. A waiting subclass must define `wait_function(self) -> bool` inside the subclass and register it with `self.setWaitFunc(self.wait_function)` after `super().__init__()`. Pass the method, not its result.

## Function
- Read `param.inputs[STEP_ID]` as `bytes` or `bytearray`, converting the latter to `bytes` before invoking the abstract processing hook.
- Supply completed dependency outputs and the shared per-run state to the hook.
- Record the subclass's `StepRunOutput`, card, and completion state; report input/type or processing errors through the workflow param.
- Leave acquisition, decoding, content validation, filenames, and persistence entirely to the caller or subclass. The base performs no file-system or network I/O.

## Implementation Guide
- For JSON pipelines, map the node's JSON `name` to its class in `WorkflowPipelinePool(node_classes=..., pipeline_json=...)`. Match `name` to `STEP_ID` and ordinary output-producing `depends` to `DEPENDENCIES`. Native condition edges may differ: `DEPENDENCIES` must require only outputs that actually exist, not an unreported condition output or all alternative branch outputs. Each class or zero-argument factory must create a fresh node. Global node registration and registry `clone()` hooks are not supported; the explicit class map is required.
- Post bytes directly with `engine.run(request_id, {STEP_ID: file_bytes})` or `engine.answer(request_id, file_bytes)` when the run reports a pause.
- If transport input is a path, base64 string, JSON upload object, or file list, convert it to one bytes payload in the caller or custom wait function, then assign it to `param.inputs[STEP_ID]` before releasing the gate.
- An empty `b""` payload is valid; reject it in the callback or processing hook if the application requires a nonempty file. Missing/nonbinary input fails immediately when no callback waits or converts it.
- Keep binary data needed by downstream nodes in `session_state`; keep `card` and `derived` JSON-serializable for AG-UI events. Implement any local/remote persistence explicitly in `process_input`.
- Migrate previous `save_files` / `build_step_output` overrides into `process_input`; these hooks and built-in storage options have been removed.
- The bound callback takes no arguments; return `False` while waiting and `True` when the chosen input is ready. Input acquisition and validation belong to this subclass method, not the base node.
- The base class provides `self.get_workflow_param()`. Call it inside `wait_function` and protect input/state access with `param.condition`. Skip waiting when `param.can_run(...)` is false.
- Report `"awaiting_input"` / `"running"` through `param.set_state` if `WorkflowEngine` should return while paused. Without pause reporting, the engine call stays open until native completion.
- Reset event-based gates per run or keep them in `param.state`, because pool leases reuse nodes.

## Example
```python
from ag_ui_workflow import StepRunOutput, WorkflowFileNode


class ReferenceImageNode(WorkflowFileNode):
    STEP_ID = "reference_image"
    TITLE = "Reference Image"
    PROMPT = "Upload an image to use as reference."

    def __init__(self):
        super().__init__()
        self.setWaitFunc(self.wait_function)

    def wait_function(self) -> bool:
        param = self.get_workflow_param()
        with param.condition:
            if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
                return True
            raw_input = param.inputs.get(self.STEP_ID)
            ready = isinstance(raw_input, (bytes, bytearray))
            param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
            return ready

    def process_input(self, file_bytes, dependency_results, session_state):
        session_state["reference_image_bytes"] = file_bytes
        return StepRunOutput(
            card={"title": self.TITLE, "summary": f"Received {len(file_bytes)} bytes."},
            derived={"referenceImageByteSize": len(file_bytes)},
        )
```