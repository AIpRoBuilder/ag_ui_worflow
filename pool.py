from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from threading import Condition
from typing import Iterator, Sequence

from pydaograph import CStatus, GElement, GPipeline, GPipelineManager

from .params import WORKFLOW_PARAM_KEY, WorkflowParam


def _check(status: CStatus, operation: str) -> None:
    if status.isErr():
        raise RuntimeError(f"{operation} failed: {status.getInfo()}")


class WorkflowPipelinePool:
    """Fixed set of initialized pipelines handed out one run at a time.

    Pipelines come from a graph JSON string with an explicit ``node_classes`` mapping,
    from ready-built ``pipelines``, or from ``nodes``: node classes (each with ``STEP_ID``
    and ``DEPENDENCIES``, listed after their dependencies). JSON mappings use node names
    as keys and classes or zero-argument factories as values; each must create a fresh
    ``GElement``. The native builder retains these instances without registration or cloning.
    Global node registration and registry-based JSON loading are not supported.
    ``GPipelineManager`` owns the free/used lists; a ``Condition`` adds waiting for a free
    slot and coordinated shutdown. Build the pool once and keep it for the lifetime of the
    process to reuse initialized nodes and parameters across requests.
    """

    def __init__(
        self,
        *,
        pipeline_json: str | None = None,
        node_classes: Mapping[str, Callable[[], GElement]] | None = None,
        pipelines: Sequence[GPipeline] | None = None,
        nodes: Sequence[type] | None = None,
        size: int = 1,
    ) -> None:
        if sum(source is not None for source in (pipeline_json, pipelines, nodes)) != 1:
            raise ValueError("exactly one of pipeline_json, pipelines or nodes must be provided")
        if pipelines is None and (not isinstance(size, int) or size < 1):
            raise ValueError("size must be a positive integer")
        if pipeline_json is not None and not pipeline_json.strip():
            raise ValueError("pipeline_json must be a non-empty JSON string")
        if pipeline_json is not None and node_classes is None:
            raise ValueError("pipeline_json requires node_classes; global node registration is not supported")
        if node_classes is not None and pipeline_json is None:
            raise ValueError("node_classes requires pipeline_json")
        # The pybind overload accepts a dict, not an arbitrary Mapping. Snapshot it
        # once so every pooled pipeline is constructed from the same class map.
        class_map = dict(node_classes) if node_classes is not None else {}

        self._manager: GPipelineManager | None = GPipelineManager()
        self._params: dict[GPipeline, WorkflowParam] = {}
        self._nodes: list[dict[str, object]] = []  # programmatically built nodes, kept until shutdown
        self._condition = Condition()
        self._active = 0
        self._closing = False
        self._closed = False
        self._initialized = False

        try:
            if pipeline_json is not None:
                pipelines = [GPipeline() for _ in range(size)]
                for pipeline in pipelines:
                    _check(pipeline.buildFromJsonStr(pipeline_json, class_map), "buildFromJsonStr")
            elif nodes is not None:
                pipelines = [self._build(nodes) for _ in range(size)]

            # Externally provided pipelines are expected to be built but not initialized.
            for pipeline in pipelines:
                param = WorkflowParam()
                _check(pipeline.createGParam(param, WORKFLOW_PARAM_KEY), "createGParam")
                self._params[pipeline] = param
                _check(self._manager.add(pipeline), "manager.add")

            _check(self._manager.init(), "manager.init")
            self._initialized = True
        except Exception:
            self.close()
            raise

    @property
    def closed(self) -> bool:
        return self._closing or self._closed

    def _build(self, node_classes: Sequence[type]) -> GPipeline:
        pipeline, built = GPipeline(), {}
        for cls in node_classes:
            node = built[cls.STEP_ID] = cls()
            _check(
                pipeline.registerGElement(node, {built[dep] for dep in cls.DEPENDENCIES}, cls.STEP_ID),
                f"register {cls.STEP_ID}",
            )
        self._nodes.append(built)
        return pipeline

    @contextmanager
    def lease(self) -> Iterator[tuple[GPipeline, WorkflowParam]]:
        """Lease a pipeline exclusively, waiting while all are in use.

        The param is cleared and the pipeline returned to the pool on exit, even
        when the request fails.
        """
        with self._condition:
            while True:
                if self.closed:
                    raise RuntimeError("workflow pipeline pool is closed")
                pipeline = self._manager.fetch()
                if pipeline is not None:
                    self._active += 1
                    break
                # fetch() does not block.
                self._condition.wait()

        param = self._params[pipeline]
        try:
            yield pipeline, param
        finally:
            param.clear()
            with self._condition:
                try:
                    _check(self._manager.release(pipeline), "manager.release")
                finally:
                    self._active -= 1
                    self._condition.notify_all()

    def close(self) -> None:
        """Reject new requests, wait for active leases, then destroy the pipelines."""
        with self._condition:
            while self._closing and not self._closed:
                self._condition.wait()
            if self._closed:
                return
            self._closing = True
            self._condition.notify_all()
            while self._active:
                self._condition.wait()

            try:
                if self._initialized:
                    _check(self._manager.destroy(), "manager.destroy")
                    self._initialized = False
                _check(self._manager.clear(), "manager.clear")
            except Exception:
                self._closing = False
                self._condition.notify_all()
                raise

            # The manager keeps the pipelines alive until its wrapper is released.
            self._params.clear()
            self._manager = None
            self._nodes.clear()
            self._closed = True
            self._condition.notify_all()

    def __enter__(self) -> WorkflowPipelinePool:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()
