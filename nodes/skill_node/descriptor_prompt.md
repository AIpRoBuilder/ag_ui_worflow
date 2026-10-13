# Workflow Skill Node Descriptor

## Structure
- Subclass `WorkflowSkillNode` for nodes backed by an external `skill.md` document.
- Define `STEP_ID`, `TITLE`, `PROMPT`, `DEPENDENCIES`, and either `SKILL_DIR` or `SKILL_MD_PATH`.
- If the subclass needs input, define `wait_function(self) -> bool` inside the subclass and register the bound method via `self.setWaitFunc(self.wait_function)` after `super().__init__()`. Pass the method without calling it. The base node installs no wait callback; `INPUT_REQUIRED` is metadata only.
- Implement `process_operation(...)` to call the skill after installation completes.
- Use the parsed skill attributes `skill_description`, `skill_install_commands`, `skill_using`, and `skill_examples` to drive the implementation.

## Function
- Load a `skill.md` file and parse its major sections on initialization.
- Run installation commands from the skill document before the node executes.
- Execute skill-backed logic with optional user input, dependency outputs, and the shared `session_state` dict.
- Return a `StepRunOutput` for downstream workflow consumption.

## Implementation Guide
- For JSON pipelines, map the node's JSON `name` to its class in `WorkflowPipelinePool(node_classes=..., pipeline_json=...)`. Match `name` to `STEP_ID` and ordinary output-producing `depends` to `DEPENDENCIES`. Native condition edges may differ: `DEPENDENCIES` must require only outputs that actually exist, not an unreported condition output or all alternative branch outputs. Each class or zero-argument factory must create a fresh node. Global node registration and registry `clone()` hooks are not supported; the explicit class map is required.
- Point `SKILL_DIR` at the directory containing `skill.md` unless you need a custom filename via `SKILL_MD_PATH`.
- Keep installation commands in `skill.md` idempotent because initialization may happen more than once across processes.
- Write `process_operation` so it can use either the two-argument or three-argument signature supported by the base class.
- Surface actionable failures by raising clear exceptions when the skill cannot be executed or required parsed sections are empty.
- Reference `self.skill_using` and `self.skill_examples` inside subclass logic when generating prompts or instructions.
- For interactive skills, set `INPUT_REQUIRED = True` and explicitly register the subclass's wait method. Its bound callback takes no arguments, returns `False` while paused and `True` when input is ready, and must not block.
- The base class provides `self.get_workflow_param()`. Call it inside `wait_function`; use `param.condition` for input/state access and skip waiting when `param.can_run(...)` is false.
- Report `"awaiting_input"` / `"running"` via `param.set_state` if `WorkflowEngine` should return an input request; unreported gates keep the call open until completion.
- Reset event-based gates per run or store them in `param.state`, since pooled nodes are reused.

## Example
```python
from ag_ui_workflow import StepRunOutput, WorkflowSkillNode


class SkillSummarizerNode(WorkflowSkillNode):
    STEP_ID = "skill_summarizer"
    TITLE = "Skill Summarizer"
    SKILL_DIR = "skills/summarizer"
    INPUT_REQUIRED = True

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

    def process_operation(self, user_input, dependency_results, session_state):
        response = f"{self.skill_description}\n\nInput:\n{user_input}".strip()
        return StepRunOutput(
            card={"title": self.TITLE, "response": response},
            derived={"skillResponse": response},
        )
```