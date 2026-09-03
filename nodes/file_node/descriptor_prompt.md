# Workflow File Node Descriptor

## Structure
- Subclass `WorkflowFileNode` for steps that accept uploaded or referenced files and persist them to storage.
- Define `STEP_ID`, `TITLE`, `PROMPT`, and optional storage-related environment conventions on the subclass when needed.
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
- Customize `build_step_output` instead of mutating session structures directly when only the result schema needs to change.
- If the node accepts optional files, set `INPUT_REQUIRED = False` on the subclass.

## Example
```python
from ag_ui_workflow import StepRunOutput, WorkflowFileNode


class ReferenceImageNode(WorkflowFileNode):
    STEP_ID = "reference_image"
    TITLE = "Reference Image"
    PROMPT = "Upload an image to use as reference."

    def build_step_output(self, saved_files):
        first_path = saved_files[0]["path"] if saved_files else ""
        return StepRunOutput(
            card={"title": self.TITLE, "files": saved_files},
            derived={"referenceImagePath": first_path, "savedFiles": saved_files},
        )
```