"""Run with: python3.10 -m unittest discover -s tests -p test_wait_functions.py."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import runpy
from threading import Event
import unittest
from unittest.mock import Mock, patch

from pydaograph import GPipeline

# Load the checkout via the console example without calling main().
demo = runpy.run_path(str(Path(__file__).with_name("debug_engine.py")))
from ag_ui_workflow import (  # noqa: E402
    StepRunOutput,
    WorkflowEngine,
    WorkflowFileNode,
    WorkflowParam,
    WorkflowPipelinePool,
    WorkflowStepNode,
)
from ag_ui_workflow.params import WORKFLOW_PARAM_KEY  # noqa: E402


class AutoNode(WorkflowStepNode):
    STEP_ID = "auto"
    INPUT_REQUIRED = True  # Metadata alone must not pause the pipeline.
    executed = []

    def process_input(self, user_input, dependency_results, session_state):
        self.executed.append(self.get_workflow_param().request_id)
        return StepRunOutput(derived={"auto": True})


class CallbackNode(demo["AskNameNode"]):
    DEPENDENCIES = ["auto"]
    executed = []
    hit, release = Event(), Event()

    def wait_function(self) -> bool:
        param = self.get_workflow_param()
        with param.condition:
            if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
                return True
            if param.state.get("callback_error"):
                raise ValueError("input source unavailable")
            if param.request_id.startswith("native-gate"):
                self.hit.set()
                ready = self.release.is_set()
                if ready:
                    param.inputs[self.STEP_ID] = "Event"
                if param.request_id != "native-gate-hidden":
                    param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
                return ready
            value = param.state.get("callback_value", param.inputs.get(self.STEP_ID))
            ready = isinstance(value, str) and (bool(value.strip()) or param.state.get("allow_empty", False))
            if ready:
                param.inputs[self.STEP_ID] = value.strip()
            param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
            return ready

    def process_input(self, user_input, dependency_results, session_state):
        self.executed.append((self.get_workflow_param().request_id, user_input))
        return super().process_input(user_input, dependency_results, session_state)


class FinishNode(demo["GreetNode"]):
    executed = []

    def process_input(self, user_input, dependency_results, session_state):
        self.executed.append(self.get_workflow_param().request_id)
        return super().process_input(user_input, dependency_results, session_state)


class WaitFunctionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nodes = (AutoNode, CallbackNode, FinishNode)
        graph_json = json.dumps({
            "nodes": [
                {"name": node.STEP_ID, "depends": node.DEPENDENCIES}
                for node in reversed(cls.nodes)
            ],
        })
        cls.pool = WorkflowPipelinePool(
            pipeline_json=graph_json,
            node_classes={node.STEP_ID: node for node in cls.nodes},
            size=2,
        )

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()

    def setUp(self):
        AutoNode.executed.clear()
        CallbackNode.executed.clear()
        FinishNode.executed.clear()
        CallbackNode.hit.clear()
        CallbackNode.release.clear()
        self.engine = WorkflowEngine(graph_config_id="test", steps_meta=[n.step_meta() for n in self.nodes], pool=self.pool)
        self.pipeline_calls = []
        original_run = GPipeline.asyncRun

        def counted_run(pipeline):
            self.pipeline_calls.append(pipeline)
            return original_run(pipeline)

        self.patch = patch.object(GPipeline, "asyncRun", counted_run)
        self.patch.start()

    def tearDown(self):
        CallbackNode.release.set()
        self.engine.close()
        self.patch.stop()

    def test_blank_answer_keeps_same_native_future(self):
        result = self.engine.run("posted")
        self.assertEqual(result.status, "awaiting_input")
        self.assertEqual(AutoNode.executed, ["posted"])
        native_future = self.engine._runs["posted"].future
        result = self.engine.answer("posted", "  ")
        self.assertEqual(result.status, "awaiting_input")
        self.assertIs(self.engine._runs["posted"].future, native_future)
        self.assertEqual(CallbackNode.executed, [])
        result = self.engine.answer("posted", "  Ada  ")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.outputs["greet"].derived, {"greeting": "Hello, Ada!"})
        self.assertEqual(CallbackNode.executed, [("posted", "Ada")])
        self.assertEqual(AutoNode.executed, ["posted"])
        self.assertEqual(FinishNode.executed, ["posted"])
        self.assertEqual(len(self.pipeline_calls), 1)
        with self.assertRaises(LookupError):
            self.engine.answer("posted", "again")

    def test_prefilled_input(self):
        result = self.engine.run("prefilled", {"ask_name": "Grace"})
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.outputs["ask_name"].derived, {"name": "Grace"})
        self.assertEqual(len(self.pipeline_calls), 1)

    def _callback_result(self, request_id, **state):
        self.assertEqual(self.engine.run(request_id).status, "awaiting_input")
        param = self.engine._runs[request_id].param
        with param.condition:
            param.state.update(state)
            self.assertTrue(param.condition.wait_for(lambda: param.finished, timeout=5))
        return self.engine.run(request_id, resume=True)

    def test_custom_source(self):
        result = self._callback_result("custom", callback_value="Lin")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.outputs["ask_name"].derived, {"name": "Lin"})
        self.assertEqual(len(self.pipeline_calls), 1)

    def test_custom_validation(self):
        result = self._callback_result("empty", callback_value="", allow_empty=True)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.outputs["ask_name"].derived, {"name": ""})

    def test_wait_function_exception_is_reported(self):
        result = self._callback_result("error", callback_error=True)
        self.assertEqual(result.status, "failed")
        self.assertIn("input source unavailable", result.error)
        self.assertEqual(CallbackNode.executed, [])

    def test_native_event_gate(self):
        result = self.engine.run("native-gate")
        self.assertEqual(result.status, "awaiting_input")
        self.assertTrue(CallbackNode.hit.is_set())
        param = self.engine._runs["native-gate"].param
        CallbackNode.release.set()
        with param.condition:
            self.assertTrue(param.condition.wait_for(lambda: param.finished, timeout=5))
        self.assertEqual(self.engine.run("native-gate", resume=True).status, "completed")
        self.assertEqual(len(self.pipeline_calls), 1)

    def test_unreported_event_gate_keeps_engine_call_open(self):
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(self.engine.run, "native-gate-hidden")
            try:
                self.assertTrue(CallbackNode.hit.wait(timeout=5))
                self.assertFalse(result.done())
            finally:
                CallbackNode.release.set()
            self.assertEqual(result.result(timeout=5).status, "completed")
        self.assertEqual(len(self.pipeline_calls), 1)

    def test_close_cancels_unreported_event_gate(self):
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(self.engine.run, "native-gate-hidden")
            try:
                self.assertTrue(CallbackNode.hit.wait(timeout=5))
                self.engine.close()
                self.assertEqual(result.result(timeout=5).status, "failed")
                self.assertEqual(self.pool._active, 0)
            finally:
                CallbackNode.release.set()

    def test_concurrent_runs_keep_inputs_separate(self):
        for request_id in ("a", "b"):
            self.assertEqual(self.engine.run(request_id).status, "awaiting_input")
        for request_id, name in (("b", "Bob"), ("a", "Ada")):
            result = self.engine.answer(request_id, name)
            self.assertEqual(result.outputs["ask_name"].derived, {"name": name})
        self.assertEqual(len(self.pipeline_calls), 2)

    def test_sse_events_use_subclass_wait_function(self):
        self.assertIn("input_required", "".join(self.engine.events("sse")))
        completed = "".join(self.engine.events("sse", {"ask_name": "Ada"}, resume=True))
        self.assertIn("Hello, Ada!", completed)
        self.assertEqual(len(self.pipeline_calls), 1)

    def test_cancel_and_reuse(self):
        self.engine.run("cancel")
        self.engine.close("cancel")
        self.assertEqual(self.pool._active, 0)
        for request_id in ("reuse-1", "reuse-2"):
            self.assertEqual(self.engine.run(request_id).status, "awaiting_input")
            self.assertEqual(self.engine.answer(request_id, "Ada").status, "completed")


class NodeParamAccessTest(unittest.TestCase):
    bases = (WorkflowStepNode, WorkflowFileNode)

    def test_base_nodes_have_no_global_registry_clone_hooks(self):
        for base in self.bases:
            with self.subTest(base=base.__name__):
                self.assertNotIn("clone", base.__dict__)

    def test_each_base_owns_lookup_method(self):
        param = WorkflowParam()
        for base in self.bases:
            with self.subTest(base=base.__name__):
                self.assertIn("get_workflow_param", base.__dict__)
                owner = Mock()
                owner.hasGParam.return_value = True
                owner.getGParam.return_value = param
                self.assertIs(base.get_workflow_param(owner), param)
                owner.hasGParam.assert_called_once_with(WORKFLOW_PARAM_KEY)
                owner.getGParam.assert_called_once_with(WORKFLOW_PARAM_KEY)

    def test_missing_param_is_rejected(self):
        for base in self.bases:
            with self.subTest(base=base.__name__):
                owner = Mock()
                owner.hasGParam.return_value = False
                with self.assertRaisesRegex(RuntimeError, "not attached"):
                    base.get_workflow_param(owner)
                owner.getGParam.assert_not_called()

    def test_wrong_param_type_is_rejected(self):
        for base in self.bases:
            with self.subTest(base=base.__name__):
                owner = Mock()
                owner.hasGParam.return_value = True
                owner.getGParam.return_value = object()
                with self.assertRaisesRegex(RuntimeError, "not attached"):
                    base.get_workflow_param(owner)


if __name__ == "__main__":
    unittest.main()