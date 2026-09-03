from collections.abc import Callable
from typing import Any
from pydaograph import GPipeline, GParam, register_class


PIPELINE_ID_GPARAM_KEY = "pipeline.id"
NODE_MAIN_UTILITY_SIGNATURE_ATTR = "__ag_ui_node_main_utility_signature__"
NODE_SUBCLASS_IMPLEMENTATION_SIGNATURE_ATTR = "__ag_ui_node_subclass_implementation_signature__"


@register_class
class PipelineIdParam(GParam):
    """Shared pipeline identifier stored as a PyDaoGraph ``GParam``."""

    def __init__(self, value: str = "") -> None:
        super().__init__()
        self.value = str(value)


def set_pipeline_id(
    pipeline: GPipeline,
    pipeline_id: str,
    key: str = PIPELINE_ID_GPARAM_KEY,
):
    """Attach a pipeline identifier that every node can read at runtime.

    The identifier is stored as a pipeline-scoped ``GParam``. Nodes can then
    access it during ``run()`` with ``self.getGParam(key)`` or the convenience
    helper :func:`get_pipeline_id`.
    """

    return pipeline.createGParam(PipelineIdParam(pipeline_id), key)


def get_pipeline_id(owner: Any, key: str = PIPELINE_ID_GPARAM_KEY) -> str | None:
    """Read the shared pipeline identifier from a pipeline or node object."""

    if not hasattr(owner, "hasGParam") or not owner.hasGParam(key):
        return None

    param = owner.getGParam(key)
    value = getattr(param, "value", None)
    if value is None:
        return None
    text = str(value)
    return text if text else None


def node_main_utility(func: Callable[..., Any]) -> Callable[..., Any]:
    """Mark a node method as the class main utility function.

    AST tools can identify this decorator and runtime code can read the marker
    via :func:`get_node_main_utility_signature`.
    """

    setattr(func, NODE_MAIN_UTILITY_SIGNATURE_ATTR, func.__name__)
    return func


def node_subclass_implementation(func: Callable[..., Any]) -> Callable[..., Any]:
    """Mark a node method as a subclass override or implementation hook."""

    setattr(func, NODE_SUBCLASS_IMPLEMENTATION_SIGNATURE_ATTR, func.__name__)
    return func


def _get_marked_node_method_names(node_or_class: Any, marker_attr: str) -> list[str]:
    cls = node_or_class if isinstance(node_or_class, type) else node_or_class.__class__
    method_names: list[str] = []
    seen: set[str] = set()
    for base in cls.__mro__:
        for attr in base.__dict__.values():
            marker = getattr(attr, marker_attr, None)
            if isinstance(marker, str) and marker and marker not in seen:
                method_names.append(marker)
                seen.add(marker)
    return method_names


def get_node_main_utility_signature(node_or_class: Any) -> str | None:
    """Return the marked main utility method name for a node class/instance."""

    method_names = _get_marked_node_method_names(
        node_or_class,
        NODE_MAIN_UTILITY_SIGNATURE_ATTR,
    )
    return method_names[0] if method_names else None


def get_node_subclass_implementation_signatures(node_or_class: Any) -> list[str]:
    """Return marked subclass override points for a node class/instance."""

    return _get_marked_node_method_names(
        node_or_class,
        NODE_SUBCLASS_IMPLEMENTATION_SIGNATURE_ATTR,
    )