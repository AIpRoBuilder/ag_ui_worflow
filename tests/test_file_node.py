"""Abstract file nodes receive bytes; subclasses own acquisition and storage policy."""
import base64
import inspect
import json
from pathlib import Path
import runpy
import unittest
from typing import get_args
from unittest.mock import Mock, patch

from pydaograph import GPipeline


# Exercise the checkout rather than a previously installed distribution.
runpy.run_path(str(Path(__file__).with_name("debug_engine.py")))
from ag_ui_workflow import (  # noqa: E402
    StepRunInput,
    StepRunOutput,
    WorkflowEngine,
    WorkflowFileNode,
    WorkflowParam,
    WorkflowPipelinePool,
    WorkflowStepNode,
)
from ag_ui_workflow.tools import (  # noqa: E402
    get_node_main_utility_signature,
    get_node_subclass_implementation_signatures,
)


class SourceNode(WorkflowStepNode):
    STEP_ID = "source"
    INPUT_REQUIRED = False

    def process_input(self, user_input, dependency_results, session_state):
        return StepRunOutput(derived={"source": "test"})


class ReceiveFileNode(WorkflowFileNode):
    STEP_ID = "file"
    TITLE = "Receive file"
    PROMPT = "Provide file bytes."
    DEPENDENCIES = ["source"]
    received = []

    def __init__(self):
        super().__init__()
        self.setWaitFunc(self.wait_function)

    def wait_function(self) -> bool:
        param = self.get_workflow_param()
        with param.condition:
            if not param.can_run(self.STEP_ID, self.DEPENDENCIES):
                return True
            value = param.state.get("incoming", param.inputs.get(self.STEP_ID))
            # Transport-specific decoding is deliberately outside the base node.
            if isinstance(value, dict) and isinstance(value.get("contentBase64"), str):
                try:
                    value = base64.b64decode(value["contentBase64"], validate=True)
                except ValueError:
                    value = None
            ready = isinstance(value, (bytes, bytearray))
            if ready:
                param.inputs[self.STEP_ID] = value
            param.set_state(self.STEP_ID, "running" if ready else "awaiting_input")
            return ready

    def process_input(self, file_bytes, dependency_results, session_state):
        self.received.append((self.get_workflow_param().request_id, file_bytes))
        return StepRunOutput(
            card={"summary": f"Received {len(file_bytes)} bytes."},
            derived={"byteSize": len(file_bytes), "source": dependency_results["source"].derived["source"]},
        )


class FinishNode(WorkflowStepNode):
    STEP_ID = "finish"
    DEPENDENCIES = ["file"]
    INPUT_REQUIRED = False

    def process_input(self, user_input, dependency_results, session_state):
        return StepRunOutput(derived={"byteSize": dependency_results["file"].derived["byteSize"]})


class FileNodeContractTest(unittest.TestCase):
    def test_base_and_incomplete_subclass_cannot_be_instantiated(self):
        class IncompleteFileNode(WorkflowFileNode):
            pass

        for cls in (WorkflowFileNode, IncompleteFileNode):
            with self.subTest(node=cls.__name__):
                self.assertTrue(inspect.isabstract(cls))
                with self.assertRaisesRegex(TypeError, "abstract.*process_input"):
                    cls()
        self.assertFalse(inspect.isabstract(ReceiveFileNode))

    def test_bytes_are_part_of_the_engine_input_contract(self):
        self.assertIn(bytes, get_args(StepRunInput))
        self.assertIn(bytearray, get_args(StepRunInput))

    def test_file_metadata_uses_the_packaged_bytes_descriptor(self):
        meta = ReceiveFileNode.step_meta()
        self.assertEqual(meta["id"], "file")
        self.assertEqual(meta["metaNodeKind"], "WorkflowFileNode")
        self.assertIn("process_input(file_bytes", meta["structure"])
        self.assertIn("abstract", meta["metaDescription"])

    def test_processing_hook_is_exposed_to_node_tools(self):
        self.assertEqual(get_node_main_utility_signature(ReceiveFileNode), "process_input")
        self.assertEqual(get_node_subclass_implementation_signatures(ReceiveFileNode), ["process_input"])

    def test_base_has_no_waiting_parsing_storage_or_registry_policy(self):
        for name in (
            "wait_function", "clone", "_parse_file_input", "_extract_file_item",
            "_resolve_storage_backend", "_resolve_storage_dir", "save_files", "save_files_remote",
            "build_step_output", "STORAGE_BACKEND_ENV", "STORAGE_DIR_ENV", "DEFAULT_STORAGE_BACKEND",
            "DEFAULT_STORAGE_DIR",
        ):
            with self.subTest(attribute=name):
                self.assertNotIn(name, WorkflowFileNode.__dict__)


class FileNodeRunTest(unittest.TestCase):
    def setUp(self):
        self.param = WorkflowParam()
        self.owner = Mock()
        self.owner.STEP_ID = "file"
        self.owner.DEPENDENCIES = []
        self.owner.get_workflow_param.return_value = self.param
        self.output = StepRunOutput(card={"summary": "received"}, derived={"byteSize": 4})
        self.owner.process_input.return_value = self.output
        self.owner.card_payload.return_value = self.output.card

    def test_binary_payload_is_forwarded_unchanged_and_recorded(self):
        data = b"\x00\xff\x80\x00"
        self.param.inputs["file"] = data
        status = WorkflowFileNode.run(self.owner)
        self.assertTrue(status.isOK(), status.getInfo())
        self.owner.process_input.assert_called_once_with(data, {}, self.param.state)
        self.assertIs(self.owner.process_input.call_args.args[0], data)
        self.assertIs(self.param.outputs["file"], self.output)
        self.assertEqual(self.param.cards["file"], self.output.card)
        self.assertEqual(self.param.states["file"], "completed")

    def test_bytearray_is_converted_to_bytes(self):
        self.param.inputs["file"] = bytearray(b"data")
        self.assertTrue(WorkflowFileNode.run(self.owner).isOK())
        data = self.owner.process_input.call_args.args[0]
        self.assertIsInstance(data, bytes)
        self.assertEqual(data, b"data")

    def test_empty_file_is_a_valid_binary_input(self):
        self.param.inputs["file"] = b""
        self.assertTrue(WorkflowFileNode.run(self.owner).isOK())
        self.owner.process_input.assert_called_once_with(b"", {}, self.param.state)

    def test_nonbinary_input_fails_without_parsing_or_processing(self):
        for value in (None, "path/to/file", {"fileBytes": b"data"}, [b"data"], 42):
            with self.subTest(value=value):
                self.param.clear()
                self.param.inputs["file"] = value
                status = WorkflowFileNode.run(self.owner)
                self.assertFalse(status.isOK())
                self.assertIn("bytes", status.getInfo())
                self.assertEqual(self.param.failed_step, "file")
                self.assertEqual(self.param.outputs, {})
        self.owner.process_input.assert_not_called()

    def test_missing_input_does_not_install_an_implicit_wait(self):
        status = WorkflowFileNode.run(self.owner)
        self.assertFalse(status.isOK())
        self.assertEqual(self.param.states["file"], "failed")
        self.owner.process_input.assert_not_called()

    def test_dependency_outputs_and_shared_state_reach_the_hook(self):
        dependency = StepRunOutput(derived={"source": "test"})
        self.owner.DEPENDENCIES = ["source"]
        self.param.outputs["source"] = dependency
        self.param.inputs["file"] = b"data"
        self.param.state["metadata"] = "custom"
        self.assertTrue(WorkflowFileNode.run(self.owner).isOK())
        self.owner.process_input.assert_called_once_with(b"data", {"source": dependency}, self.param.state)

    def test_completed_or_blocked_nodes_are_not_processed(self):
        self.owner.DEPENDENCIES = ["missing"]
        self.assertTrue(WorkflowFileNode.run(self.owner).isOK())
        self.owner.DEPENDENCIES = []
        self.param.outputs["file"] = self.output
        self.assertTrue(WorkflowFileNode.run(self.owner).isOK())
        self.owner.process_input.assert_not_called()

    def test_hook_errors_and_invalid_outputs_are_reported(self):
        self.param.inputs["file"] = b"data"
        self.owner.process_input.side_effect = ValueError("invalid file header")
        status = WorkflowFileNode.run(self.owner)
        self.assertFalse(status.isOK())
        self.assertIn("invalid file header", status.getInfo())
        self.param.clear()
        self.param.inputs["file"] = b"data"
        self.owner.process_input.side_effect = None
        self.owner.process_input.return_value = {}
        status = WorkflowFileNode.run(self.owner)
        self.assertFalse(status.isOK())
        self.assertIn("process_input must return StepRunOutput", status.getInfo())


class FileNodeEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nodes = (SourceNode, ReceiveFileNode, FinishNode)
        graph = {"nodes": [{"name": node.STEP_ID, "depends": node.DEPENDENCIES} for node in cls.nodes]}
        cls.pool = WorkflowPipelinePool(
            pipeline_json=json.dumps(graph), node_classes={node.STEP_ID: node for node in cls.nodes}, size=2,
        )

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()

    def setUp(self):
        ReceiveFileNode.received.clear()
        self.engine = WorkflowEngine(graph_config_id="files", steps_meta=[n.step_meta() for n in self.nodes], pool=self.pool)
        self.native_runs = []
        original_run = GPipeline.asyncRun

        def counted_run(pipeline):
            self.native_runs.append(pipeline)
            return original_run(pipeline)

        self.run_patch = patch.object(GPipeline, "asyncRun", counted_run)
        self.run_patch.start()

    def tearDown(self):
        self.engine.close()
        self.run_patch.stop()

    def test_prefilled_bytes_reach_subclass_and_downstream_node(self):
        data = b"\x00\xff\x89PNG\r\n"
        result = self.engine.run("prefilled", {"file": data})
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.outputs["finish"].derived, {"byteSize": len(data)})
        self.assertEqual(ReceiveFileNode.received, [("prefilled", data)])
        self.assertEqual(len(self.native_runs), 1)

    def test_binary_input_produces_json_serializable_sse_output(self):
        events = "".join(self.engine.events("events", {"file": b"\x00\xff"}))
        self.assertIn("Received 2 bytes.", events)
        self.assertIn("RUN_FINISHED", events)
        self.assertNotIn("RUN_ERROR", events)

    def test_processing_failure_releases_the_pipeline_for_reuse(self):
        with patch.object(ReceiveFileNode, "process_input", side_effect=ValueError("invalid file header")):
            result = self.engine.run("failed", {"file": b"bad header"})
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.failed_step, "file")
        self.assertIn("invalid file header", result.error)
        self.assertNotIn("file", result.outputs)
        self.assertEqual(self.pool._active, 0)
        result = self.engine.run("after-failure", {"file": b"valid bytes"})
        self.assertEqual(result.status, "completed", result.error)

    def test_custom_wait_and_invalid_retry_keep_the_same_future(self):
        result = self.engine.run("posted")
        self.assertEqual(result.status, "awaiting_input")
        self.assertEqual(result.awaiting["id"], "file")
        future = self.engine._runs["posted"].future
        result = self.engine.answer("posted", "not bytes")
        self.assertEqual(result.status, "awaiting_input")
        self.assertIs(self.engine._runs["posted"].future, future)
        result = self.engine.answer("posted", b"uploaded")
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(ReceiveFileNode.received, [("posted", b"uploaded")])
        self.assertEqual(len(self.native_runs), 1)

    def test_callback_can_decode_transport_input(self):
        data = b"\x00\xff\x01"
        result = self.engine.run("encoded", {"file": {"contentBase64": base64.b64encode(data).decode("ascii")}})
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(ReceiveFileNode.received, [("encoded", data)])

    def test_callback_can_receive_bytes_from_a_custom_source(self):
        self.assertEqual(self.engine.run("custom").status, "awaiting_input")
        param = self.engine._runs["custom"].param
        with param.condition:
            param.state["incoming"] = b"queued bytes"
            self.assertTrue(param.condition.wait_for(lambda: param.finished, timeout=5))
        result = self.engine.run("custom", resume=True)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(ReceiveFileNode.received, [("custom", b"queued bytes")])

    def test_bytearray_and_empty_file_are_supported(self):
        for request_id, value in (("mutable", bytearray(b"data")), ("empty", b"")):
            result = self.engine.run(request_id, {"file": value})
            self.assertEqual(result.status, "completed", result.error)
            self.assertEqual(ReceiveFileNode.received[-1], (request_id, bytes(value)))
            self.assertIsInstance(ReceiveFileNode.received[-1][1], bytes)

    def test_concurrent_waiting_runs_keep_file_bytes_separate(self):
        for request_id in ("a", "b"):
            self.assertEqual(self.engine.run(request_id).status, "awaiting_input")
        for request_id, data in (("b", b"second"), ("a", b"first")):
            result = self.engine.answer(request_id, data)
            self.assertEqual(result.status, "completed", result.error)
            self.assertEqual(ReceiveFileNode.received[-1], (request_id, data))

    def test_cancelled_input_wait_can_reuse_the_pooled_node(self):
        self.assertEqual(self.engine.run("cancel").status, "awaiting_input")
        self.engine.close("cancel")
        self.assertEqual(self.pool._active, 0)
        result = self.engine.run("reuse", {"file": b"new file"})
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(ReceiveFileNode.received, [("reuse", b"new file")])


if __name__ == "__main__":
    unittest.main()