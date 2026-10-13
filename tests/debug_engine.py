"""Console example: a run with an input node that waits for you to type, then finishes.

    python3.10 tests/debug_engine.py
"""
import importlib.util
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The checkout folder is not named after the package and site-packages may hold a stale copy.
_spec = importlib.util.spec_from_file_location(
    "ag_ui_workflow", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
_package = importlib.util.module_from_spec(_spec)
sys.modules["ag_ui_workflow"] = _package
_spec.loader.exec_module(_package)

from ag_ui_workflow import (  # noqa: E402
    StepRunOutput,
    WorkflowEngine,
    WorkflowPipelinePool,
    WorkflowStepNode,
)


class AskNameNode(WorkflowStepNode):
    """Waits for the user to supply a name."""

    STEP_ID = "ask_name"
    TITLE = "Ask Name"
    PROMPT = "What is your name?"
    INPUT_REQUIRED = True

    def __init__(self) -> None:
        super().__init__()
        self.setWaitFunc(self.wait_function)

    def wait_function(self) -> bool:
        """False pauses native execution; True releases this node."""
        param = self.get_workflow_param()
        with param.condition:
            if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
                return True
            name = param.inputs.get(self.STEP_ID)
            ready = isinstance(name, str) and bool(name.strip())
            if ready:
                param.inputs[self.STEP_ID] = name.strip()
            param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
            return ready

    def process_input(self, user_input, dependency_results, session_state):
        return StepRunOutput(
            card={"title": self.TITLE, "summary": f"Name received: {user_input}"},
            derived={"name": user_input},
        )


class GreetNode(WorkflowStepNode):
    """Runs on its own as soon as the input node is done."""

    STEP_ID = "greet"
    TITLE = "Greet"
    DEPENDENCIES = ["ask_name"]
    INPUT_REQUIRED = False

    def process_input(self, user_input, dependency_results, session_state):
        greeting = f"Hello, {dependency_results['ask_name'].derived['name']}!"
        return StepRunOutput(
            card={"title": self.TITLE, "summary": greeting},
            derived={"greeting": greeting},
        )


NODES = (AskNameNode, GreetNode)
NODE_CLASSES = {node.STEP_ID: node for node in NODES}
PIPELINE_JSON = json.dumps({
    "nodes": [
        {"name": "ask_name"},
        {"name": "greet", "depends": ["ask_name"]},
    ],
})


def main() -> None:
    with WorkflowPipelinePool(pipeline_json=PIPELINE_JSON, node_classes=NODE_CLASSES) as pool:
        engine = WorkflowEngine(graph_config_id="demo", steps_meta=[n.step_meta() for n in NODES], pool=pool)
        request_id = str(uuid.uuid4())
        try:
            result = engine.run(request_id)
            while result.awaiting is not None:
                # Collect input outside the engine; the node callback receives it on the next poll.
                result = engine.answer(request_id, input(f"\n{result.awaiting.get('prompt', '')} "))
            for step_id in result.outputs:
                print(result.outputs[step_id].card["summary"])
            if result.error:
                print(result.error)
        except (EOFError, KeyboardInterrupt):
            print("\naborted")
        finally:
            engine.close()


if __name__ == "__main__":
    main()
