from concurrent.futures import ThreadPoolExecutor
import importlib
from pathlib import Path
import sys
from threading import Barrier, Event
from types import ModuleType
import unittest
from unittest.mock import patch

from pydaograph import CStatus, GNode, GPipeline


# Test the pool independently of the package's engine import.
package = ModuleType("_workflow_pool_tests")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[package.__name__] = package
params_module = importlib.import_module(f"{package.__name__}.params")
pool_module = importlib.import_module(f"{package.__name__}.pool")
workflow_types_module = importlib.import_module(f"{package.__name__}.workflow_types")
WorkflowParam = params_module.WorkflowParam
WorkflowPipelinePool = pool_module.WorkflowPipelinePool
WORKFLOW_PARAM_KEY = params_module.WORKFLOW_PARAM_KEY
StepRunOutput = workflow_types_module.StepRunOutput


class CanRunTest(unittest.TestCase):
    def test_repeat_allows_completed_step_and_keeps_dependency_check(self):
        param = WorkflowParam()
        param.complete("step", StepRunOutput(), {})

        self.assertFalse(param.can_run("step", []))
        self.assertTrue(param.can_run("step", [], repeat=True))
        self.assertFalse(param.can_run("step", ["missing"], repeat=True))

        param.complete("dep", StepRunOutput(), {})
        self.assertTrue(param.can_run("step", ["dep"], repeat=True))


class RequestNode(GNode):
    def __init__(self):
        super().__init__()
        self.observations = []

    def run(self):
        param = self.getGParam(WORKFLOW_PARAM_KEY)
        self.observations.append((param, param.request_id, dict(param.inputs)))
        return CStatus()


class PoolLeaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipelines = [GPipeline(), GPipeline()]
        cls.nodes = [RequestNode(), RequestNode()]
        for pipeline, node in zip(cls.pipelines, cls.nodes):
            result = pipeline.registerGElement(node, set(), "request")
            if not result.isOK():
                raise RuntimeError(result.getInfo())
        with patch.object(pool_module, "WorkflowParam", wraps=WorkflowParam) as create_param:
            cls.pool = WorkflowPipelinePool(pipelines=cls.pipelines)
        if create_param.call_count != len(cls.pipelines):
            raise AssertionError("each initialized pipeline must receive one parameter")

    @classmethod
    def tearDownClass(cls):
        cls.pool.close()

    def test_same_parameter_is_cleared_and_reused_for_each_pipeline(self):
        seen = {}
        for index in range(30):
            request_id = f"request-{index}"
            with self.pool.lease() as (pipeline, param):
                self.assertIsInstance(param, WorkflowParam)
                self.assertEqual(param.request_id, "")
                self.assertIs(pipeline.getGParam(WORKFLOW_PARAM_KEY), param)
                self.assertEqual(param.inputs, {})
                self.assertEqual(param.outputs, {})
                self.assertEqual(param.state, {})
                self.assertEqual(param.states, {})
                self.assertEqual(param.completed, [])
                self.assertFalse(param.finished)
                if pipeline in seen:
                    self.assertIs(param, seen[pipeline])
                seen[pipeline] = param
                param.request_id = request_id
                param.inputs["request"] = request_id
                param.state["old"] = request_id
                param.set_state("request", "running")
                self.assertTrue(pipeline.run().isOK())
                node = self.nodes[self.pipelines.index(pipeline)]
                self.assertIs(node.observations[-1][0], param)
                self.assertEqual(node.observations[-1][1:], (request_id, {"request": request_id}))
                param.finish(CStatus())
            self.assertEqual(param.request_id, "")
            self.assertEqual(param.inputs, {})
            self.assertEqual(param.state, {})
            self.assertEqual(param.states, {})
            self.assertFalse(param.finished)
            self.assertTrue(pipeline.hasGParam(WORKFLOW_PARAM_KEY))
            self.assertIs(pipeline.getGParam(WORKFLOW_PARAM_KEY), param)

    def test_request_id_is_assigned_by_the_caller(self):
        with self.pool.lease() as (_, first):
            self.assertEqual(first.request_id, "")
            first.request_id = "same-request"
            first.inputs["old"] = "data"
        with self.pool.lease() as (pipeline, second):
            self.assertEqual(second.request_id, "")
            second.request_id = "same-request"
            self.assertIs(pipeline.getGParam(WORKFLOW_PARAM_KEY), second)
            self.assertEqual(second.request_id, "same-request")
            self.assertEqual(second.inputs, {})

    def test_request_failure_clears_and_releases(self):
        with self.assertRaisesRegex(ValueError, "request failed"):
            with self.pool.lease() as (pipeline, param):
                param.request_id = "failure"
                param.inputs["secret"] = "value"
                raise ValueError("request failed")
        self.assertEqual(param.inputs, {})
        self.assertEqual(param.request_id, "")
        self.assertEqual(self.pool._active, 0)
        with self.pool.lease() as (recovered, next_param):
            self.assertIs(recovered.getGParam(WORKFLOW_PARAM_KEY), next_param)
            if recovered is pipeline:
                self.assertIs(next_param, param)
            self.assertEqual(next_param.inputs, {})

    def test_lease_does_not_create_or_register_parameters(self):
        with (
            patch.object(pool_module, "WorkflowParam", side_effect=AssertionError("parameter recreated")) as create_param,
            patch.object(GPipeline, "createGParam", side_effect=AssertionError("parameter registered again")) as register_param,
        ):
            with self.pool.lease() as (pipeline, param):
                self.assertIs(pipeline.getGParam(WORKFLOW_PARAM_KEY), param)
        create_param.assert_not_called()
        register_param.assert_not_called()
        self.assertEqual(self.pool._active, 0)

    def test_parallel_leases_have_distinct_pipelines_and_parameters(self):
        barrier = Barrier(2)

        def request(request_id):
            with self.pool.lease() as (pipeline, param):
                param.request_id = request_id
                barrier.wait(timeout=10)
                self.assertEqual(param.request_id, request_id)
                self.assertIs(pipeline.getGParam(WORKFLOW_PARAM_KEY), param)
                self.assertTrue(pipeline.run().isOK())
                return pipeline, param

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(request, ("parallel-a", "parallel-b")))
        self.assertIsNot(results[0][0], results[1][0])
        self.assertIsNot(results[0][1], results[1][1])

    def test_excess_request_waits_for_a_slot(self):
        entered = Event()
        waiting = Event()

        def request():
            waiting.set()
            with self.pool.lease() as (_, param):
                param.request_id = "waiting"
                entered.set()
                self.assertEqual(param.request_id, "waiting")

        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.pool.lease(), self.pool.lease():
                future = executor.submit(request)
                self.assertTrue(waiting.wait(timeout=10))
                self.assertFalse(entered.is_set())
            future.result(timeout=10)
        self.assertTrue(entered.is_set())

if __name__ == "__main__":
    unittest.main()