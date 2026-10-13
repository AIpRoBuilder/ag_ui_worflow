"""Class-map JSON construction, native node ownership, and pool API compatibility."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import gc
import importlib
import json
from pathlib import Path
import subprocess
import sys
import textwrap
from threading import Barrier
from types import MappingProxyType, ModuleType
import unittest
from unittest.mock import Mock, patch
import weakref

from pydaograph import CStatus, GCondition, GElement, GNode


# Exercise the checkout, not a potentially stale installed ag_ui_workflow package.
package = ModuleType("_workflow_json_pool_tests")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[package.__name__] = package
params_module = importlib.import_module(f"{package.__name__}.params")
pool_module = importlib.import_module(f"{package.__name__}.pool")
WorkflowPipelinePool = pool_module.WorkflowPipelinePool
WORKFLOW_PARAM_KEY = params_module.WORKFLOW_PARAM_KEY


class PoolBuilderArgumentTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.pipeline = Mock()
        self.pipeline.buildFromJsonStr.return_value = CStatus()
        self.pipeline.createGParam.return_value = CStatus()
        self.pipeline.registerGElement.return_value = CStatus()
        self.manager = Mock()
        for operation in ("add", "init", "destroy", "clear"):
            getattr(self.manager, operation).return_value = CStatus()
        self.create_pipeline = self.stack.enter_context(
            patch.object(pool_module, "GPipeline", return_value=self.pipeline)
        )
        self.create_manager = self.stack.enter_context(
            patch.object(pool_module, "GPipelineManager", return_value=self.manager)
        )
        self.stack.enter_context(patch.object(pool_module, "WorkflowParam"))

    def test_mapping_is_copied_to_a_dict_for_the_native_overload(self):
        classes = {"step": GNode}
        with WorkflowPipelinePool(
            pipeline_json='{"nodes": [{"name": "step"}]}',
            node_classes=MappingProxyType(classes),
        ):
            graph, passed_classes = self.pipeline.buildFromJsonStr.call_args.args
            self.assertEqual(graph, '{"nodes": [{"name": "step"}]}')
            self.assertIsInstance(passed_classes, dict)
            self.assertEqual(passed_classes, classes)
            self.assertIsNot(passed_classes, classes)
        self.manager.init.assert_called_once_with()
        self.manager.destroy.assert_called_once_with()

    def test_json_requires_an_explicit_class_map_before_allocating(self):
        for graph in ('{"nodes": []}', '{"nodes": [{"name": "step", "type": "RegisteredNode"}]}'):
            for options in ({}, {"node_classes": None}):
                with self.subTest(graph=graph, options=options):
                    with self.assertRaisesRegex(ValueError, "pipeline_json requires node_classes"):
                        WorkflowPipelinePool(pipeline_json=graph, **options)
        self.create_manager.assert_not_called()
        self.create_pipeline.assert_not_called()
        self.pipeline.buildFromJsonStr.assert_not_called()

    def test_empty_mapping_does_not_fall_back_to_the_registry(self):
        with WorkflowPipelinePool(pipeline_json='{"nodes": []}', node_classes={}):
            self.pipeline.buildFromJsonStr.assert_called_once_with('{"nodes": []}', {})

    def test_class_map_requires_a_json_source(self):
        for source in ({"nodes": []}, {"pipelines": [self.pipeline]}):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "node_classes requires pipeline_json"):
                WorkflowPipelinePool(node_classes={}, **source)
        self.create_manager.assert_not_called()

    def test_native_build_error_is_reported_without_initializing(self):
        self.pipeline.buildFromJsonStr.return_value = CStatus(-1, "node class not found")
        with self.assertRaisesRegex(RuntimeError, "buildFromJsonStr failed: node class not found"):
            WorkflowPipelinePool(pipeline_json='{"nodes": [{"name": "missing"}]}', node_classes={})
        self.manager.init.assert_not_called()
        self.manager.add.assert_not_called()
        self.manager.destroy.assert_not_called()
        self.manager.clear.assert_called_once_with()

    def test_constructor_exception_is_preserved_and_manager_is_cleared(self):
        self.pipeline.buildFromJsonStr.side_effect = ValueError("constructor failed")
        with self.assertRaisesRegex(ValueError, "constructor failed"):
            WorkflowPipelinePool(pipeline_json='{"nodes": [{"name": "step"}]}', node_classes={"step": GNode})
        self.manager.init.assert_not_called()
        self.manager.clear.assert_called_once_with()

    def test_node_classes_source_still_registers_dependency_instances(self):
        class First:
            STEP_ID, DEPENDENCIES = "first", []

        class Last:
            STEP_ID, DEPENDENCIES = "last", ["first"]

        with WorkflowPipelinePool(nodes=[First, Last]) as pool:
            first, last = pool._nodes[0]["first"], pool._nodes[0]["last"]
            calls = self.pipeline.registerGElement.call_args_list
            self.assertEqual(calls[0].args, (first, set(), "first"))
            self.assertEqual(calls[1].args, (last, {first}, "last"))
            self.pipeline.buildFromJsonStr.assert_not_called()


_node_references = []


class RecordingNode(GNode):
    def __init__(self, label="class"):
        super().__init__()
        self.label = label
        self.initializations = 0
        self.runs = 0
        _node_references.append(weakref.ref(self))

    def init(self):
        self.initializations += 1
        self.param = self.getGParam(WORKFLOW_PARAM_KEY)
        return CStatus()

    def run(self):
        self.runs += 1
        self.param.state.setdefault("visits", []).append((self.getName(), self.label, self.param.request_id))
        return CStatus()

    def clone(self):
        raise AssertionError("class-map builds must construct fresh nodes, not clone prototypes")


class SelectBranch(GCondition):
    def choose(self):
        return self.getGParam(WORKFLOW_PARAM_KEY).inputs.get("branch", 1)


class JsonPoolNativeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _node_references.clear()
        cls.registered = GElement.registeredClasses()
        graph = {"nodes": [
            {"name": "finish", "depends": ["left", "right"]},
            {"name": "repeat", "depends": ["first"], "loop": 2, "type": "unregistered"},
            {"name": "first"},
            {"name": "select", "meta_type": "GCondition", "depends": ["repeat"]},
            {"name": "left", "depends": ["select"]},
            {"name": "right", "depends": ["select"]},
        ]}
        classes = {name: RecordingNode for name in ("first", "left", "right", "finish")}
        classes["repeat"] = lambda: RecordingNode("factory")
        classes["select"] = SelectBranch
        cls.pool = WorkflowPipelinePool(pipeline_json=json.dumps(graph), node_classes=MappingProxyType(classes), size=2)

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()
        gc.collect()
        if any(reference() is not None for reference in _node_references):
            raise AssertionError("JSON node wrappers outlived their closed pool")

    def test_names_dependencies_loops_factory_and_condition_branches(self):
        with self.pool.lease() as (pipeline, param):
            param.request_id = "json"
            self.assertTrue(pipeline.run().isOK())
            self.assertEqual(param.state["visits"], [
                ("first", "class", "json"),
                ("repeat", "factory", "json"),
                ("repeat", "factory", "json"),
                ("right", "class", "json"),
                ("finish", "class", "json"),
            ])
        self.assertEqual(param.state, {})
        self.assertEqual(GElement.registeredClasses(), self.registered)

    def test_nodes_survive_collection_and_reuse_without_reinitialization(self):
        gc.collect()
        nodes = [reference() for reference in _node_references]
        self.assertEqual(len(nodes), 10)
        self.assertEqual(len({id(node) for node in nodes}), 10)
        self.assertTrue(all(node is not None and node.initializations == 1 for node in nodes))
        with self.pool.lease() as (pipeline, param):
            param.request_id = "reuse"
            param.inputs["branch"] = 0
            self.assertIs(pipeline.getGParam(WORKFLOW_PARAM_KEY), param)
            self.assertTrue(pipeline.run().isOK())
            self.assertEqual([visit[0] for visit in param.state["visits"]], ["first", "repeat", "repeat", "left", "finish"])
        self.assertTrue(all(node.initializations == 1 for node in nodes))

    def test_parallel_json_pipelines_have_isolated_nodes_and_parameters(self):
        barrier = Barrier(2)

        def execute(request_id):
            with self.pool.lease() as (pipeline, param):
                param.request_id = request_id
                barrier.wait(timeout=10)
                self.assertTrue(pipeline.run().isOK())
                self.assertTrue(all(visit[2] == request_id for visit in param.state["visits"]))
                return pipeline, param

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(execute, ("parallel-a", "parallel-b")))
        self.assertIsNot(results[0][0], results[1][0])
        self.assertIsNot(results[0][1], results[1][1])

    def run_workflow_example(self, code):
        # Isolate extra native pools from this pool.
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_node_class_source_does_not_use_the_global_registry_or_clone(self):
        self.run_workflow_example('''
            import runpy
            from unittest.mock import patch
            from pydaograph import GElement
            demo = runpy.run_path("tests/debug_engine.py")
            nodes = demo["NODES"]
            registered = GElement.registeredClasses()
            with patch.object(GElement, "registerClass", side_effect=AssertionError("global registration")), \\
                 patch.object(GElement, "create", side_effect=AssertionError("global lookup")), \\
                 patch.object(GElement, "clone", side_effect=AssertionError("prototype cloning")):
                with demo["WorkflowPipelinePool"](nodes=nodes, size=2) as pool:
                    engine = demo["WorkflowEngine"](
                        graph_config_id="explicit", steps_meta=[n.step_meta() for n in nodes], pool=pool
                    )
                    try:
                        assert engine.run("explicit").status == "awaiting_input"
                        result = engine.answer("explicit", "Ada")
                        assert result.status == "completed", result.error
                        assert result.outputs["greet"].derived["greeting"] == "Hello, Ada!"
                    finally:
                        engine.close()
            assert GElement.registeredClasses() == registered
        ''')

    def test_condition_branches_and_merge_with_workflow_output_dependencies(self):
        self.run_workflow_example('''
            import json
            import runpy
            from pydaograph import GCondition
            runpy.run_path("tests/debug_engine.py")
            from ag_ui_workflow import StepRunOutput, WorkflowEngine, WorkflowPipelinePool, WorkflowStepNode
            from ag_ui_workflow.params import WORKFLOW_PARAM_KEY

            class Root(WorkflowStepNode):
                STEP_ID = "root"
                def process_input(self, *args):
                    return StepRunOutput(derived={"root": True})

            class Select(GCondition):
                def choose(self):
                    return self.getGParam(WORKFLOW_PARAM_KEY).inputs["branch"]

            class Left(Root):
                STEP_ID = "left"
                DEPENDENCIES = ["root"]  # Required output, not the native condition edge.

            class Right(Left):
                STEP_ID = "right"

            class Merge(Left):
                STEP_ID = "merge"
                def process_input(self, *args):
                    outputs = self.get_workflow_param().outputs
                    selected = "left" if "left" in outputs else "right"
                    return StepRunOutput(derived={"selected": selected})

            nodes = [Root, Left, Right, Merge]
            graph = json.dumps({"nodes": [
                {"name": "root"},
                {"name": "select", "meta_type": "GCondition", "depends": ["root"]},
                {"name": "left", "depends": ["select"]},
                {"name": "right", "depends": ["select"]},
                {"name": "merge", "depends": ["left", "right"]},
            ]})
            classes = {node.STEP_ID: node for node in nodes}
            classes["select"] = Select
            with WorkflowPipelinePool(pipeline_json=graph, node_classes=classes) as pool:
                engine = WorkflowEngine(
                    graph_config_id="condition", steps_meta=[node.step_meta() for node in nodes], pool=pool
                )
                try:
                    for branch, name in enumerate(("left", "right")):
                        result = engine.run(name, {"branch": branch})
                        assert result.status == "completed", result.error
                        assert set(result.outputs) == {"root", name, "merge"}, result.outputs
                        assert result.outputs["merge"].derived == {"selected": name}
                finally:
                    engine.close()
        ''')


if __name__ == "__main__":
    unittest.main()