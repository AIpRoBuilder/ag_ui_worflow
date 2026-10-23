"""Console example: merge two graph specs and run the left side three times.

    python3.10 tests/debug_graph_merge_loop.py

The left graph is materialized as one native ``GRegion`` with a group loop.
Both left steps set ``REPEATABLE = True`` so ``WorkflowStepNode`` accepts the
native re-entry and overwrites its output on every pass.
"""
import importlib.util
import json
import sys
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
    GraphSpec,
    StepMeta,
    StepRunOutput,
    WorkflowEngine,
    WorkflowPipelinePool,
    WorkflowStepNode,
    merge_graphs,
)


class LoopingStep(WorkflowStepNode):
    """A step that is allowed to run again when the native group loops."""

    REPEATABLE = True
    INPUT_REQUIRED = False

    def __init__(self) -> None:
        super().__init__()
        self.runs = 0

    def process_input(self, user_input, dependency_results, session_state):
        self.runs += 1
        upstream = {
            dependency: output.derived.get("iteration")
            for dependency, output in dependency_results.items()
        }
        return StepRunOutput(
            card={"title": self.TITLE, "summary": f"{self.TITLE} pass {self.runs}"},
            derived={"iteration": self.runs, "upstream": upstream},
        )


class CollectStep(LoopingStep):
    STEP_ID = "collect"
    TITLE = "Collect"
    DEPENDENCIES: list[str] = []


class TransformStep(LoopingStep):
    STEP_ID = "transform"
    TITLE = "Transform"
    DEPENDENCIES = ["collect"]


class FinishStep(WorkflowStepNode):
    STEP_ID = "finish"
    TITLE = "Finish"
    DEPENDENCIES = ["transform"]
    INPUT_REQUIRED = False

    def __init__(self) -> None:
        super().__init__()
        self.runs = 0

    def process_input(self, user_input, dependency_results, session_state):
        self.runs += 1
        return StepRunOutput(
            card={"title": self.TITLE, "summary": f"Finished after loop pass {self.runs}"},
            derived={"finalIteration": self.runs},
        )


LEFT_NODES = (CollectStep, TransformStep)
RIGHT_NODES = (FinishStep,)

LEFT_JSON = json.dumps(
    {
        "nodes": [
            {"name": "collect"},
            {"name": "transform", "depends": ["collect"]},
        ]
    }
)
RIGHT_JSON = json.dumps({"nodes": [{"name": "finish"}]})


def minimal_step_meta(
    cls: type[WorkflowStepNode],
    *,
    dependencies: list[str] | None = None,
) -> StepMeta:
    """Engine metadata without the optional descriptor document payload."""
    return {
        "id": cls.STEP_ID,
        "title": cls.TITLE,
        "prompt": cls.PROMPT,
        "dependencies": list(cls.DEPENDENCIES) if dependencies is None else dependencies,
    }


LEFT_META = [minimal_step_meta(node) for node in LEFT_NODES]
# The bridge is added by merge_graphs, so the right spec declares no dependency yet.
RIGHT_META = [minimal_step_meta(FinishStep, dependencies=[])]


def main() -> None:
    left = GraphSpec.from_json(
        LEFT_JSON,
        {node.STEP_ID: node for node in LEFT_NODES},
        steps_meta=LEFT_META,
        loop=3,
    )
    right = GraphSpec.from_json(
        RIGHT_JSON,
        {node.STEP_ID: node for node in RIGHT_NODES},
        steps_meta=RIGHT_META,
    )
    merged = merge_graphs(left, right, bridges={"finish": ["transform"]})

    print("merged native graph:")
    print(json.dumps(json.loads(merged.pipeline_json), indent=2))
    print("\nmerged steps_meta (id/dependencies only):")
    print(
        json.dumps(
            [
                {"id": entry["id"], "dependencies": entry.get("dependencies", [])}
                for entry in merged.steps_meta
            ],
            indent=2,
        )
    )
    print("merged node_classes:", merged.node_classes, merged.pipeline_json)
    with WorkflowPipelinePool(
        pipeline_json=merged.pipeline_json,
        node_classes=merged.node_classes,
    ) as pool:

        engine = WorkflowEngine(
            graph_config_id="debug-merge-loop",
            steps_meta=merged.steps_meta,
            pool=pool,
        )
        try:
            result = engine.run("debug-merge-loop")
            print("\nnew steps:", result.new_steps)
            print("collect:", result.outputs["collect"].derived)
            print("transform:", result.outputs["transform"].derived)
            print("finish:", result.outputs["finish"].derived)
            if result.error:
                print("error:", result.error)
        finally:
            engine.close()


if __name__ == "__main__":
    main()
