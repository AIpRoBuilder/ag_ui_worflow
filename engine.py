from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from threading import Lock, Thread
from typing import Any, Iterator

from pydaograph import CStatus, GPipeline, StdFutureCStatus

from .events import WorkflowEventFactory
from .params import WorkflowParam
from .pool import WorkflowPipelinePool
from .streaming import to_sse_payload
from .workflow_types import StepRunInput, StepRunOutput, step_output_text


@dataclass(slots=True)
class RunResult:
    """What one request saw of its run: the steps it completed and what the run waits for."""

    request_id: str
    status: str  # "completed", "awaiting_input" or "failed"
    new_steps: list[str]  # steps completed during this request, in order
    outputs: dict[str, StepRunOutput]  # every step completed so far
    states: dict[str, str]
    awaiting: dict[str, Any] | None = None  # meta of the step the run waits on
    error: str = ""
    code: int = 0
    failed_step: str = ""
    deltas: dict[str, list[str]] = field(default_factory=dict)


class _Run:
    """One leased pipeline and its native asyncRun future, retained across requests."""

    def __init__(self) -> None:
        self.lock = Lock()
        self.lease = ExitStack()
        self.pipeline: GPipeline | None = None
        self.param: WorkflowParam | None = None
        self.future: StdFutureCStatus | None = None
        self.completion: Thread | None = None
        self.reported = 0
        self.ended = False

    def start(self, pool: WorkflowPipelinePool, request_id: str, inputs: dict[str, StepRunInput]) -> None:
        self.pipeline, self.param = self.lease.enter_context(pool.lease())
        self.param.request_id = request_id
        for step_id, value in inputs.items():
            self.param.provide_input(step_id, value)
        self.future = self.pipeline.asyncRun()
        # Native futures expose get()/wait(), not callbacks or done(). This watcher only reports completion.
        self.completion = Thread(target=self._finish, name=f"workflow-result-{request_id}", daemon=True)
        self.completion.start()

    def _finish(self) -> None:
        try:
            status = self.future.get()
        except Exception as exc:
            status = CStatus(-1, f"pipeline run failed: {exc}")
        self.param.finish(status)

    def result(self, steps: dict[str, dict[str, Any]]) -> RunResult:
        """Observe reported pauses or native completion; never invoke the wait function."""
        param = self.param
        with param.condition:
            param.condition.wait_for(param.is_settled)
            failure = param.failure
            if failure is None and param.finished and not param.run_status.isOK():
                failure = param.run_status
            awaiting = param.awaiting if not param.finished and failure is None else []
            step_id = awaiting[0] if awaiting else ""
            result = RunResult(
                request_id=param.request_id,
                status="failed" if failure is not None else "awaiting_input" if awaiting else "completed",
                new_steps=param.completed[self.reported:],
                outputs=dict(param.outputs),
                states=dict(param.states),
                awaiting=steps.get(step_id, {"id": step_id}) if awaiting else None,
                error=failure.getInfo() if failure is not None else "",
                code=failure.getCode() if failure is not None else 0,
                failed_step=param.failed_step,
                deltas=dict(param.streamed_text_deltas),
            )
            self.reported = len(param.completed)
            return result

    def end(self) -> None:
        """Cancel the pipeline if it still runs, then hand it back to the pool."""
        self.ended = True
        # A cancel before native execution starts is reset; repeat until its future has completed.
        while self.completion is not None and self.completion.is_alive():
            if not self.param.finished:
                self.pipeline.cancel()
            self.completion.join(0.05)
        self.lease.close()


class WorkflowEngine:
    """Runs the steps of one graph config on pipelines leased from a shared pool.

    A run is identified by its ``request_id``. The first ``run`` with an id leases a pipeline
    and calls ``asyncRun()`` once. Subclasses explicitly install ``setWaitFunc(wait_function)``;
    the native graph pauses while that function returns False. The engine only observes
    states reported through WorkflowParam, posts input, and collects native completion.
    Callbacks that do not report a pause simply keep this call open until the graph finishes.
    """

    def __init__(
        self,
        *,
        graph_config_id: str,
        steps_meta: list[dict[str, Any]],
        pool: WorkflowPipelinePool,
    ) -> None:
        self.graph_config_id = graph_config_id
        self.pool = pool
        self.steps_meta = self._normalize_steps_meta(steps_meta)
        self._step_map = {step["id"]: step for step in self.steps_meta}
        self._events = WorkflowEventFactory()

        self._lock = Lock()  # guards _runs; each run has its own lock for its requests
        self._runs: dict[str, _Run] = {}

    def _normalize_steps_meta(self, steps_meta: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized_steps: list[dict[str, Any]] = []
        for raw_step in steps_meta:
            if not isinstance(raw_step, dict):
                continue

            step = dict(raw_step)
            step_id = str(step.get("id", "")).strip()
            if not step_id:
                continue

            dependencies_raw = step.get("dependencies") or []
            if isinstance(dependencies_raw, (str, bytes)):
                dependencies_raw = [dependencies_raw]
            if not isinstance(dependencies_raw, list):
                dependencies_raw = []

            step["id"] = step_id
            step["dependencies"] = [str(dep).strip() for dep in dependencies_raw if str(dep).strip()]
            normalized_steps.append(step)

        return normalized_steps

    def _terminal_step_ids(self) -> set[str]:
        parents = {dep for step in self.steps_meta for dep in step["dependencies"]}
        return {step["id"] for step in self.steps_meta if step["id"] not in parents}

    def awaiting_input(self, request_id: str) -> str | None:
        """Step the run of this request id waits on, if any."""
        with self._lock:
            run = self._runs.get(request_id)
        if run and run.param:
            with run.param.condition:
                return next(iter(run.param.awaiting), None)
        return None

    def run(
        self,
        request_id: str,
        inputs: dict[str, StepRunInput] | None = None,
        *,
        resume: bool = False,
    ) -> RunResult:
        """Start the run of ``request_id``, or continue it, feeding ``inputs`` by step id.

        With ``resume`` a run must already exist, otherwise ``LookupError`` is raised.
        A user wait function reports ``awaiting_input`` through ``param.set_state`` if this
        call should return while paused. Inputs are not validated or interpreted here.
        """
        while True:
            with self._lock:
                run = self._runs.get(request_id)
                if run is None:
                    if resume:
                        raise LookupError(f"no run exists for request {request_id}")
                    run = self._runs[request_id] = _Run()
            # A full pool may block this request, but never holds the global registry lock.
            with run.lock:
                if run.ended:
                    continue
                try:
                    if run.future is None:
                        run.start(self.pool, request_id, inputs or {})
                    else:
                        for step_id, value in (inputs or {}).items():
                            run.param.provide_input(step_id, value)
                    result = run.result(self._step_map)
                except BaseException:
                    self._drop(request_id, run)
                    raise
                if result.status != "awaiting_input":
                    self._drop(request_id, run)
                return result

    def answer(self, request_id: str, user_input: StepRunInput) -> RunResult:
        """Continue the run waiting on ``request_id`` with the user's input for its waiting step."""
        step_id = self.awaiting_input(request_id)
        if step_id is None:
            raise LookupError(f"no run is waiting for request {request_id}")
        return self.run(request_id, {step_id: user_input}, resume=True)

    def events(
        self,
        request_id: str,
        inputs: dict[str, StepRunInput] | None = None,
        *,
        resume: bool = False,
    ) -> Iterator[str]:
        """``run`` as a stream of AG-UI events (server-sent event payloads)."""
        yield to_sse_payload(self._events.start_event(self.graph_config_id, request_id))
        result = self.run(request_id, inputs, resume=resume)
        yield from self._step_events(result)
        if result.awaiting is not None:
            yield to_sse_payload(self._events.input_required_event(step=result.awaiting))
        if result.status == "failed":
            yield to_sse_payload(self._events.error_event(message=result.error, code=str(result.code)))
        yield to_sse_payload(
            self._events.finish_event(self.graph_config_id, request_id, result=self._finish_payload(result))
        )

    def close(self, request_id: str | None = None) -> None:
        """Abandon runs that wait for input (one request id, or all) and return their pipelines."""
        with self._lock:
            runs = [(rid, run) for rid, run in self._runs.items() if request_id in (None, rid)]
        # Runs that already hold a pipeline go first: one still waiting for a pipeline needs them freed.
        for rid, run in sorted(runs, key=lambda item: item[1].future is None):
            # An unreported native gate keeps run() inside its lock. Cancel before waiting for it.
            while not run.lock.acquire(timeout=0.05):
                if not run.ended and run.pipeline is not None:
                    run.pipeline.cancel()
            try:
                self._drop(rid, run)
            finally:
                run.lock.release()

    def _drop(self, request_id: str, run: _Run) -> None:
        run.end()
        with self._lock:
            if self._runs.get(request_id) is run:
                del self._runs[request_id]

    def _step_events(self, result: RunResult) -> Iterator[str]:
        terminal_ids = self._terminal_step_ids()
        for step_id in result.new_steps:
            output = result.outputs[step_id]
            yield to_sse_payload(self._events.step_started_event(step_name=step_id))
            for event in self._events.message_events(
                content=step_output_text(output),
                deltas=result.deltas.get(step_id),
            ):
                yield to_sse_payload(event)
            yield to_sse_payload(
                self._events.step_card_event(
                    step=self._step_map.get(step_id, {"id": step_id}),
                    output=output,
                    state=result.states.get(step_id, "completed"),
                    unlocked=True,
                    is_final=(step_id in terminal_ids),
                )
            )
            yield to_sse_payload(self._events.step_finished_event(step_name=step_id))

    def _finish_payload(self, result: RunResult) -> dict[str, Any]:
        if result.status == "failed":
            return {"ok": False, "stepId": result.failed_step}

        last_step_id = result.new_steps[-1] if result.new_steps else ""
        payload: dict[str, Any] = {
            "ok": True,
            "stepId": last_step_id,
            "isFinal": last_step_id in self._terminal_step_ids(),
            "completedSteps": list(result.outputs),
        }
        if payload["isFinal"]:
            payload["final"] = result.outputs[last_step_id].derived
        if result.awaiting is not None:
            payload["awaitingInput"] = result.awaiting["id"]
        return payload
