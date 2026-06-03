from __future__ import annotations

import ast
import base64
import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

from meta_agent.tools.file_tools import parse_skill_md, extract_skill_commands

from ..services import workflow_service_registry
from ..session import get_node_workflow_session

try:  # Optional dependency for OpenAI-compatible chat providers
    from openai import OpenAI
except Exception:  # pragma: no cover - handled at runtime
    OpenAI = None  # type: ignore


_UPLOAD_DIR = Path(tempfile.gettempdir()) / "meta_agent_uploads"


def _safe_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _decode_bytes_string(value: Any) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if not isinstance(value, str):
        return None

    stripped = value.strip()
    if not stripped:
        return None

    if stripped.startswith("data:") and "base64," in stripped:
        _, _, b64_part = stripped.partition("base64,")
        try:
            return base64.b64decode(b64_part)
        except Exception:
            return None

    try:
        parsed = ast.literal_eval(stripped)
    except Exception:
        return None

    if isinstance(parsed, bytes):
        return parsed
    if isinstance(parsed, bytearray):
        return bytes(parsed)
    return None


def _materialize_upload_to_path(payload: dict[str, Any]) -> str | None:
    file_name = _safe_string(payload.get("fileName") or payload.get("filename") or "uploaded_input")
    content_b64 = payload.get("fileContentBase64") or payload.get("contentBase64")
    content_text = payload.get("fileContent") or payload.get("content")
    content_bytes = payload.get("fileBytes") or payload.get("file_bytes")

    data: bytes | None = None
    if isinstance(content_b64, str) and content_b64.strip():
        try:
            data = base64.b64decode(content_b64)
        except Exception:
            data = None

    if data is None:
        data = _decode_bytes_string(content_bytes)

    if data is None and isinstance(content_text, str):
        data = _decode_bytes_string(content_text)

    if data is None and isinstance(content_text, str):
        data = content_text.encode("utf-8")

    if data is None:
        return None

    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file_name).suffix
    target = _UPLOAD_DIR / f"upload_{uuid.uuid4().hex}{suffix}"
    target.write_bytes(data)
    return str(target)


def _normalize_step_input(raw_input: Any) -> str:
    if isinstance(raw_input, str):
        stripped = raw_input.strip()
        if not stripped:
            return ""
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
    else:
        parsed = raw_input

    if isinstance(parsed, dict):
        uploaded_path = _materialize_upload_to_path(parsed)
        if uploaded_path:
            return uploaded_path

        for key in ("filePath", "file_path", "path"):
            if key in parsed:
                return _safe_string(parsed.get(key))

        for key in ("input", "value", "text"):
            if key in parsed:
                return _safe_string(parsed.get(key))

        return json.dumps(parsed, ensure_ascii=False)

    if isinstance(parsed, list):
        return json.dumps(parsed, ensure_ascii=False)

    return _safe_string(parsed)


def _get_step_output_derived_keys(owner: Any, step_id: str) -> list[str]:
    try:
        session = get_node_workflow_session(owner)
    except RuntimeError:
        return []

    output = session.step_outputs.get(step_id)
    if output is None or not isinstance(output.derived, dict):
        return []
    return list(output.derived.keys())


def _workflow_root_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_service_md_path(service_name: str) -> Path | None:
    raw_name = _safe_string(service_name)
    if not raw_name:
        return None

    candidate_paths: list[Path] = []
    raw_path = Path(raw_name)
    repo_root = _workflow_root_dir()

    if raw_path.suffix.lower() == ".md":
        candidate_paths.extend(
            [
                raw_path,
                repo_root / raw_path,
            ]
        )
    else:
        candidate_paths.extend(
            [
                raw_path / "service.md",
                repo_root / raw_path / "service.md",
                repo_root / "agent_services" / raw_path / "service.md",
            ]
        )

    for candidate in candidate_paths:
        try:
            resolved = candidate.expanduser().resolve()
        except Exception:
            continue
        if resolved.exists() and resolved.is_file():
            return resolved
    return None


def _collect_declared_services_for_step(step_id: str, session_state: dict[str, Any]) -> list[dict[str, str]]:
    meta_map = session_state.get("_workflow_step_meta_map")
    if not isinstance(meta_map, dict):
        return []
    step_meta = meta_map.get(step_id)
    if not isinstance(step_meta, dict):
        return []
    services = step_meta.get("services") or []
    if not isinstance(services, list):
        return []

    normalized: list[dict[str, str]] = []
    for item in services:
        if not isinstance(item, dict):
            continue
        service_name = _safe_string(item.get("service_name"))
        if not service_name:
            continue
        normalized.append(
            {
                "service_name": service_name,
                "use_desc": _safe_string(item.get("use_desc")),
            }
        )
    return normalized


def _resolve_service_usages_for_step(step_id: str, session_state: dict[str, Any]) -> list[dict[str, Any]]:
    service_defs = _collect_declared_services_for_step(step_id, session_state)
    if not service_defs:
        return []

    usages: list[dict[str, Any]] = []
    for item in service_defs:
        service_name = item["service_name"]
        use_desc = item["use_desc"]

        record = workflow_service_registry.require_running(service_name)

        service_md_path = _resolve_service_md_path(service_name)
        if service_md_path is None:
            raise FileNotFoundError(
                f"service '{service_name}' is running but service.md was not found. "
                f"Expected '{service_name}/service.md' or 'agent_services/{service_name}/service.md'."
            )

        service_md_text = service_md_path.read_text(encoding="utf-8")
        sections = parse_skill_md(service_md_text)
        using_text = _safe_string(sections.get("Using"))

        usages.append(
            {
                "service_name": service_name,
                "use_desc": use_desc,
                "service_md_path": str(service_md_path),
                "service_using": using_text,
                "pid": record.pid,
                "status": record.status,
            }
        )

    return usages