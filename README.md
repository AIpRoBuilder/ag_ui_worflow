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
from ag_ui_workflow import WorkflowEngine, WorkflowSession
```

## SpatialTemporal Contract Node

`SpatialTemporalContractNode` generates a spatial-temporal contract JSON from a scenario, image, or video description by calling the OpenAI API with a packaged markdown system prompt.

Set `OPENAI_API_KEY` before using it. You can optionally override the model with `OPENAI_MODEL` or `session_state["spatialTemporalContractModel"]`.
You can also add extra generation guidance through `session_state["spatialTemporalContractPrompt"]` or override `_build_generation_user_prompt()` / `_generate_contract()` in a subclass.

## License

Apache License 2.0. See [LICENSE](LICENSE).