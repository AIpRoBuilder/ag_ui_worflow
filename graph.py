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

from pydaograph import GElement


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

    Construction validates unique names, known dependencies, one factory per
    node, metadata referencing real nodes, and acyclic dependencies. Use
    :meth:`from_json` to start from an existing graph JSON string.
    """

    nodes: list[dict[str, Any]]
    classes: Mapping[str, NodeFactory]
    steps_meta: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.nodes = [dict(node) for node in self.nodes]
        self.classes = dict(self.classes)
        self.steps_meta = [dict(step) for step in self.steps_meta]
        _validate_spec(self)

    @classmethod
    def from_json(
        cls,
        pipeline_json: str,
        node_classes: Mapping[str, NodeFactory],
        *,
        steps_meta: Iterable[dict[str, Any]] = (),
    ) -> GraphSpec:
        """Build a spec from ``{"nodes": [...]}`` JSON text and its class map."""
        try:
            payload = json.loads(pipeline_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"pipeline_json is not valid JSON: {exc.msg}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
            raise ValueError('pipeline_json must be a JSON object with a "nodes" array')
        return cls(nodes=payload["nodes"], classes=node_classes, steps_meta=list(steps_meta))

    @property
    def pipeline_json(self) -> str:
        """JSON text accepted by ``GPipeline.buildFromJsonStr`` and the pool."""
        return json.dumps({"nodes": self.nodes}, ensure_ascii=False)

    @property
    def node_classes(self) -> dict[str, NodeFactory]:
        """A copy of the name -> factory mapping accepted by the pool."""
        return dict(self.classes)


def merge_graphs(
    left: GraphSpec,
    right: GraphSpec,
    *,
    bridges: Mapping[str, Sequence[str]] | None = None,
    rename: Callable[[str, str], str] | None = None,
) -> GraphSpec:
    """Combine ``left`` and ``right`` into one DAG and return the merged spec.

    ``bridges`` maps a node in the right graph to nodes in the left graph it
    must wait for; both keys and values use the original, pre-``rename`` names.
    Because bridge edges only point from the right graph into the left graph,
    merging two acyclic specs stays acyclic.

    ``rename`` is called as ``rename(side, name)`` with ``side`` either
    ``"left"`` or ``"right"``. It is required when the two graphs reuse a node
    name; the default keeps names unchanged and raises on a collision. Renaming
    a workflow step also requires updating its class attributes, see
    :func:`rebind_step_class`.

    Inputs are not mutated. ``steps_meta`` entries are merged alongside the
    nodes, and their ``dependencies`` / ``branches`` are rewritten with the same
    names (bridge dependencies are added to the right-side entries).
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

    def mapped(side: str, name: str) -> str:
        return name_map[(side, name)]

    merged_nodes: list[dict[str, Any]] = []
    merged_classes: dict[str, NodeFactory] = {}
    for side, spec in (("left", left), ("right", right)):
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
                    [*dependencies, *(mapped("left", dep) for dep in bridge_map.get(original, ()))]
                )
            if dependencies or "depends" in node:
                merged_node["depends"] = dependencies
            merged_nodes.append(merged_node)
            merged_classes[merged_name] = spec.classes[original]

    merged_meta: list[dict[str, Any]] = []
    for side, spec in (("left", left), ("right", right)):
        for entry in spec.steps_meta:
            original = entry["id"]
            merged_entry = {
                key: value for key, value in entry.items() if key not in ("id", *_META_REFERENCE_FIELDS)
            }
            merged_entry["id"] = mapped(side, original)
            dependencies = _dedupe(
                [mapped(side, dep) for dep in _dependency_list(entry, "dependencies", original)]
            )
            if side == "right":
                dependencies = _dedupe(
                    [*dependencies, *(mapped("left", dep) for dep in bridge_map.get(original, ()))]
                )
            if dependencies or "dependencies" in entry:
                merged_entry["dependencies"] = dependencies
            if "branches" in entry:
                merged_entry["branches"] = [
                    mapped(side, branch) for branch in _dependency_list(entry, "branches", original)
                ]
            merged_meta.append(merged_entry)

    return GraphSpec(nodes=merged_nodes, classes=merged_classes, steps_meta=merged_meta)


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


def _validate_spec(spec: GraphSpec) -> None:
    nodes: dict[str, dict[str, Any]] = {}
    for node in spec.nodes:
        if not isinstance(node, dict):
            raise ValueError("each graph node must be a dict")
        name = node.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("each graph node needs a non-empty string 'name'")
        if name in nodes:
            raise ValueError(f"duplicate graph node name: {name!r}")
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
        if step_id not in nodes:
            raise ValueError(f"steps_meta id {step_id!r} has no matching graph node")
        for key in _META_REFERENCE_FIELDS:
            references = _dependency_list(entry, key, f"steps_meta {step_id!r}")
            unknown = [reference for reference in references if reference not in nodes]
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
