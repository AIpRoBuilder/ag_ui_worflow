from __future__ import annotations

import ast
import base64
import inspect
import json
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any
import importlib
import sys
from pathlib import Path
import tempfile
from functools import lru_cache
import inspect

from ..services import workflow_service_registry
from ..session import get_node_workflow_session


_UPLOAD_DIR = Path(tempfile.gettempdir()) / "meta_agent_uploads"

_NODE_DESCRIPTOR_SECTIONS = {
    "Structure": "structure",
    "Function": "function",
    "Implementation Guide": "implementation_guide",
    "Example": "example",
}

def parse_skill_md(text: str) -> dict[str, str]:
    """Parse a markdown document into a dict keyed by H2 section name.

    Only level-2 headings (``## Heading``) are used as section boundaries.
    The title (H1) is stored under the key ``"_title"``.
    """
    sections: dict[str, str] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n").rstrip("\r")
        if stripped.startswith("## "):
            if current_key is not None:
                sections[current_key] = "".join(current_lines).strip()
            current_key = stripped[3:].strip()
            current_lines = []
        elif stripped.startswith("# ") and current_key is None:
            # H1 title – store separately
            sections["_title"] = stripped[2:].strip()
        else:
            if current_key is not None:
                current_lines.append(line)

    if current_key is not None:
        sections[current_key] = "".join(current_lines).strip()

    return sections


def extract_skill_commands(section_text: str) -> list[str]:
    commands: list[str] = []
    in_block = False

    for line in section_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_block = not in_block
            continue
        if not in_block:
            continue

        command = stripped.lstrip("$ ").strip()
        if command:
            commands.append(command)

    return commands


def resolve_package_root(current_file: str) -> Path:
    default_package_root = Path(current_file).resolve().parents[1]
    ag_spec = importlib.util.find_spec("ag_ui_workflow")
    if ag_spec and ag_spec.origin:
        return Path(ag_spec.origin).resolve().parent
    return default_package_root


def ensure_package_parent_on_sys_path(root_dir: Path) -> Path:
    parent_dir = str(root_dir.parent)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    return root_dir


def bootstrap_package_root(current_file: str) -> Path:
    return ensure_package_parent_on_sys_path(resolve_package_root(current_file))

@lru_cache(maxsize=None)
def _read_node_descriptor_prompt(descriptor_path: str) -> str:
    path = Path(descriptor_path)
    if not path.exists():
        raise FileNotFoundError(f"descriptor prompt not found at {path}")
    return path.read_text(encoding="utf-8").strip()


def _resolve_node_descriptor_path(owner: Any, descriptor_prompt_file: str = "descriptor_prompt.md") -> Path:
    descriptor_path = Path(descriptor_prompt_file)
    if descriptor_path.is_absolute():
        candidate_paths = [descriptor_path]
    else:
        cls = owner if isinstance(owner, type) else owner.__class__
        candidate_paths: list[Path] = []
        for mro_cls in cls.__mro__:
            try:
                module_file = Path(inspect.getfile(mro_cls)).resolve()
            except (TypeError, OSError):
                continue

            candidate = module_file.parent / descriptor_path
            if candidate not in candidate_paths:
                candidate_paths.append(candidate)

        if not candidate_paths:
            candidate_paths = [descriptor_path]

    resolved_path = next((candidate for candidate in candidate_paths if candidate.exists()), candidate_paths[-1])
    if not resolved_path.exists():
        raise FileNotFoundError(f"descriptor prompt not found at {resolved_path}")
    return resolved_path


def _load_node_descriptor_prompt(owner: Any, descriptor_prompt_file: str = "descriptor_prompt.md") -> str:
    descriptor_path = _resolve_node_descriptor_path(owner, descriptor_prompt_file)
    return _read_node_descriptor_prompt(str(descriptor_path))


def _load_node_descriptor_sections(owner: Any, descriptor_prompt_file: str = "descriptor_prompt.md") -> dict[str, str]:
    sections = parse_skill_md(_load_node_descriptor_prompt(owner, descriptor_prompt_file))
    return {
        mapped_key: sections.get(section_name, "").strip()
        for section_name, mapped_key in _NODE_DESCRIPTOR_SECTIONS.items()
    }


def _apply_node_descriptor_attributes(owner: Any, descriptor_prompt_file: str = "descriptor_prompt.md") -> dict[str, str]:
    descriptor_text = _load_node_descriptor_prompt(owner, descriptor_prompt_file)
    sections = _load_node_descriptor_sections(owner, descriptor_prompt_file)
    setattr(owner, "meta_description", descriptor_text)
    for attr_name, value in sections.items():
        setattr(owner, attr_name, value)
    return sections


def _build_node_descriptor_meta(owner: Any, descriptor_prompt_file: str = "descriptor_prompt.md") -> dict[str, str]:
    sections = _load_node_descriptor_sections(owner, descriptor_prompt_file)
    return {
        "metaDescription": _load_node_descriptor_prompt(owner, descriptor_prompt_file),
        "structure": sections["structure"],
        "function": sections["function"],
        "implementationGuide": sections["implementation_guide"],
        "example": sections["example"],
    }

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
