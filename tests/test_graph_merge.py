"""GraphSpec composition: merge graph definitions before building one pipeline."""
import copy
import importlib
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from pydaograph import CStatus, GNode, GPipeline


# Exercise the checkout, not a potentially stale installed ag_ui_workflow package.
package = ModuleType("_workflow_graph_merge_tests")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[package.__name__] = package
graph_module = importlib.import_module(f"{package.__name__}.graph")
pool_module = importlib.import_module(f"{package.__name__}.pool")
GraphSpec = graph_module.GraphSpec
merge_graphs = graph_module.merge_graphs
rebind_step_class = graph_module.rebind_step_class


class RecordingNode(GNode):
    order = []

    def run(self):
        type(self).order.append(self.getName())
        return CStatus()


def make_spec(nodes, *, steps_meta=()):
    """Build a spec whose nodes all use the same recording node class."""
    return GraphSpec(
        nodes=nodes,
        classes={node["name"]: RecordingNode for node in nodes},
        steps_meta=list(steps_meta),
    )


def node_names(spec):
    return [node["name"] for node in spec.nodes]


def node_by_name(spec, name):
    return next(node for node in spec.nodes if node["name"] == name)


class MergeGraphsTest(unittest.TestCase):
    def setUp(self):
        RecordingNode.order = []

    def test_bridge_runs_the_right_graph_after_the_left_graph(self):
        left = make_spec([{"name": "a"}, {"name": "b", "depends": ["a"]}])
        right = make_spec([{"name": "c"}, {"name": "d", "depends": ["c"]}])

        merged = merge_graphs(left, right, bridges={"c": ["b"]})

        self.assertEqual(node_names(merged), ["a", "b", "c", "d"])
        self.assertEqual(node_by_name(merged, "c")["depends"], ["b"])
        self.assertEqual(node_by_name(merged, "d")["depends"], ["c"])

        pipeline = GPipeline()
        status = pipeline.buildFromJsonStr(merged.pipeline_json, merged.node_classes)
        self.assertTrue(status.isOK(), status.getInfo())
        status = pipeline.process(1)
        self.assertTrue(status.isOK(), status.getInfo())
        self.assertEqual(RecordingNode.order, ["a", "b", "c", "d"])

    def test_duplicate_names_require_a_rename_function(self):
        left = make_spec([{"name": "start"}, {"name": "end", "depends": ["start"]}])
        right = make_spec([{"name": "start"}, {"name": "finish", "depends": ["start"]}])

        with self.assertRaisesRegex(ValueError, "duplicate node name"):
            merge_graphs(left, right, bridges={"start": ["end"]})

        merged = merge_graphs(
            left,
            right,
            bridges={"start": ["end"]},
            rename=lambda side, name: f"{side}:{name}",
        )

        self.assertEqual(
            node_names(merged),
            ["left:start", "left:end", "right:start", "right:finish"],
        )
        self.assertEqual(node_by_name(merged, "right:start")["depends"], ["left:end"])
        self.assertEqual(node_by_name(merged, "right:finish")["depends"], ["right:start"])

    def test_unknown_bridge_references_are_rejected(self):
        left = make_spec([{"name": "a"}])
        right = make_spec([{"name": "b"}])

        with self.assertRaisesRegex(ValueError, "bridge target"):
            merge_graphs(left, right, bridges={"missing": ["a"]})
        with self.assertRaisesRegex(ValueError, "bridge dependency"):
            merge_graphs(left, right, bridges={"b": ["missing"]})

    def test_steps_meta_is_merged_and_rewritten(self):
        left = make_spec(
            [{"name": "a"}, {"name": "b", "depends": ["a"]}],
            steps_meta=[
                {"id": "a", "title": "A", "dependencies": []},
                {"id": "b", "title": "B", "dependencies": ["a"]},
            ],
        )
        right = make_spec(
            [{"name": "c"}, {"name": "d", "depends": ["c"]}],
            steps_meta=[
                {"id": "c", "title": "C", "dependencies": [], "branches": ["d"]},
                {"id": "d", "title": "D", "dependencies": ["c"]},
            ],
        )

        merged = merge_graphs(
            left,
            right,
            bridges={"c": ["b"]},
            rename=lambda side, name: f"{side}:{name}",
        )

        meta = {entry["id"]: entry for entry in merged.steps_meta}
        self.assertEqual(meta["right:c"]["dependencies"], ["left:b"])
        self.assertEqual(meta["right:c"]["branches"], ["right:d"])
        self.assertEqual(meta["right:c"]["title"], "C")
        self.assertEqual(meta["left:b"]["dependencies"], ["left:a"])
        self.assertEqual(meta["right:d"]["dependencies"], ["right:c"])

    def test_merge_does_not_mutate_its_inputs(self):
        left = make_spec([{"name": "a"}, {"name": "b", "depends": ["a"]}])
        right = make_spec([{"name": "c"}])
        left_nodes = copy.deepcopy(left.nodes)
        right_nodes = copy.deepcopy(right.nodes)
        left_classes = dict(left.classes)

        merged = merge_graphs(left, right, bridges={"c": ["b"]})

        self.assertEqual(left.nodes, left_nodes)
        self.assertEqual(right.nodes, right_nodes)
        self.assertEqual(dict(left.classes), left_classes)
        self.assertIsNot(merged.nodes[0], left.nodes[0])
        self.assertEqual(node_by_name(merged, "c")["depends"], ["b"])

    def test_graph_spec_rejects_cycles_and_unknown_metadata(self):
        with self.assertRaisesRegex(ValueError, "dependency cycle"):
            GraphSpec(
                nodes=[{"name": "a", "depends": ["b"]}, {"name": "b", "depends": ["a"]}],
                classes={"a": RecordingNode, "b": RecordingNode},
            )

        with self.assertRaisesRegex(ValueError, "has no matching graph node"):
            GraphSpec(
                nodes=[{"name": "a"}],
                classes={"a": RecordingNode},
                steps_meta=[{"id": "missing", "dependencies": []}],
            )

        with self.assertRaisesRegex(ValueError, "no class factory"):
            GraphSpec(nodes=[{"name": "a"}], classes={})

    def test_from_json_accepts_the_pool_json_shape(self):
        spec = GraphSpec.from_json('{"nodes": [{"name": "a"}]}', {"a": RecordingNode})
        self.assertEqual(node_names(spec), ["a"])

        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            GraphSpec.from_json("{", {"a": RecordingNode})
        with self.assertRaisesRegex(ValueError, "nodes"):
            GraphSpec.from_json('{"node": []}', {"a": RecordingNode})


class RebindStepClassTest(unittest.TestCase):
    def test_new_subclass_overrides_step_metadata(self):
        class BaseStep:
            STEP_ID = "old"
            DEPENDENCIES = []

        merged_step = rebind_step_class(BaseStep, step_id="new", dependencies=["upstream"])

        self.assertEqual(BaseStep.STEP_ID, "old")
        self.assertEqual(BaseStep.DEPENDENCIES, [])
        self.assertEqual(merged_step.STEP_ID, "new")
        self.assertEqual(merged_step.DEPENDENCIES, ["upstream"])
        self.assertTrue(issubclass(merged_step, BaseStep))


class PoolReloadGraphTest(unittest.TestCase):
    def test_reload_graph_forwards_the_spec(self):
        spec = make_spec([{"name": "a"}])
        pool = pool_module.WorkflowPipelinePool.__new__(pool_module.WorkflowPipelinePool)

        with patch.object(pool_module.WorkflowPipelinePool, "reload") as reload:
            pool.reload_graph(spec, size=2)

        reload.assert_called_once_with(
            pipeline_json=spec.pipeline_json,
            node_classes=spec.node_classes,
            size=2,
        )


if __name__ == "__main__":
    unittest.main()
