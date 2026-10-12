# ag-ui-workflow

`ag-ui-workflow` provides workflow runtime components for AG UI pipelines.

## Requirements

- Python 3.10 or newer
- `git`
- `pip`, `uv`, or Poetry
- A working C++ build toolchain because `pydaograph` is installed from source

On macOS, install Xcode Command Line Tools first:

```bash
xcode-select --install
```

## Install

### pip

Install from a local checkout:

```bash
python3.10 -m pip install .
```

### uv

Install from a local checkout:

```bash
uv venv .venv
source .venv/bin/activate
uv pip install .
```

### Poetry

Add the local checkout to an existing Poetry project:

```bash
poetry add /absolute/path/to/ag_ui_worflow
```

If you want to install from inside this checkout using Poetry's environment:

```bash
poetry env use python3.10
poetry run pip install .
```

Poetry 2.x is recommended.

## Quick Check

```python
from ag_ui_workflow import WorkflowEngine, WorkflowPipelinePool
```

## Usage

A `WorkflowPipelinePool` holds a fixed number of pipelines and hands them out one run at a
time. Build it once per process from node classes (listed after their dependencies), a graph
JSON string (`pipeline_json=`) or ready-made `pipelines=`, and share it between engines:

```python
nodes = [AskNameNode, GreetNode]
pool = WorkflowPipelinePool(nodes=nodes, size=4)
engine = WorkflowEngine(graph_config_id="demo", steps_meta=[n.step_meta() for n in nodes], pool=pool)
```

For an existing graph configuration, pass its JSON content directly:

```python
pool = WorkflowPipelinePool(pipeline_json=graph_json, size=4)
```

A run is identified by its `request_id`. `engine.run(request_id)` leases a pipeline and
calls native `asyncRun()` once. The engine retains that future and lease across requests;
a completion watcher collects its final status. It does not install, wrap, or invoke wait
functions. A subclass registers its own `setWaitFunc` callback: returning `False` pauses
native execution before the node runs, and `True` releases it.

Base step, file, skill, and condition nodes install **no** wait callback.
`INPUT_REQUIRED` is UI metadata only. If a subclass needs to wait, define a
`wait_function(self) -> bool` method inside the subclass and register it with
`self.setWaitFunc(self.wait_function)`. To make the engine
return a `RunResult` while paused, report `"awaiting_input"` through `param.set_state`;
report `"running"` when released. Without pause reporting, the engine call stays open
until native completion, which supports ordinary event/breakpoint callbacks.

```python
result = engine.run("req-1")                # starts the run; status "awaiting_input"
print(result.awaiting["prompt"])            # what the run asks for
result = engine.answer("req-1", "Ada")      # same run goes on; status "completed"
print(result.new_steps, result.outputs)     # steps finished by this call, and all outputs
```

`answer` gives the input to the step the run waits on and raises `LookupError` when no run
waits for that id, so it never starts a new run. `run(request_id, {step_id: input})` feeds
inputs by step id (also before a step is reached), and a finished run is forgotten, so its id
starts a fresh run. `engine.events(...)` takes the same arguments and yields AG-UI events: an
`input_required` custom event where the run waits, then `RUN_FINISHED`.

Define the native wait function inside the subclass, not in the engine:

```python
from ag_ui_workflow import StepRunOutput, WorkflowStepNode


class AskNameNode(WorkflowStepNode):
    STEP_ID = "ask_name"
    TITLE = "Ask Name"
    PROMPT = "What is your name?"

    def __init__(self):
        super().__init__()
        self.setWaitFunc(self.wait_function)

    def wait_function(self) -> bool:
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
        return StepRunOutput(card={"summary": user_input}, derived={"name": user_input})
```

The bound method is a non-blocking `() -> bool` callback polled by pydaograph. Pass
`self.wait_function`, not `self.wait_function()`, to `setWaitFunc`.
Input acquisition, validation, and release conditions are entirely user-defined: read posted
input, a queue, or a `threading.Event`. Call the base node's `self.get_workflow_param()` inside the
callback, after the pool attaches it. Protect input/state access with `param.condition`.
Return `True` for skipped nodes so failed dependencies do not leave a gate waiting forever.
Reset event-based gates for each run or store them in `param.state`, because pooled nodes
are reused. The engine never reruns completed steps to resume a paused graph.

The [console example](tests/debug_engine.py) collects input outside the engine and forwards
it with `answer()`. The [FastAPI example](tests/debug_engine_server.py) uses one global
engine and two POST endpoints; the [client](tests/debug_engine_client.py) supplies input
with the same `requestId`.

A waiting run keeps its pipeline, so give the pool enough pipelines for the runs that can
wait at once. `engine.close(request_id)` (or `engine.close()` for all) cancels waiting runs
natively and returns their pipelines; close the engine before the pool.

Nodes read and write run data with `self.get_workflow_param()`: `inputs` (user input per
step), `outputs`, `cards`, `states`, `state` and `streamed_text_deltas`. They skip themselves
when an upstream step did not finish.

## License

Apache License 2.0. See [LICENSE](LICENSE).