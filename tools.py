from typing import Any
from pydaograph import GPipeline, GParam, register_class


PIPELINE_ID_GPARAM_KEY = "pipeline.id"


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