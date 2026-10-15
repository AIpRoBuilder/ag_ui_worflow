from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Condition
from typing import Iterator, Sequence

from pydaograph import CStatus, GElement, GPipeline, GPipelineManager

from .params import WORKFLOW_PARAM_KEY, WorkflowParam


def _check(status: CStatus, operation: str) -> None:
    if status.isErr():
        raise RuntimeError(f"{operation} failed: {status.getInfo()}")


@dataclass(eq=False)
class _Generation:
    """The pipelines built from one graph and the leases currently out on them."""

    manager: GPipelineManager
    params: dict[GPipeline, WorkflowParam] = field(default_factory=dict)
    nodes: list[dict[str, object]] = field(default_factory=list)  # programmatically built nodes, kept until destroyed
    active: int = 0
    initialized: bool = False


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
    process to reuse initialized nodes and parameters across requests. ``reload`` replaces the
    graph without interrupting leases: they finish on the pipelines they hold.
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
        self._condition = Condition()
        self._closing = False
        self._closed = False
        self._retired: list[_Generation] = []  # replaced graphs that still have leases out
        self._current: _Generation | None = self._create(pipeline_json, node_classes, pipelines, nodes, size)
        self._size = len(self._current.params)

    @property
    def closed(self) -> bool:
        return self._closing or self._closed

    @property
    def _active(self) -> int:
        current = [self._current] if self._current is not None else []
        return sum(generation.active for generation in (*self._retired, *current))

    def reload(
        self,
        *,
        pipeline_json: str | None = None,
        node_classes: Mapping[str, Callable[[], GElement]] | None = None,
        pipelines: Sequence[GPipeline] | None = None,
        nodes: Sequence[type] | None = None,
        size: int | None = None,
    ) -> None:
        """Serve new leases from a freshly built graph; leases already out finish on the old one.

        Takes the same sources as the constructor, and keeps the current size unless ``size``
        is given. If the new graph fails to build, the current one keeps serving.
        """
        generation = self._create(
            pipeline_json, node_classes, pipelines, nodes, self._size if size is None else size
        )
        with self._condition:
            if not self.closed:
                self._retired.append(self._current)
                self._current = generation
                self._size = len(generation.params)
                self._condition.notify_all()  # waiters on a full old graph refetch from the new one
                self._reap()
                return
        self._destroy(generation)
        raise RuntimeError("workflow pipeline pool is closed")

    def _create(
        self,
        pipeline_json: str | None,
        node_classes: Mapping[str, Callable[[], GElement]] | None,
        pipelines: Sequence[GPipeline] | None,
        nodes: Sequence[type] | None,
        size: int,
    ) -> _Generation:
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

        generation = _Generation(GPipelineManager())
        try:
            if pipeline_json is not None:
                pipelines = [GPipeline() for _ in range(size)]
                for pipeline in pipelines:
                    _check(pipeline.buildFromJsonStr(pipeline_json, class_map), "buildFromJsonStr")
            elif nodes is not None:
                pipelines = [self._build(nodes, generation) for _ in range(size)]

            # Externally provided pipelines are expected to be built but not initialized.
            for pipeline in pipelines:
                param = WorkflowParam()
                _check(pipeline.createGParam(param, WORKFLOW_PARAM_KEY), "createGParam")
                generation.params[pipeline] = param
                _check(generation.manager.add(pipeline), "manager.add")

            _check(generation.manager.init(), "manager.init")
            generation.initialized = True
        except Exception:
            self._destroy(generation)
            raise
        return generation

    @staticmethod
    def _build(node_classes: Sequence[type], generation: _Generation) -> GPipeline:
        pipeline, built = GPipeline(), {}
        for cls in node_classes:
            node = built[cls.STEP_ID] = cls()
            _check(
                pipeline.registerGElement(node, {built[dep] for dep in cls.DEPENDENCIES}, cls.STEP_ID),
                f"register {cls.STEP_ID}",
            )
        generation.nodes.append(built)
        return pipeline

    @staticmethod
    def _destroy(generation: _Generation) -> None:
        if generation.initialized:
            _check(generation.manager.destroy(), "manager.destroy")
            generation.initialized = False
        _check(generation.manager.clear(), "manager.clear")
        generation.params.clear()
        generation.nodes.clear()

    def _reap(self) -> None:
        """Destroy replaced graphs with no lease left. Caller holds the condition."""
        for generation in [g for g in self._retired if g.active == 0]:
            self._destroy(generation)
            self._retired.remove(generation)

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
                generation = self._current
                pipeline = generation.manager.fetch()
                if pipeline is not None:
                    generation.active += 1
                    break
                # fetch() does not block. reload() also wakes this wait.
                self._condition.wait()

        param = generation.params[pipeline]
        try:
            yield pipeline, param
        finally:
            param.clear()
            with self._condition:
                try:
                    # Back to the graph it came from, even if that one has been replaced since.
                    _check(generation.manager.release(pipeline), "manager.release")
                finally:
                    generation.active -= 1
                    try:
                        self._reap()
                    finally:
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
                self._reap()
                self._destroy(self._current)
            except Exception:
                self._closing = False
                self._condition.notify_all()
                raise

            # The manager keeps the pipelines alive until its wrapper is released.
            self._current = None
            self._closed = True
            self._condition.notify_all()

    def __enter__(self) -> WorkflowPipelinePool:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()
