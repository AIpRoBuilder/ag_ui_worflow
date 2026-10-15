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
JSON string with its explicit class map (`pipeline_json=` and `node_classes=`), or ready-made
`pipelines=`, and share it between engines:

```python
nodes = [AskNameNode, GreetNode]
pool = WorkflowPipelinePool(nodes=nodes, size=4)
engine = WorkflowEngine(graph_config_id="demo", steps_meta=[n.step_meta() for n in nodes], pool=pool)
```

For an existing graph configuration, pass its JSON content and a mapping from
**JSON node names** to Python classes (or zero-argument factories):

```python
import json

graph_json = json.dumps({
    "nodes": [
        {"name": "ask_name"},
        {"name": "greet", "depends": ["ask_name"]},
    ]
})
node_classes = {"ask_name": AskNameNode, "greet": GreetNode}
pool = WorkflowPipelinePool(pipeline_json=graph_json, node_classes=node_classes, size=4)
```

This calls `GPipeline.buildFromJsonStr(graph_json, node_classes)` for each pooled
pipeline. Every constructor must return a fresh `GElement`; use a factory such as
`lambda: ConfiguredNode(config)` when arguments are needed. Native pydaograph retains
the Python instances and their overrides. **Global node registration is not supported**:
do not use `pydaograph.register`, `register_class`, or `GElement.registerClass` for workflow
nodes. Workflow base nodes have no registry `clone()` hooks.
The map is keyed by `name`, not `type`; JSON `type` is optional and ignored.
`pipeline_json=` without `node_classes=` raises `ValueError` before allocating pipelines.
An explicit empty map is valid only for an empty graph; every declared node needs a map
entry, with no fallback to a global registry. `nodes=` constructs the supplied classes
directly; `registerGElement` attaches instances to that pipeline, not to a global registry.

To ship a new graph without interrupting running requests, call `pool.reload(...)` with the
same sources as the constructor (`pipeline_json=` and `node_classes=`, `nodes=` or `pipelines=`):

```python
pool.reload(pipeline_json=new_graph_json, node_classes=new_node_classes)
```

The new pipelines are built first, so a failed build leaves the current graph serving. Leases
already out, including runs paused for input, finish on the graph they started on, which is
destroyed with its last lease. New and queued requests use the new graph. An engine needs no
change when step ids, dependencies and metadata stay the same.

For workflow node subclasses, JSON `name` must match `STEP_ID`, and JSON `depends`
should match `DEPENDENCIES` for ordinary output-producing dependencies.
JSON declaration order need not be topological. Optional `loop` controls native run
repetitions; workflow nodes still record one completion per run. Native condition groups need
`meta_type: "GCondition"` and a mapped `GCondition` subclass; branch nodes list the
condition's name in `depends`, in branch-selection order.

Condition edges need extra care: `DEPENDENCIES` lists required **workflow outputs**,
whereas JSON `depends` controls native scheduling. A plain `GCondition` does not record
a workflow output. Branches should require only upstream outputs that actually exist,
unless the custom condition explicitly records its own output. A merge can list both
branches in JSON `depends` (pydaograph promotes them to the owning condition), but must
not require both alternative outputs in `DEPENDENCIES`; consume the selected branch's
output in its implementation instead.

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

## File bytes

`WorkflowFileNode` is an abstract bytes-input node. Implement
`process_input(file_bytes, dependency_results, session_state)` and return a `StepRunOutput`.
The base accepts one `bytes` payload (or converts a `bytearray` to `bytes`), records the
output, and reports errors. It does not install a wait function, parse upload formats,
read paths, or choose/save to a local or remote storage backend.

```python
from ag_ui_workflow import StepRunOutput, WorkflowEngine, WorkflowFileNode, WorkflowPipelinePool


class FileInfoNode(WorkflowFileNode):
    STEP_ID = "file"
    TITLE = "Receive file"

    def process_input(self, file_bytes, dependency_results, session_state):
        session_state["file_bytes"] = file_bytes
        return StepRunOutput(
            card={"summary": f"Received {len(file_bytes)} bytes."},
            derived={"byteSize": len(file_bytes)},
        )


with WorkflowPipelinePool(nodes=[FileInfoNode]) as pool:
    engine = WorkflowEngine(graph_config_id="file-demo", steps_meta=[FileInfoNode.step_meta()], pool=pool)
    try:
        result = engine.run("file-1", {"file": b"\x00\xff\x89PNG"})
        print(result.outputs["file"].derived["byteSize"])
    finally:
        engine.close()
```

For interactive uploads, register a subclass-bound `wait_function` as described above;
post bytes with `answer(request_id, file_bytes)` once it reports `"awaiting_input"`.
Decode base64/JSON, read paths, or acquire bytes from a queue in that callback or in the
caller, assigning the bytes to `param.inputs[STEP_ID]` before returning `True`.
See the [file-node descriptor example](nodes/file_node/descriptor_prompt.md).
Empty bytes are valid; content/size validation belongs to the subclass. Without a wait
callback, missing or nonbinary input fails rather than waiting automatically.

Previous `save_files` and `build_step_output` overrides must be replaced with
`process_input`; automatic storage and upload parsing have been removed. Keep binary
data in `session_state` when later nodes need it, and return JSON-serializable card/derived
metadata for AG-UI events. No global node registration or cloning is supported.

## License

Apache License 2.0. See [LICENSE](LICENSE).