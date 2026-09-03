# Workflow Skill Node Descriptor

## Structure
- Subclass `WorkflowSkillNode` for nodes backed by an external `skill.md` document.
- Define `STEP_ID`, `TITLE`, `PROMPT`, `DEPENDENCIES`, and either `SKILL_DIR` or `SKILL_MD_PATH`.
- Implement `process_operation(...)` to call the skill after installation completes.
- Use the parsed skill attributes `skill_description`, `skill_install_commands`, `skill_using`, and `skill_examples` to drive the implementation.

## Function
- Load a `skill.md` file and parse its major sections on initialization.
- Run installation commands from the skill document before the node executes.
- Execute skill-backed logic with optional user input, dependency outputs, and session state.
- Return a `StepRunOutput` for downstream workflow consumption.

## Implementation Guide
- Point `SKILL_DIR` at the directory containing `skill.md` unless you need a custom filename via `SKILL_MD_PATH`.
- Keep installation commands in `skill.md` idempotent because initialization may happen more than once across processes.
- Write `process_operation` so it can use either the two-argument or three-argument signature supported by the base class.
- Surface actionable failures by raising clear exceptions when the skill cannot be executed or required parsed sections are empty.
- Reference `self.skill_using` and `self.skill_examples` inside subclass logic when generating prompts or instructions.

## Example
```python
from ag_ui_workflow import StepRunOutput, WorkflowSkillNode


class SkillSummarizerNode(WorkflowSkillNode):
    STEP_ID = "skill_summarizer"
    TITLE = "Skill Summarizer"
    SKILL_DIR = "skills/summarizer"

    def process_operation(self, user_input, dependency_results, session_state):
        response = f"{self.skill_description}\n\nInput:\n{user_input}".strip()
        return StepRunOutput(
            card={"title": self.TITLE, "response": response},
            derived={"skillResponse": response},
        )
```