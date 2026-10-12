# Workflow File Node Descriptor

## Structure
- Subclass `WorkflowFileNode` for steps that accept uploaded or referenced files and persist them to storage.
- Define `STEP_ID`, `TITLE`, `PROMPT`, and optional storage-related environment conventions on the subclass when needed.
- The base node does not wait automatically; `INPUT_REQUIRED` is UI metadata only. A waiting subclass must define `wait_function(self) -> bool` inside the subclass and register it with `self.setWaitFunc(self.wait_function)` after `super().__init__()`. Pass the method, not its result.
- Override `save_files(files, session_state, storage_override)` only when the default storage flow is insufficient.
- Override `build_step_output(saved_files)` when the downstream workflow needs a custom card or derived schema.

## Function
- Parse input payloads that may contain file paths, base64 content, or serialized byte strings.
- Save normalized file payloads to local or remote storage.
- Return storage locations and summary metadata in a `StepRunOutput`.
- Expose saved file details to downstream nodes through `derived` output.

## Implementation Guide
- Expect the incoming `files` list to contain dictionaries with `fileName` and `bytes` after parsing.
- Use `session_state` to read runtime storage preferences such as remote uploader callbacks or storage directories.
- Preserve the default behavior when possible so uploads work for both local and remote backends.
- Customize `build_step_output` instead of mutating the workflow param directly when only the result schema needs to change.
- If the node accepts optional files, set `INPUT_REQUIRED = False` on the subclass.
- The bound callback takes no arguments; return `False` while waiting and `True` when valid files are available. Input acquisition and validation belong to this subclass method, not the base node.
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
            files, _ = self._parse_file_input(param.inputs.get(self.STEP_ID))
            ready = bool(files)
            param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
            return ready

    def build_step_output(self, saved_files):
        first_path = saved_files[0]["path"] if saved_files else ""
        return StepRunOutput(
            card={"title": self.TITLE, "files": saved_files},
            derived={"referenceImagePath": first_path, "savedFiles": saved_files},
        )
```