"""FastAPI server: a run with an input node, started by one POST and continued by another.

    python3.10 tests/debug_engine_server.py

    curl -X POST localhost:8000/runs -H 'content-type: application/json' \
         -d '{"requestId": "req-1"}'
    curl -X POST localhost:8000/runs/continue -H 'content-type: application/json' \
         -d '{"requestId": "req-1", "input": "Ada"}'

One global engine serves every request, and the `requestId` identifies the run. The first
request starts the run: the pipeline waits at the input node and keeps its pool slot. The
second request, with the same `requestId`, gives the node its input and the same run goes
on from where it stopped. tests/debug_engine_client.py makes both calls.
"""
import importlib.util
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The checkout folder is not named after the package and site-packages may hold a stale copy.
_spec = importlib.util.spec_from_file_location(
    "ag_ui_workflow", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
_package = importlib.util.module_from_spec(_spec)
sys.modules["ag_ui_workflow"] = _package
_spec.loader.exec_module(_package)

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from ag_ui_workflow import (  # noqa: E402
    RunResult,
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
        """Wait for a posted name and report the pause to the engine."""
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

# A run that waits for input keeps its pipeline, so the pool size caps the waiting runs.
POOL = WorkflowPipelinePool(pipeline_json=PIPELINE_JSON, node_classes=NODE_CLASSES, size=4)
ENGINE = WorkflowEngine(graph_config_id="demo", steps_meta=[n.step_meta() for n in NODES], pool=POOL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    finally:
        ENGINE.close()
        POOL.close()


app = FastAPI(lifespan=lifespan)


class StartRequest(BaseModel):
    requestId: str = Field(min_length=1)


class ContinueRequest(StartRequest):
    input: str


def respond(result: RunResult) -> dict:
    return {
        "requestId": result.request_id,
        "status": result.status,
        "awaiting": result.awaiting and {"stepId": result.awaiting["id"], "prompt": result.awaiting["prompt"]},
        "error": result.error or None,
        "steps": [
            {"stepId": step_id, "card": result.outputs[step_id].card, "derived": result.outputs[step_id].derived}
            for step_id in result.new_steps
        ],
    }


# Plain `def` endpoints run in FastAPI's thread pool, so waiting on the pipeline does not block the loop.
@app.post("/runs")
def start_run(body: StartRequest) -> dict:
    return respond(ENGINE.run(body.requestId))


@app.post("/runs/continue")
def continue_run(body: ContinueRequest) -> dict:
    try:
        return respond(ENGINE.answer(body.requestId, body.input))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
