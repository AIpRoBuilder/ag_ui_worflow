"""Pure-Python graph composition for ``WorkflowPipelinePool``.

pydaograph builds a pipeline once and owns its elements; there is no native
operation that merges two initialized ``GPipeline`` objects. This module merges
the *definitions* instead: two ``GraphSpec`` values are combined into one DAG
that is then handed to ``GPipeline.buildFromJsonStr`` (directly or through
``WorkflowPipelinePool.reload``).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydaograph import GElement, GRegion


NodeFactory = Callable[[], GElement]

# The field names that hold workflow step ids in ``steps_meta`` entries.
_META_REFERENCE_FIELDS = ("dependencies", "branches")


@dataclass(slots=True)
class GraphSpec:
    """A pipeline graph definition with one node factory per node name.

    ``nodes`` uses the JSON shape accepted by ``GPipeline.buildFromJsonStr``:
    each entry is a dict with a unique ``name`` and an optional ``depends``
    list of node names. ``classes`` maps every node name to a zero-argument
    callable returning a fresh ``GElement``. ``steps_meta`` optionally carries
    the engine metadata (``id`` plus optional ``dependencies`` / ``branches``)
    for the same graph.

    ``loop`` repeats this whole graph when it is materialized: the spec is
    collapsed into one native ``GRegion`` node whose group loop runs the
    internal DAG that many times. It is not a per-node loop.

    ``hidden_steps`` lists step ids nested inside wrapper nodes produced by
    :func:`merge_graphs`. It lets ``steps_meta`` keep real step ids while the
    wrapped nodes are no longer top-level. Callers normally leave it empty.

    Construction validates unique names, known dependencies, one factory per
    node, metadata referencing real steps, and acyclic dependencies. Use
    :meth:`from_json` to start from an existing graph JSON string.
    """

    nodes: list[dict[str, Any]]
    classes: Mapping[str, NodeFactory]
    steps_meta: list[dict[str, Any]] = field(default_factory=list)
    loop: int = 1
    hidden_steps: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.nodes = [dict(node) for node in self.nodes]
        self.classes = dict(self.classes)
        self.steps_meta = [dict(step) for step in self.steps_meta]
        if isinstance(self.hidden_steps, (str, bytes, bytearray)):
            raise ValueError("hidden_steps must be an iterable of step ids, not a string")
        self.hidden_steps = set(self.hidden_steps)
        _validate_spec(self)

    @classmethod
    def from_json(
        cls,
        pipeline_json: str,
        node_classes: Mapping[str, NodeFactory],
        *,
        steps_meta: Iterable[dict[str, Any]] = (),
        loop: int = 1,
    ) -> GraphSpec:
        """Build a spec from ``{"nodes": [...]}`` JSON text and its class map."""
        try:
            payload = json.loads(pipeline_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"pipeline_json is not valid JSON: {exc.msg}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
            raise ValueError('pipeline_json must be a JSON object with a "nodes" array')
        return cls(
            nodes=payload["nodes"],
            classes=node_classes,
            steps_meta=list(steps_meta),
            loop=loop,
        )

    def materialized_graph(self) -> tuple[list[dict[str, Any]], dict[str, NodeFactory]]:
        """Return the node list and class map pydaograph should build.

        A graph with ``loop == 1`` is returned as-is. A looped graph is collapsed
        into one ``GRegion`` node carrying the native loop count, so its whole
        internal DAG executes that many times.
        """
        if self.loop == 1:
            return [dict(node) for node in self.nodes], dict(self.classes)

        wrapper = self._wrapper_name()
        names = {node["name"]: node["name"] for node in self.nodes}
        return (
            [{"name": wrapper, "meta_type": "GRegion", "loop": self.loop}],
            {wrapper: _region_factory(self, names)},
        )

    @property
    def pipeline_json(self) -> str:
        """JSON text accepted by ``GPipeline.buildFromJsonStr`` and the pool."""
        nodes, _ = self.materialized_graph()
        return json.dumps({"nodes": nodes}, ensure_ascii=False)

    @property
    def node_classes(self) -> dict[str, NodeFactory]:
        """The class map matching :attr:`pipeline_json`."""
        return self.materialized_graph()[1]

    def _wrapper_name(self) -> str:
        return _unique_name("__pipeline__", {node["name"] for node in self.nodes})


def merge_graphs(
    left: GraphSpec,
    right: GraphSpec,
    *,
    bridges: Mapping[str, Sequence[str]] | None = None,
    rename: Callable[[str, str], str] | None = None,
    loops: Mapping[str, int] | None = None,
) -> GraphSpec:
    """Combine ``left`` and ``right`` into one DAG and return the merged spec.

    ``bridges`` maps a node in the right graph to nodes in the left graph it
    must wait for; both keys and values use the original, pre-``rename`` names.
    Because bridge edges only point from the right graph into the left graph,
    merging two acyclic specs stays acyclic.

    ``loops`` overrides ``left.loop`` / ``right.loop`` with ``"left"`` /
    ``"right"`` keys. A looped side becomes one native ``GRegion`` node with
    pydaograph's group loop, so the whole side executes N times. Bridges
    involving a looped side attach to the wrapper node, at group granularity.

    ``rename`` is called as ``rename(side, name)`` with ``side`` either
    ``"left"`` or ``"right"``. It is required when the two graphs reuse a node
    name; the default keeps names unchanged and raises on a collision. Renaming
    a workflow step also requires updating its class attributes, see
    :func:`rebind_step_class`.

    Inputs are not mutated. ``steps_meta`` entries are merged alongside the
    nodes, and their ``dependencies`` / ``branches`` are rewritten with the same
    names. Bridge dependencies use real step ids, not wrapper node names.
    """
    bridge_map = {
        target: _dedupe(_dependency_list({"bridges": deps}, "bridges", f"bridge target {target!r}"))
        for target, deps in (bridges or {}).items()
    }
    left_names = {node["name"] for node in left.nodes}
    right_names = {node["name"] for node in right.nodes}
    for target, dependencies in bridge_map.items():
        if target not in right_names:
            raise ValueError(f"bridge target {target!r} is not a node in the right graph")
        for dependency in dependencies:
            if dependency not in left_names:
                raise ValueError(f"bridge dependency {dependency!r} is not a node in the left graph")

    loop_overrides = dict(loops or {})
    unknown_sides = set(loop_overrides) - {"left", "right"}
    if unknown_sides:
        raise ValueError(
            f"unknown loop side(s): {', '.join(map(repr, sorted(unknown_sides, key=str)))}"
        )
    left_loop = _graph_loop(loop_overrides.get("left", left.loop), "left")
    right_loop = _graph_loop(loop_overrides.get("right", right.loop), "right")
    if left_loop > 1 and not left.nodes:
        raise ValueError("left loop requires at least one node")
    if right_loop > 1 and not right.nodes:
        raise ValueError("right loop requires at least one node")

    rename = rename or (lambda _side, name: name)
    name_map: dict[tuple[str, str], str] = {}
    taken: dict[str, tuple[str, str]] = {}
    for side, spec in (("left", left), ("right", right)):
        for node in spec.nodes:
            original = node["name"]
            merged_name = rename(side, original)
            if not isinstance(merged_name, str) or not merged_name.strip():
                raise ValueError(f"rename returned an empty name for {side} node {original!r}")
            if merged_name in taken:
                other_side, other_name = taken[merged_name]
                raise ValueError(
                    f"duplicate node name {merged_name!r} after merge "
                    f"({side}:{original}, {other_side}:{other_name}); pass a rename function"
                )
            taken[merged_name] = (side, original)
            name_map[(side, original)] = merged_name

    used = set(taken)
    left_wrapper = _unique_name("__left_pipeline__", used) if left_loop > 1 else None
    if left_wrapper is not None:
        used.add(left_wrapper)
    right_wrapper = _unique_name("__right_pipeline__", used) if right_loop > 1 else None
    if right_wrapper is not None:
        used.add(right_wrapper)

    def mapped(side: str, name: str) -> str:
        return name_map[(side, name)]

    def renamed_step(side: str, name: str) -> str:
        # Metadata may reference a step nested inside a wrapper and therefore
        # absent from the top-level node map.
        return name_map.get((side, name), rename(side, name))

    def left_dependency(name: str) -> str:
        return left_wrapper if left_wrapper is not None else mapped("left", name)

    bridge_meta_dependencies = {
        target: _dedupe(mapped("left", dep) for dep in dependencies)
        for target, dependencies in bridge_map.items()
    }
    all_bridge_meta_dependencies = _dedupe(
        dep for dependencies in bridge_meta_dependencies.values() for dep in dependencies
    )
    right_wrapper_dependencies = (
        _dedupe(
            left_dependency(dep)
            for dependencies in bridge_map.values()
            for dep in dependencies
        )
        if right_wrapper is not None
        else []
    )

    merged_nodes: list[dict[str, Any]] = []
    merged_classes: dict[str, NodeFactory] = {}
    hidden_steps: set[str] = set()

    for side, spec, loop, wrapper in (
        ("left", left, left_loop, left_wrapper),
        ("right", right, right_loop, right_wrapper),
    ):
        emitted_names = {node["name"]: mapped(side, node["name"]) for node in spec.nodes}

        if loop > 1:
            assert wrapper is not None
            merged_classes[wrapper] = _region_factory(spec, emitted_names)
            wrapper_node: dict[str, Any] = {
                "name": wrapper,
                "meta_type": "GRegion",
                "loop": loop,
            }
            if side == "right" and right_wrapper_dependencies:
                wrapper_node["depends"] = right_wrapper_dependencies
            merged_nodes.append(wrapper_node)
            hidden_steps.update(emitted_names.values())
            hidden_steps.update(renamed_step(side, step) for step in spec.hidden_steps)
            continue

        for node in spec.nodes:
            original = node["name"]
            merged_name = mapped(side, original)
            merged_node = {key: value for key, value in node.items() if key != "depends"}
            merged_node["name"] = merged_name
            dependencies = _dedupe(
                [mapped(side, dep) for dep in _dependency_list(node, "depends", original)]
            )
            if side == "right":
                dependencies = _dedupe(
                    [*dependencies, *(left_dependency(dep) for dep in bridge_map.get(original, ()))]
                )
            if dependencies or "depends" in node:
                merged_node["depends"] = dependencies
            merged_nodes.append(merged_node)
            merged_classes[merged_name] = spec.classes[original]

        hidden_steps.update(renamed_step(side, step) for step in spec.hidden_steps)

    merged_meta: list[dict[str, Any]] = []
    for side, spec in (("left", left), ("right", right)):
        for entry in spec.steps_meta:
            original = entry["id"]
            merged_entry = {
                key: value for key, value in entry.items() if key not in ("id", *_META_REFERENCE_FIELDS)
            }
            merged_entry["id"] = renamed_step(side, original)
            dependencies = _dedupe(
                [renamed_step(side, dep) for dep in _dependency_list(entry, "dependencies", original)]
            )
            if side == "right":
                if right_loop > 1:
                    dependencies = _dedupe([*dependencies, *all_bridge_meta_dependencies])
                else:
                    dependencies = _dedupe(
                        [*dependencies, *bridge_meta_dependencies.get(original, ())]
                    )
            if dependencies or "dependencies" in entry:
                merged_entry["dependencies"] = dependencies
            if "branches" in entry:
                merged_entry["branches"] = [
                    renamed_step(side, branch)
                    for branch in _dependency_list(entry, "branches", original)
                ]
            merged_meta.append(merged_entry)

    return GraphSpec(
        nodes=merged_nodes,
        classes=merged_classes,
        steps_meta=merged_meta,
        loop=1,
        hidden_steps=hidden_steps,
    )


def rebind_step_class(
    cls: type[Any],
    *,
    step_id: str | None = None,
    dependencies: Sequence[str] | None = None,
) -> type[Any]:
    """Return a subclass with a new ``STEP_ID`` and/or ``DEPENDENCIES``.

    ``merge_graphs`` rewrites the native graph and ``steps_meta``, but a
    ``WorkflowStepNode`` subclass reads ``STEP_ID`` / ``DEPENDENCIES`` from its
    class when it reports completion, checks ``param.can_run(...)``, and builds
    ``step_meta()``. Use this helper when a merge renames a step or adds a
    bridge dependency that must be visible to those code paths.
    """
    attributes: dict[str, Any] = {}
    if step_id is not None:
        attributes["STEP_ID"] = step_id
    if dependencies is not None:
        attributes["DEPENDENCIES"] = list(dependencies)
    return type(cls.__name__, (cls,), attributes)


def _dependency_list(container: Mapping[str, Any], key: str, owner: str) -> list[str]:
    """Return a validated list of node names from ``container[key]``."""
    raw = container.get(key)
    if raw is None:
        return []
    if isinstance(raw, (str, bytes, bytearray)):
        raise ValueError(f"{owner} {key!r} must be a list of node names, not a string")
    if not isinstance(raw, Sequence):
        raise ValueError(f"{owner} {key!r} must be a list of node names")
    return _strings(raw, f"{owner} {key!r}")


def _strings(values: Iterable[Any], owner: str) -> list[str]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{owner} must contain non-empty string node names")
        result.append(value)
    return result


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _region_factory(spec: GraphSpec, emitted_names: Mapping[str, str]) -> NodeFactory:
    """Build one fresh ``GRegion`` containing fresh copies of ``spec``'s nodes."""

    def create() -> GElement:
        children: dict[str, GElement] = {}
        for node in spec.nodes:
            original = node["name"]
            child = spec.classes[original]()
            child.setName(emitted_names[original])
            child.setLoop(_node_loop(node))
            children[original] = child

        for node in spec.nodes:
            original = node["name"]
            dependencies = _dependency_list(node, "depends", f"node {original!r}")
            if dependencies:
                children[original].addDependGElements({children[dep] for dep in dependencies})

        return GRegion(list(children.values()))

    return create


def _node_loop(node: Mapping[str, Any]) -> int:
    return int(node.get("loop", 1))


def _graph_loop(value: Any, owner: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{owner} loop must be a positive integer")
    return value


def _unique_name(base: str, used: set[str]) -> str:
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}{index}"
        index += 1
    return candidate


def _validate_spec(spec: GraphSpec) -> None:
    _graph_loop(spec.loop, "graph")
    if spec.loop > 1 and not spec.nodes:
        raise ValueError("a looped graph needs at least one node")

    nodes: dict[str, dict[str, Any]] = {}
    for node in spec.nodes:
        if not isinstance(node, dict):
            raise ValueError("each graph node must be a dict")
        name = node.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("each graph node needs a non-empty string 'name'")
        if name in nodes:
            raise ValueError(f"duplicate graph node name: {name!r}")
        if "loop" in node:
            value = node["loop"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"node {name!r} 'loop' must be a non-negative integer")
        nodes[name] = node

    for name, node in nodes.items():
        dependencies = _dependency_list(node, "depends", f"node {name!r}")
        unknown = [dependency for dependency in dependencies if dependency not in nodes]
        if unknown:
            raise ValueError(
                f"node {name!r} depends on unknown node(s): {', '.join(map(repr, unknown))}"
            )
        if name in dependencies:
            raise ValueError(f"node {name!r} cannot depend on itself")

    for name, factory in spec.classes.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("class map keys must be non-empty node names")
        if not callable(factory):
            raise ValueError(f"class factory for graph node {name!r} is not callable")

    missing = sorted(set(nodes) - set(spec.classes))
    extra = sorted(set(spec.classes) - set(nodes))
    if missing:
        raise ValueError(f"no class factory for graph node(s): {', '.join(map(repr, missing))}")
    if extra:
        raise ValueError(f"class factory without a graph node: {', '.join(map(repr, extra))}")

    for step in spec.hidden_steps:
        if not isinstance(step, str) or not step.strip():
            raise ValueError("hidden_steps must contain non-empty step ids")

    known_steps = set(nodes) | set(spec.hidden_steps)

    meta_ids: set[str] = set()
    for entry in spec.steps_meta:
        if not isinstance(entry, dict):
            raise ValueError("each steps_meta entry must be a dict")
        step_id = entry.get("id")
        if not isinstance(step_id, str) or not step_id.strip():
            raise ValueError("each steps_meta entry needs a non-empty string 'id'")
        if step_id in meta_ids:
            raise ValueError(f"duplicate steps_meta id: {step_id!r}")
        meta_ids.add(step_id)
        if step_id not in known_steps:
            raise ValueError(
                f"steps_meta id {step_id!r} has no matching graph node or hidden step"
            )
        for key in _META_REFERENCE_FIELDS:
            references = _dependency_list(entry, key, f"steps_meta {step_id!r}")
            unknown = [reference for reference in references if reference not in known_steps]
            if unknown:
                raise ValueError(
                    f"steps_meta {step_id!r} {key!r} references unknown node(s): "
                    f"{', '.join(map(repr, unknown))}"
                )

    _validate_acyclic(nodes)


def _validate_acyclic(nodes: Mapping[str, Mapping[str, Any]]) -> None:
    dependencies = {
        name: _dependency_list(node, "depends", f"node {name!r}") for name, node in nodes.items()
    }
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            start = stack.index(name)
            cycle = [*stack[start:], name]
            raise ValueError("dependency cycle: " + " -> ".join(cycle))
        visiting.add(name)
        stack.append(name)
        for dependency in dependencies[name]:
            visit(dependency)
        stack.pop()
        visiting.remove(name)
        visited.add(name)

    for name in dependencies:
        visit(name)
