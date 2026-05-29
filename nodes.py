from pydaograph import GNode, CStatus
import asyncio
import ast
import base64
from datetime import timedelta
import json
import mimetypes
import os
from pathlib import Path
import inspect
import tempfile
from urllib.request import Request, urlopen
import uuid
from typing import Any
from .types import StepRunOutput
from .session import get_bound_workflow_session
from .services import workflow_service_registry
from meta_agent.tools.file_tools import parse_skill_md, extract_skill_commands

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


def _get_step_output_derived_keys(step_id: str) -> list[str]:
    try:
        session = get_bound_workflow_session()
    except RuntimeError:
        return []

    output = session.step_outputs.get(step_id)
    if output is None or not isinstance(output.derived, dict):
        return []
    return list(output.derived.keys())


def _workflow_root_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent


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


class WorkflowStepNode(GNode):
    STEP_ID = ""
    TITLE = ""
    PROMPT = ""
    DEPENDENCIES: list[str] = []
    SERVICES: list[dict[str, str]] = []
    INPUT_REQUIRED = True
    NODE_KIND = "input"

    def __init__(self) -> None:
        super().__init__()
        self.setName(self.STEP_ID)
        self.setWaitForInput(False)
        self.setInputPrompt(self.PROMPT)
        self.setInputHandler(self._input_handler)

    def _input_handler(self, user_input: str) -> CStatus:
        session = get_bound_workflow_session()
        session.pending_inputs[self.STEP_ID] = user_input
        return CStatus()

    def run(self) -> CStatus:
        session = get_bound_workflow_session()
        self._set_state("running")
        raw_input = _normalize_step_input(session.pending_inputs.get(self.STEP_ID, ""))
        if not raw_input:
            self._set_state("awaiting_input")
            return CStatus(1003, f"input required for step {self.STEP_ID}")

        dependency_results = {
            dep: session.step_outputs[dep]
            for dep in self.DEPENDENCIES
            if dep in session.step_outputs
        }
        try:
            self.use_service(session.state)
            output = self.process_input(
                raw_input,
                dependency_results,
                session.state,
            )
            if not isinstance(output, StepRunOutput):
                self._set_state("failed")
                return CStatus(1001, f"step {self.STEP_ID} failed: process_input must return StepRunOutput")
        except Exception as exc:  # pragma: no cover
            self._set_state("failed")
            return CStatus(1001, f"step {self.STEP_ID} failed: {exc}")

        session.step_outputs[self.STEP_ID] = output
        session.step_cards[self.STEP_ID] = self.card_payload(output)
        self._set_state("completed")
        callback = session.submit_callbacks.pop(self.STEP_ID, None)
        if callback is not None:
            callback(output)
        session.pending_inputs.pop(self.STEP_ID, None)
        return CStatus()

    def _set_state(self, state: str) -> None:
        session = get_bound_workflow_session()
        session.step_states[self.STEP_ID] = state

    def card_payload(self, output: StepRunOutput) -> dict[str, Any]:
        return output.card

    def get_derived_keys(self) -> list[str]:
        return _get_step_output_derived_keys(self.STEP_ID)

    def use_service(self, session_state: dict[str, Any]) -> list[dict[str, Any]]:
        meta_map = session_state.get("_workflow_step_meta_map")
        if not isinstance(meta_map, dict):
            meta_map = {}
            session_state["_workflow_step_meta_map"] = meta_map
        if self.STEP_ID not in meta_map:
            meta_map[self.STEP_ID] = self.step_meta()

        usages = _resolve_service_usages_for_step(self.STEP_ID, session_state)
        if usages:
            service_usage_map = session_state.setdefault("service_usage", {})
            if isinstance(service_usage_map, dict):
                service_usage_map[self.STEP_ID] = usages
        return usages

    def process_input(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        raise NotImplementedError()

    def clone(self):
        return self

    @classmethod
    def step_meta(cls) -> dict[str, Any]:
        return {
            "id": cls.STEP_ID,
            "title": cls.TITLE,
            "prompt": cls.PROMPT,
            "dependencies": list(cls.DEPENDENCIES),
            "services": list(cls.SERVICES),
            "inputRequired": cls.INPUT_REQUIRED,
            "nodeKind": cls.NODE_KIND,
        }


class WorkflowFileNode(GNode):
    INPUT_REQUIRED = True
    STEP_ID = ""
    TITLE = ""
    PROMPT = ""
    DEPENDENCIES: list[str] = []
    NODE_KIND = "file"

    STORAGE_BACKEND_ENV = "META_AGENT_FILE_STORAGE_BACKEND"
    STORAGE_DIR_ENV = "META_AGENT_FILE_STORAGE_DIR"
    DEFAULT_STORAGE_BACKEND = "local"
    DEFAULT_STORAGE_DIR = Path(tempfile.gettempdir()) / "meta_agent_file_storage"

    def __init__(self) -> None:
        super().__init__()
        self.setName(self.STEP_ID)
        self.setWaitForInput(False)
        self.setInputPrompt(self.PROMPT)
        self.setInputHandler(self._input_handler)

    def _input_handler(self, user_input: str) -> CStatus:
        session = get_bound_workflow_session()
        session.pending_inputs[self.STEP_ID] = user_input
        return CStatus()

    def run(self) -> CStatus:
        session = get_bound_workflow_session()
        self._set_state("running")

        files, storage_override = self._parse_file_input(session.pending_inputs.get(self.STEP_ID, ""))
        if self.INPUT_REQUIRED and not files:
            self._set_state("awaiting_input")
            return CStatus(1003, f"input required for step {self.STEP_ID}")

        dependency_results = {
            dep: session.step_outputs[dep]
            for dep in self.DEPENDENCIES
            if dep in session.step_outputs
        }

        try:
            saved_files = self.save_files(files, session.state, storage_override)
            output = self.build_step_output(saved_files)
            if not isinstance(output, StepRunOutput):
                self._set_state("failed")
                return CStatus(1001, f"step {self.STEP_ID} failed: build_step_output must return StepRunOutput")
        except Exception as exc:  # pragma: no cover
            self._set_state("failed")
            return CStatus(1001, f"step {self.STEP_ID} failed: {exc}")

        session.step_outputs[self.STEP_ID] = output
        session.step_cards[self.STEP_ID] = self.card_payload(output)
        self._set_state("completed")
        callback = session.submit_callbacks.pop(self.STEP_ID, None)
        if callback is not None:
            callback(output)
        session.pending_inputs.pop(self.STEP_ID, None)
        return CStatus()

    def _set_state(self, state: str) -> None:
        session = get_bound_workflow_session()
        session.step_states[self.STEP_ID] = state

    def card_payload(self, output: StepRunOutput) -> dict[str, Any]:
        return output.card

    def get_derived_keys(self) -> list[str]:
        return _get_step_output_derived_keys(self.STEP_ID)

    def _parse_file_input(self, raw_input: Any) -> tuple[list[dict[str, Any]], str | None]:
        parsed: Any = raw_input
        if isinstance(raw_input, str):
            stripped = raw_input.strip()
            if not stripped:
                return [], None
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = stripped

        files: list[dict[str, Any]] = []
        storage_override: str | None = None

        def _append_file(candidate: Any) -> None:
            file_item = self._extract_file_item(candidate)
            if file_item is not None:
                files.append(file_item)

        if isinstance(parsed, dict):
            storage_override = _safe_string(
                parsed.get("storagePath")
                or parsed.get("storage_path")
                or parsed.get("targetDir")
                or parsed.get("target_dir")
                or parsed.get("saveDir")
                or parsed.get("save_dir")
                or parsed.get("location")
            ) or None

            for key in ("files", "fileList", "uploads", "items"):
                values = parsed.get(key)
                if isinstance(values, list):
                    for item in values:
                        _append_file(item)

            if not files:
                _append_file(parsed)

        elif isinstance(parsed, list):
            for item in parsed:
                _append_file(item)
        else:
            _append_file(parsed)

        return files, storage_override

    def _extract_file_item(self, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None

        if isinstance(value, dict):
            file_name = _safe_string(
                value.get("fileName")
                or value.get("filename")
                or value.get("name")
                or value.get("originalName")
                or "uploaded_file"
            )

            data: bytes | None = None

            content_b64 = value.get("fileContentBase64") or value.get("contentBase64")
            if isinstance(content_b64, str) and content_b64.strip():
                try:
                    data = base64.b64decode(content_b64)
                except Exception:
                    data = None

            if data is None:
                data = _decode_bytes_string(value.get("fileBytes") or value.get("file_bytes"))

            if data is None:
                content_text = value.get("fileContent") or value.get("content")
                data = _decode_bytes_string(content_text)

            if data is None and isinstance(value.get("path"), str):
                candidate = Path(_safe_string(value.get("path")))
                if candidate.exists() and candidate.is_file():
                    data = candidate.read_bytes()
                    if not file_name or file_name == "uploaded_file":
                        file_name = candidate.name

            if data is None:
                return None

            return {
                "fileName": Path(file_name).name or "uploaded_file",
                "bytes": data,
            }

        if isinstance(value, bytes):
            return {"fileName": "uploaded_file", "bytes": value}

        if isinstance(value, bytearray):
            return {"fileName": "uploaded_file", "bytes": bytes(value)}

        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None

            parsed_data = _decode_bytes_string(raw)
            if parsed_data is not None:
                return {"fileName": "uploaded_file", "bytes": parsed_data}

            try:
                decoded_b64 = base64.b64decode(raw, validate=True)
                return {"fileName": "uploaded_file", "bytes": decoded_b64}
            except Exception:
                return None

        return None

    def _resolve_storage_backend(self, session_state: dict[str, Any]) -> str:
        state_backend = _safe_string(session_state.get("fileStorageBackend") or session_state.get("storageBackend"))
        env_backend = _safe_string(os.getenv(self.STORAGE_BACKEND_ENV, self.DEFAULT_STORAGE_BACKEND))
        backend = (state_backend or env_backend or self.DEFAULT_STORAGE_BACKEND).lower()
        return backend if backend in {"local", "remote"} else self.DEFAULT_STORAGE_BACKEND

    def _resolve_storage_dir(self, session_state: dict[str, Any], storage_override: str | None) -> Path:
        if storage_override:
            return Path(storage_override).expanduser().resolve()

        state_dir = _safe_string(session_state.get("fileStorageDir") or session_state.get("storageDir"))
        if state_dir:
            return Path(state_dir).expanduser().resolve()

        env_dir = _safe_string(os.getenv(self.STORAGE_DIR_ENV, ""))
        if env_dir:
            return Path(env_dir).expanduser().resolve()

        return self.DEFAULT_STORAGE_DIR.resolve()

    def save_files(
        self,
        files: list[dict[str, Any]],
        session_state: dict[str, Any],
        storage_override: str | None = None,
    ) -> list[dict[str, Any]]:
        if storage_override:
            session_state["_workflow_file_storage_override"] = storage_override
        else:
            session_state.pop("_workflow_file_storage_override", None)
        try:
            return self.save_files_remote(files, session_state)
        finally:
            session_state.pop("_workflow_file_storage_override", None)

    def save_files_remote(
        self,
        files: list[dict[str, Any]],
        session_state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        backend = self._resolve_storage_backend(session_state)
        storage_override = _safe_string(session_state.get("_workflow_file_storage_override")) or None
        if backend != "remote":
            storage_dir = self._resolve_storage_dir(session_state, storage_override)
            storage_dir.mkdir(parents=True, exist_ok=True)

            saved: list[dict[str, Any]] = []
            for item in files:
                file_name = Path(_safe_string(item.get("fileName")) or "uploaded_file").name or "uploaded_file"
                data = item.get("bytes")
                if not isinstance(data, bytes):
                    continue

                target_path = storage_dir / file_name
                target_path.write_bytes(data)
                saved.append(
                    {
                        "fileName": file_name,
                        "storage": "local",
                        "location": str(target_path),
                        "byteSize": len(data),
                    }
                )

            return saved

        remote_uploader = session_state.get("fileRemoteUploader")
        if not callable(remote_uploader):
            raise ValueError(
                "remote file storage requested but session_state['fileRemoteUploader'] is not callable"
            )

        saved: list[dict[str, Any]] = []
        for item in files:
            file_name = Path(_safe_string(item.get("fileName")) or "uploaded_file").name or "uploaded_file"
            data = item.get("bytes")
            if not isinstance(data, bytes):
                continue

            location = _safe_string(remote_uploader(file_name, data, session_state))
            if not location:
                raise ValueError(f"remote uploader returned empty location for file {file_name}")

            saved.append(
                {
                    "fileName": file_name,
                    "storage": "remote",
                    "location": location,
                    "byteSize": len(data),
                }
            )

        return saved

    def build_step_output(self, saved_files: list[dict[str, Any]]) -> StepRunOutput:
        locations = [
            _safe_string(item.get("location"))
            for item in saved_files
            if _safe_string(item.get("location"))
        ]
        file_names = [
            _safe_string(item.get("fileName"))
            for item in saved_files
            if _safe_string(item.get("fileName"))
        ]
        summary = f"Saved {len(saved_files)} file(s)."
        card = {
            "fileCount": len(saved_files),
            "files": file_names,
            "locations": locations,
        }
        derived = {
            "savedFiles": file_names,
            "savedLocations": locations,
            "fileCount": len(saved_files),
        }
        return StepRunOutput(summary=summary, card=card, derived=derived)

    def clone(self):
        return self

    @classmethod
    def step_meta(cls) -> dict[str, Any]:
        return {
            "id": cls.STEP_ID,
            "title": cls.TITLE,
            "prompt": cls.PROMPT,
            "dependencies": list(cls.DEPENDENCIES),
            "inputRequired": cls.INPUT_REQUIRED,
            "nodeKind": cls.NODE_KIND,
        }


class WorkflowSkillNode(GNode):
    """A workflow node backed by a skill defined in a skill.md file.

    Subclasses set ``SKILL_DIR`` to the directory containing ``skill.md`` (or
    ``SKILL_MD_PATH`` to point directly at the file).  On instantiation the
    skill's ``## Installation`` block is executed so that the required packages
    are available.  Subclasses must override :meth:`process_operation` to call the
    skill and return a :class:`StepRunOutput`.

    Parsed sections from the skill document are exposed as:
      - ``self.skill_description`` – text under ``## Description``
      - ``self.skill_install_commands`` – list of shell command strings
      - ``self.skill_using`` – text under ``## Using``
      - ``self.skill_examples`` – text under ``## Examples``
    """

    STEP_ID: str = ""
    TITLE: str = ""
    PROMPT: str = ""
    DEPENDENCIES: list[str] = []
    INPUT_REQUIRED: bool = True
    NODE_KIND: str = "skill"

    # Path to the directory that contains skill.md, or to skill.md itself.
    # Subclasses should set one of these.
    SKILL_DIR: str = ""
    SKILL_MD_PATH: str = ""

    # ---------------------------------------------------------------------------
    # Initialisation
    # ---------------------------------------------------------------------------

    INSTALL_TIMEOUT: int = 240  # seconds to wait for background installation

    def __init__(self) -> None:
        import threading
        super().__init__()
        self.setName(self.STEP_ID)
        self.setWaitForInput(False)
        self.setInputPrompt(self.PROMPT)
        self.setInputHandler(self._input_handler)

        # Resolve the path to skill.md
        skill_md_path = self._resolve_skill_md_path()

        # Parse the document
        skill_md_text = Path(skill_md_path).read_text(encoding="utf-8")
        sections = parse_skill_md(skill_md_text)

        self.skill_description: str = sections.get("Description", "")
        self.skill_install_commands: list[str] = extract_skill_commands(
            sections.get("Installation", "")
        )
        self.skill_using: str = sections.get("Using", "")
        self.skill_examples: str = sections.get("Examples", "")
        self._install_errors: list[str] = []

        # Run installation commands in a background thread so __init__ is non-blocking.
        self._install_thread: threading.Thread = threading.Thread(
            target=self._install_packages,
            daemon=True,
            name=f"skill-install-{self.STEP_ID}",
        )
        self._install_thread.start()

    def _input_handler(self, user_input: str) -> CStatus:
        session = get_bound_workflow_session()
        session.pending_inputs[self.STEP_ID] = user_input
        return CStatus()

    # ---------------------------------------------------------------------------
    # Path resolution
    # ---------------------------------------------------------------------------

    def _resolve_skill_md_path(self) -> str:
        """Return the absolute path to skill.md for this node."""
        if self.SKILL_MD_PATH:
            p = Path(self.SKILL_MD_PATH)
            if not p.is_absolute():
                p = Path(__file__).parent.parent.parent / p
            if not p.exists():
                raise FileNotFoundError(f"skill.md not found at {p}")
            return str(p)

        if self.SKILL_DIR:
            p = Path(self.SKILL_DIR)
            if not p.is_absolute():
                p = Path(__file__).parent.parent.parent / p
            candidate = p / "skill.md"
            if not candidate.exists():
                raise FileNotFoundError(f"skill.md not found in {p}")
            return str(candidate)

        raise ValueError(
            f"{self.__class__.__name__} must set SKILL_DIR or SKILL_MD_PATH"
        )

    # ---------------------------------------------------------------------------
    # Package installation
    # ---------------------------------------------------------------------------

    def _install_packages(self) -> None:
        """Run each installation command captured from ## Installation (runs in background thread)."""
        import subprocess
        import warnings

        for cmd in self.skill_install_commands:
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    msg = (
                        f"WorkflowSkillNode installation command returned "
                        f"non-zero exit code {result.returncode}: {cmd!r}\n"
                        f"stderr: {result.stderr.strip()}"
                    )
                    self._install_errors.append(msg)
                    warnings.warn(msg, stacklevel=2)
            except Exception as exc:
                msg = f"WorkflowSkillNode installation command failed: {cmd!r}: {exc}"
                self._install_errors.append(msg)
                warnings.warn(msg, stacklevel=2)

    def _wait_for_installation(self) -> CStatus:
        """Block until the background install thread finishes or INSTALL_TIMEOUT elapses."""
        self._install_thread.join(timeout=self.INSTALL_TIMEOUT)
        if self._install_thread.is_alive():
            return CStatus(
                1001,
                f"step {self.STEP_ID} failed: package installation timed out after {self.INSTALL_TIMEOUT}s",
            )
        if self._install_errors:
            return CStatus(
                1001,
                f"step {self.STEP_ID} failed: package installation errors: {'; '.join(self._install_errors)}",
            )
        return CStatus()

    def run(self) -> CStatus:
        session = get_bound_workflow_session()
        self._set_state("running")
        raw_input = _normalize_step_input(session.pending_inputs.get(self.STEP_ID, ""))
        if self.INPUT_REQUIRED and not raw_input:
            self._set_state("awaiting_input")
            return CStatus(1003, f"input required for step {self.STEP_ID}")

        # Wait for background package installation before executing the skill.
        install_status = self._wait_for_installation()
        if install_status.isErr():
            self._set_state("failed")
            return install_status

        dependency_results = {
            dep: session.step_outputs[dep]
            for dep in self.DEPENDENCIES
            if dep in session.step_outputs
        }
        try:
            params = inspect.signature(self.process_operation).parameters.values()
            accepts_user_input = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params) or len(list(params)) >= 3
            if accepts_user_input:
                output = self.process_operation(
                    raw_input,
                    dependency_results,
                    session.state,
                )
            else:
                output = self.process_operation(
                    dependency_results,
                    session.state,
                )
            if not isinstance(output, StepRunOutput):
                self._set_state("failed")
                return CStatus(
                    1001,
                    f"step {self.STEP_ID} failed: process_operation must return StepRunOutput",
                )
        except Exception as exc:
            self._set_state("failed")
            return CStatus(1001, f"step {self.STEP_ID} failed: {exc}")

        session.step_outputs[self.STEP_ID] = output
        session.step_cards[self.STEP_ID] = self.card_payload(output)
        self._set_state("completed")
        callback = session.submit_callbacks.pop(self.STEP_ID, None)
        if callback is not None:
            callback(output)
        session.pending_inputs.pop(self.STEP_ID, None)
        return CStatus()

    def _set_state(self, state: str) -> None:
        session = get_bound_workflow_session()
        session.step_states[self.STEP_ID] = state

    def card_payload(self, output: StepRunOutput) -> dict[str, Any]:
        return output.card

    def get_derived_keys(self) -> list[str]:
        return _get_step_output_derived_keys(self.STEP_ID)

    def process_operation(
        self,
        user_input: str,
        dependency_results: dict[str, "StepRunOutput"],
        session_state: dict[str, Any],
    ) -> "StepRunOutput":
        """Execute the skill and return a :class:`StepRunOutput`.

        Subclasses must implement this method.  Use ``self.skill_using`` and
        ``self.skill_examples`` as reference for how to call the underlying
        library installed from ``skill.md``.

        Args:
            user_input: Normalised string input provided by the user for this
                step.
            dependency_results: Outputs of upstream steps keyed by step ID.
            session_state: Mutable shared workflow session state dict.

        Returns:
            A :class:`StepRunOutput` containing the skill result.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__}.process_operation() must be implemented by the user.\n"
            f"Refer to self.skill_using and self.skill_examples for usage guidance.\n"
            f"skill_using:\n{self.skill_using}\n\nskill_examples:\n{self.skill_examples}"
        )

    def clone(self):
        return self

    @classmethod
    def step_meta(cls) -> dict[str, Any]:
        return {
            "id": cls.STEP_ID,
            "title": cls.TITLE,
            "prompt": cls.PROMPT,
            "dependencies": list(cls.DEPENDENCIES),
            "inputRequired": cls.INPUT_REQUIRED,
            "nodeKind": cls.NODE_KIND,
        }
        
class WorkflowOperationNode(GNode):
    INPUT_REQUIRED = False
    STEP_ID = ""
    TITLE = ""
    PROMPT = ""
    DEPENDENCIES: list[str] = []
    SERVICES: list[dict[str, str]] = []
    NODE_KIND = "operation"

    def __init__(self) -> None:
        super().__init__()
        self.setName(self.STEP_ID)
        self.setWaitForInput(False)

    def run(self) -> CStatus:
        session = get_bound_workflow_session()
        self._set_state("running")
        dependency_results = {
            dep: session.step_outputs[dep]
            for dep in self.DEPENDENCIES
            if dep in session.step_outputs
        }
        try:
            self.use_service(session.state)
            output = self.process_operation(
                dependency_results,
                session.state,
            )
            if not isinstance(output, StepRunOutput):
                self._set_state("failed")
                return CStatus(1001, f"step {self.STEP_ID} failed: process_operation must return StepRunOutput")
        except Exception as exc:  # pragma: no cover
            self._set_state("failed")
            return CStatus(1001, f"step {self.STEP_ID} failed: {exc}")

        session.step_outputs[self.STEP_ID] = output
        session.step_cards[self.STEP_ID] = self.card_payload(output)
        self._set_state("completed")
        callback = session.submit_callbacks.pop(self.STEP_ID, None)
        if callback is not None:
            callback(output)
        session.pending_inputs.pop(self.STEP_ID, None)
        return CStatus()

    def _set_state(self, state: str) -> None:
        session = get_bound_workflow_session()
        session.step_states[self.STEP_ID] = state

    def card_payload(self, output: StepRunOutput) -> dict[str, Any]:
        return output.card

    def get_derived_keys(self) -> list[str]:
        return _get_step_output_derived_keys(self.STEP_ID)

    def use_service(self, session_state: dict[str, Any]) -> list[dict[str, Any]]:
        meta_map = session_state.get("_workflow_step_meta_map")
        if not isinstance(meta_map, dict):
            meta_map = {}
            session_state["_workflow_step_meta_map"] = meta_map
        if self.STEP_ID not in meta_map:
            meta_map[self.STEP_ID] = self.step_meta()

        usages = _resolve_service_usages_for_step(self.STEP_ID, session_state)
        if usages:
            service_usage_map = session_state.setdefault("service_usage", {})
            if isinstance(service_usage_map, dict):
                service_usage_map[self.STEP_ID] = usages
        return usages

    def clone(self):
        return self

    @classmethod
    def step_meta(cls) -> dict[str, Any]:
        return {
            "id": cls.STEP_ID,
            "title": cls.TITLE,
            "prompt": cls.PROMPT,
            "dependencies": list(cls.DEPENDENCIES),
            "services": list(cls.SERVICES),
            "inputRequired": cls.INPUT_REQUIRED,
            "nodeKind": cls.NODE_KIND,
        }

    def process_operation(
        self,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        raise NotImplementedError()


class WorkflowServiceNode(GNode):
    DEFAULT_WORKDIR = str(Path.cwd())
    INPUT_REQUIRED = False
    STEP_ID = ""
    TITLE = ""
    PROMPT = ""
    DEPENDENCIES: list[str] = []
    NODE_KIND = "service"

    SANDBOX_DOMAIN_ENV = "SANDBOX_DOMAIN"
    SANDBOX_IMAGE_ENV = "SANDBOX_IMAGE"

    DEFAULT_SANDBOX_DOMAIN = "localhost:8120"
    DEFAULT_SANDBOX_IMAGE = "ubuntu:22.04"

    DEFAULT_SANDBOX_TIMEOUT_SECONDS = 600
    DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
    DEFAULT_KILL_ON_EXIT = True

    def __init__(self) -> None:
        super().__init__()
        self.setName(self.STEP_ID)
        self.setWaitForInput(False)
        self._installed: bool = False
        self._service_running: bool = False
        self._pid: int | None = None

    def run(self) -> CStatus:
        session = get_bound_workflow_session()
        self._set_state("running")
        dependency_results = {
            dep: session.step_outputs[dep]
            for dep in self.DEPENDENCIES
            if dep in session.step_outputs
        }
        try:
            output = self.process_operation(
                dependency_results,
                session.state,
            )
            if not isinstance(output, StepRunOutput):
                self._set_state("failed")
                return CStatus(1001, f"step {self.STEP_ID} failed: process_operation must return StepRunOutput")
        except Exception as exc:  # pragma: no cover
            self._set_state("failed")
            workflow_service_registry.mark_failed(self.STEP_ID, str(exc))
            return CStatus(1001, f"step {self.STEP_ID} failed: {exc}")

        session.step_outputs[self.STEP_ID] = output
        session.step_cards[self.STEP_ID] = self.card_payload(output)
        self._set_state("completed")
        callback = session.submit_callbacks.pop(self.STEP_ID, None)
        if callback is not None:
            callback(output)
        session.pending_inputs.pop(self.STEP_ID, None)
        return CStatus()

    def _set_state(self, state: str) -> None:
        session = get_bound_workflow_session()
        session.step_states[self.STEP_ID] = state

    def card_payload(self, output: StepRunOutput) -> dict[str, Any]:
        return output.card

    def get_derived_keys(self) -> list[str]:
        return _get_step_output_derived_keys(self.STEP_ID)

    def clone(self):
        return self

    @classmethod
    def step_meta(cls) -> dict[str, Any]:
        return {
            "id": cls.STEP_ID,
            "title": cls.TITLE,
            "prompt": cls.PROMPT,
            "dependencies": list(cls.DEPENDENCIES),
            "inputRequired": cls.INPUT_REQUIRED,
            "nodeKind": cls.NODE_KIND,
        }

    # ------------------------------------------------------------------
    # Two-phase execution: install → start
    # Service registration happens after start succeeds.
    # ------------------------------------------------------------------

    def install_environment(
        self,
        dependency_results: dict[str, "StepRunOutput"],
        session_state: dict[str, Any],
    ) -> bool:
        """Phase 1 – Install / prepare the runtime environment.

        This method is called first and must complete successfully before
        :meth:`start_service` is invoked.  Typical tasks include installing
        packages, writing config files, or pulling a container image.

        Use the helper methods (e.g. :meth:`run_in_sandbox`,
        :meth:`build_instance_spec`) to interact with the sandbox if needed.

        Args:
            dependency_results: Outputs of upstream steps keyed by step ID.
            session_state: Mutable shared workflow session state dict.

        Returns:
            ``True`` if installation completed successfully, ``False`` otherwise.
            Returning ``False`` aborts execution before :meth:`start_service` is
            called.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__}.install_environment() must be implemented.\n"
            "Install packages, write configs, or pull container images here."
        )

    def start_service(
        self,
        dependency_results: dict[str, "StepRunOutput"],
        session_state: dict[str, Any],
    ) -> int:
        """Phase 2 – Start the service (runs after :meth:`install_environment`).

        This method is called only after :meth:`install_environment` returns
        ``True``, and is skipped automatically if the service is already running
        (i.e. ``self._pid`` is set from a previous call).

        Use it to launch a background process, start a server, or bring up a
        container.  Return the PID of the launched process.

        Args:
            dependency_results: Outputs of upstream steps keyed by step ID.
            session_state: Mutable shared workflow session state dict.

        Returns:
            The integer PID of the running service process.  A value ``<= 0``
            is treated as a failure.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__}.start_service() must be implemented.\n"
            "Launch the background service and return its PID here."
        )

    def process_operation(
        self,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        """Orchestrate install/start and register running service.

        Calls :meth:`install_environment`, then :meth:`start_service`. After
        start succeeds, this node is registered into
        ``workflow_service_registry`` with running status.
        """
        # --- Phase 1: install environment (skipped if already completed) ---
        if not self._installed:
            installed = self.install_environment(dependency_results, session_state)
            if not isinstance(installed, bool):
                raise TypeError(
                    f"install_environment must return bool, got {type(installed).__name__}"
                )
            if not installed:
                raise RuntimeError(
                    f"{self.__class__.__name__}.install_environment() returned False – installation failed."
                )
            self._installed = True

        # --- Phase 2: start service (skipped if already running) ---
        if not self._service_running or self._pid is None:
            pid = self.start_service(dependency_results, session_state)
            if not isinstance(pid, int):
                raise TypeError(
                    f"start_service must return int (PID), got {type(pid).__name__}"
                )
            if pid <= 0:
                raise RuntimeError(
                    f"{self.__class__.__name__}.start_service() returned {pid} – service failed to start."
                )
            self._pid = pid
            self._service_running = True

        workflow_service_registry.register_service(
            self.STEP_ID,
            node_class=self.__class__.__name__,
            metadata={
                "title": self.TITLE,
                "node_kind": self.NODE_KIND,
                "dependencies": list(self.DEPENDENCIES),
            },
        )
        workflow_service_registry.update_service_status(
            self.STEP_ID,
            status="running",
            is_running=True,
            pid=self._pid,
            installed=self._installed,
            last_error="",
        )

        return StepRunOutput(
            summary=f"Service {self.STEP_ID} is running (pid={self._pid}).",
            card={
                "service": self.STEP_ID,
                "status": "running",
                "pid": self._pid,
                "installed": self._installed,
            },
            derived={
                "service_name": self.STEP_ID,
                "service_running": True,
                "pid": self._pid,
                "installed": self._installed,
            },
        )

class WorkflowChatNode(GNode):
    INPUT_REQUIRED = True
    STEP_ID = ""
    TITLE = ""
    PROMPT = ""
    DEPENDENCIES: list[str] = []
    NODE_KIND = "chat"

    PROVIDER_ENV = "META_AGENT_LLM_PROVIDER"
    MODEL_ENV = "META_AGENT_LLM_MODEL"
    BASE_URL_ENV = "META_AGENT_LLM_BASE_URL"

    DEFAULT_PROVIDER = "deepseek"
    DEFAULT_MODEL_BY_PROVIDER = {
        "openai": "gpt-4.1-mini",
        "deepseek": "deepseek-chat",
        "qwen": "qwen-plus",
    }

    DEEPSEEK_BASE_URL = "https://api.deepseek.com"
    QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    TEMPERATURE = 0.2
    MAX_TOKENS = 8192
    SYSTEM_PROMPT = (
        "You are a helpful workflow assistant. Use dependency outputs and user input to produce a concise, useful answer."
    )

    def __init__(self) -> None:
        super().__init__()
        self.setName(self.STEP_ID)
        self.setWaitForInput(False)
        self.setInputPrompt(self.PROMPT)
        self.setInputHandler(self._input_handler)
        self._provider = self._resolve_provider()
        self._model = self._resolve_model(self._provider)
        self._client = self._build_openai_client(self._provider)

    def _input_handler(self, user_input: str) -> CStatus:
        session = get_bound_workflow_session()
        session.pending_inputs[self.STEP_ID] = user_input
        return CStatus()

    def run(self) -> CStatus:
        session = get_bound_workflow_session()
        self._set_state("running")
        session.streamed_text_deltas.pop(self.STEP_ID, None)
        raw_input = _normalize_step_input(session.pending_inputs.get(self.STEP_ID, ""))
        if self.INPUT_REQUIRED and not raw_input:
            self._set_state("awaiting_input")
            return CStatus(1003, f"input required for step {self.STEP_ID}")

        dependency_results = {
            dep: session.step_outputs[dep]
            for dep in self.DEPENDENCIES
            if dep in session.step_outputs
        }
        try:
            prompt_or_output = self.process_chat(
                raw_input,
                dependency_results,
                session.state,
            )
            if isinstance(prompt_or_output, StepRunOutput):
                output = prompt_or_output
                session.streamed_text_deltas.pop(self.STEP_ID, None)
            else:
                user_prompt = _safe_string(prompt_or_output)
                if not user_prompt:
                    self._set_state("failed")
                    return CStatus(1001, f"step {self.STEP_ID} failed: process_chat must return a non-empty prompt string")

                response_text = self._request_llm(user_prompt)
                output = self.build_step_output(response_text)

            if not isinstance(output, StepRunOutput):
                self._set_state("failed")
                return CStatus(1001, f"step {self.STEP_ID} failed: run must produce StepRunOutput")
        except Exception as exc:  # pragma: no cover
            self._set_state("failed")
            return CStatus(1001, f"step {self.STEP_ID} failed: {exc}")

        session.step_outputs[self.STEP_ID] = output
        session.step_cards[self.STEP_ID] = self.card_payload(output)
        self._set_state("completed")
        callback = session.submit_callbacks.pop(self.STEP_ID, None)
        if callback is not None:
            callback(output)
        session.pending_inputs.pop(self.STEP_ID, None)
        return CStatus()

    def _set_state(self, state: str) -> None:
        session = get_bound_workflow_session()
        session.step_states[self.STEP_ID] = state

    def card_payload(self, output: StepRunOutput) -> dict[str, Any]:
        return output.card

    def get_derived_keys(self) -> list[str]:
        return _get_step_output_derived_keys(self.STEP_ID)

    def _serialize_dependency_results(
        self,
        dependency_results: dict[str, StepRunOutput],
    ) -> dict[str, Any]:
        return {
            dep: {
                "summary": output.summary,
                "card": output.card,
                "derived": output.derived,
            }
            for dep, output in dependency_results.items()
        }

    def build_user_prompt(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> str:
        dependency_payload = self._serialize_dependency_results(dependency_results)
        sections = [
            "User input:",
            user_input,
            "",
            "Dependency results (JSON):",
            json.dumps(dependency_payload, ensure_ascii=False, indent=2),
            "",
            "Session state (JSON):",
            json.dumps(session_state, ensure_ascii=False, indent=2),
        ]
        return "\n".join(sections).strip()

    def _resolve_provider(self) -> str:
        provider = os.getenv(self.PROVIDER_ENV, self.DEFAULT_PROVIDER)
        return _safe_string(provider).lower() or self.DEFAULT_PROVIDER

    def _resolve_model(self, provider: str) -> str:
        model = os.getenv(self.MODEL_ENV)
        if model and model.strip():
            return model.strip()
        return self.DEFAULT_MODEL_BY_PROVIDER.get(provider, self.DEFAULT_MODEL_BY_PROVIDER["openai"])

    def _build_openai_client(self, provider: str):
        if OpenAI is None:
            raise ImportError(
                "openai is not installed. Install `openai` to use WorkflowChatNode with OpenAI-compatible providers."
            )

        custom_base_url = os.getenv(self.BASE_URL_ENV, "").strip()
        if provider == "deepseek":
            api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
            if not api_key:
                raise ValueError("Missing DEEPSEEK_API_KEY for WorkflowChatNode")
            return OpenAI(api_key=api_key, base_url=custom_base_url or self.DEEPSEEK_BASE_URL)

        if provider == "qwen":
            api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
            if not api_key:
                raise ValueError("Missing DASHSCOPE_API_KEY for WorkflowChatNode")
            return OpenAI(api_key=api_key, base_url=custom_base_url or self.QWEN_BASE_URL)

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ValueError("Missing OPENAI_API_KEY for WorkflowChatNode")
        if custom_base_url:
            return OpenAI(api_key=api_key, base_url=custom_base_url)
        return OpenAI(api_key=api_key)

    def process_chat(
        self,
        user_input: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> str:
        return self.build_user_prompt(user_input, dependency_results, session_state)

    def _request_llm(self, user_prompt: str) -> str:
        response_stream = self._client.chat.completions.create(
            model=self._model,
            temperature=self.TEMPERATURE,
            max_tokens=self.MAX_TOKENS,
            stream=True,
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        print(response_stream)

        deltas: list[str] = []
        for chunk in response_stream:
            if not getattr(chunk, "choices", None):
                continue
            choice = chunk.choices[0]
            delta_obj = getattr(choice, "delta", None)
            delta = getattr(delta_obj, "content", "") if delta_obj is not None else ""
            if isinstance(delta, str) and delta:
                deltas.append(delta)
            elif isinstance(delta, list):
                for item in delta:
                    text = getattr(item, "text", "") if item is not None else ""
                    if isinstance(text, str) and text:
                        deltas.append(text)

        content = _safe_string("".join(deltas))
        if not content:
            raise RuntimeError("WorkflowChatNode received empty LLM response")

        session = get_bound_workflow_session()
        session.streamed_text_deltas[self.STEP_ID] = deltas
        return content

    def build_step_output(self, content: str) -> StepRunOutput:
        card = {
            "provider": self._provider,
            "model": self._model,
            "response": content,
        }
        derived = {
            "response": content,
            "provider": self._provider,
            "model": self._model,
        }
        return StepRunOutput(summary=content, card=card, derived=derived)

    def clone(self):
        return self

    @classmethod
    def step_meta(cls) -> dict[str, Any]:
        return {
            "id": cls.STEP_ID,
            "title": cls.TITLE,
            "prompt": cls.PROMPT,
            "dependencies": list(cls.DEPENDENCIES),
            "inputRequired": cls.INPUT_REQUIRED,
            "nodeKind": cls.NODE_KIND,
        }


class WorkflowImageNode(GNode):
    INPUT_REQUIRED = False
    STEP_ID = ""
    TITLE = ""
    PROMPT = ""
    DEPENDENCIES: list[str] = []
    NODE_KIND = "image"

    API_KEY_ENV = "ARK_API_KEY"
    MODEL_ENV = "META_AGENT_VLM_MODEL"
    BASE_URL_ENV = "META_AGENT_VLM_BASE_URL"
    DEFAULT_MODEL = "doubao-seed-2-0-pro-260215"
    DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"

    SYSTEM_PROMPT = (
        "You are a vision-language workflow assistant. Analyze the provided image and combine it "
        "with dependency context to produce concise, actionable results."
    )

    def __init__(self) -> None:
        super().__init__()
        self.setName(self.STEP_ID)
        self.setWaitForInput(False)
        self._model = self._resolve_model()
        self._base_url = self._resolve_base_url()
        self._client = None
        self._client_init_error: Exception | None = None
        try:
            self._client = self._build_openai_client()
        except Exception as exc:
            self._client_init_error = exc
    
    def run(self) -> CStatus:
        session = get_bound_workflow_session()
        self._set_state("running")

        dependency_results = {
            dep: session.step_outputs[dep]
            for dep in self.DEPENDENCIES
            if dep in session.step_outputs
        }
        image_refs = self._collect_image_locations_from_dependencies(dependency_results)
        if not image_refs:
            self._set_state("failed")
            return CStatus(1001, f"step {self.STEP_ID} failed: no image locations found in dependency_results")

        try:
            prompt_or_output = self.process_images_prompts(
                image_refs,
                "",
                dependency_results,
                session.state,
            )
            if not isinstance(prompt_or_output, str):
                self._set_state("failed")
                return CStatus(1001, f"step {self.STEP_ID} failed: process_images_prompts must return a string prompt")

            user_prompt = _safe_string(prompt_or_output)
            if not user_prompt:
                self._set_state("failed")
                return CStatus(1001, f"step {self.STEP_ID} failed: process_images_prompts must return a non-empty prompt string")

            response_text, image_meta = self._request_vision_model(image_refs, user_prompt, session.state)
            output = self.build_step_output(response_text, image_refs, "", image_meta)

            if not isinstance(output, StepRunOutput):
                self._set_state("failed")
                return CStatus(1001, f"step {self.STEP_ID} failed: run must produce StepRunOutput")
        except Exception as exc:  # pragma: no cover
            self._set_state("failed")
            return CStatus(1001, f"step {self.STEP_ID} failed: {exc}")

        session.step_outputs[self.STEP_ID] = output
        session.step_cards[self.STEP_ID] = self.card_payload(output)
        self._set_state("completed")
        callback = session.submit_callbacks.pop(self.STEP_ID, None)
        if callback is not None:
            callback(output)
        session.pending_inputs.pop(self.STEP_ID, None)
        return CStatus()

    def _set_state(self, state: str) -> None:
        session = get_bound_workflow_session()
        session.step_states[self.STEP_ID] = state

    def card_payload(self, output: StepRunOutput) -> dict[str, Any]:
        return output.card

    def get_derived_keys(self) -> list[str]:
        return _get_step_output_derived_keys(self.STEP_ID)

    def _serialize_dependency_results(
        self,
        dependency_results: dict[str, StepRunOutput],
    ) -> dict[str, Any]:
        return {
            dep: {
                "summary": output.summary,
                "card": output.card,
                "derived": output.derived,
            }
            for dep, output in dependency_results.items()
        }

    def _collect_image_locations_from_dependencies(
        self,
        dependency_results: dict[str, StepRunOutput],
    ) -> list[str]:
        refs: list[str] = []

        def _append_ref(value: Any) -> None:
            if isinstance(value, str):
                ref = value.strip()
                if ref:
                    refs.append(ref)
                return

            if isinstance(value, dict):
                location = _safe_string(value.get("location") or value.get("path") or value.get("filePath"))
                if location:
                    refs.append(location)
                for nested in value.values():
                    if isinstance(nested, (dict, list)):
                        _append_ref(nested)
                return

            if isinstance(value, list):
                for item in value:
                    _append_ref(item)

        for output in dependency_results.values():
            derived = output.derived if isinstance(output.derived, dict) else {}
            if "savedLocations" in derived:
                _append_ref(derived.get("savedLocations"))
            if "imageRefs" in derived:
                _append_ref(derived.get("imageRefs"))
            if "savedFiles" in derived:
                _append_ref(derived.get("savedFiles"))
            if "images" in derived:
                _append_ref(derived.get("images"))
            if "imageRef" in derived:
                _append_ref(derived.get("imageRef"))
            if "image" in derived:
                _append_ref(derived.get("image"))

        unique_refs = [ref for ref in dict.fromkeys(refs) if ref]
        return unique_refs

    def _resolve_model(self) -> str:
        model = os.getenv(self.MODEL_ENV)
        if model and model.strip():
            return model.strip()
        return self.DEFAULT_MODEL

    def _resolve_base_url(self) -> str:
        base_url = os.getenv(self.BASE_URL_ENV)
        if base_url and base_url.strip():
            return base_url.strip()
        return self.DEFAULT_BASE_URL

    def _build_openai_client(self):
        if OpenAI is None:
            raise ImportError(
                "openai is not installed. Install `openai` to use WorkflowImageNode with OpenAI-compatible providers."
            )

        api_key = os.getenv(self.API_KEY_ENV, "").strip()
        if not api_key:
            raise ValueError(f"Missing {self.API_KEY_ENV} for WorkflowImageNode")
        return OpenAI(api_key=api_key, base_url=self._base_url)

    def build_user_prompt(
        self,
        request_text: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> str:
        dependency_payload = self._serialize_dependency_results(dependency_results)
        effective_request = request_text.strip() if request_text else "Please analyze this image in detail."
        sections = [
            "User request:",
            effective_request,
            "",
            "Dependency results (JSON):",
            json.dumps(dependency_payload, ensure_ascii=False, indent=2),
            "",
            "Session state (JSON):",
            json.dumps(session_state, ensure_ascii=False, indent=2),
        ]
        return "\n".join(sections).strip()

    def process_images_prompts(
        self,
        image_refs: list[str],
        request_text: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> str:
        primary_image_ref = image_refs[0] if image_refs else ""
        return self.process_image_prompts(primary_image_ref, request_text, dependency_results, session_state)

    def process_image_prompts(
        self,
        image_ref: str,
        request_text: str,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> str:
        return self.build_user_prompt(request_text, dependency_results, session_state)

    def _load_remote_image_bytes(
        self,
        image_ref: str,
        session_state: dict[str, Any],
    ) -> tuple[bytes | None, str | None]:
        remote_loader = session_state.get("imageRemoteLoader")
        if callable(remote_loader):
            loaded = remote_loader(image_ref, session_state)
            if isinstance(loaded, bytes):
                return loaded, None
            if isinstance(loaded, tuple) and len(loaded) == 2 and isinstance(loaded[0], bytes):
                mime = _safe_string(loaded[1]) if loaded[1] is not None else ""
                return loaded[0], mime or None

        if image_ref.startswith("http://") or image_ref.startswith("https://"):
            request = Request(image_ref, headers={"User-Agent": "meta-agent-workflow-image-node"})
            with urlopen(request, timeout=30) as response:
                image_bytes = response.read()
                content_type = _safe_string(response.headers.get("Content-Type", ""))
                mime_type = content_type.split(";")[0].strip() if content_type else ""
                return image_bytes, mime_type or None

        return None, None

    def _prepare_image_data(self, image_ref: str, session_state: dict[str, Any]) -> tuple[str, str, int]:
        ref = _safe_string(image_ref)
        if not ref:
            raise ValueError("image_ref is empty")

        if ref.startswith("data:image/") and "base64," in ref:
            header, _, b64_part = ref.partition("base64,")
            mime_type = header.replace("data:", "").replace(";", "").replace("base64", "").strip() or "image/png"
            image_bytes = base64.b64decode(b64_part)
            return ref, mime_type, len(image_bytes)

        image_bytes: bytes | None = None
        mime_type = mimetypes.guess_type(ref)[0] or "image/png"

        path = Path(ref)
        if path.exists() and path.is_file():
            image_bytes = path.read_bytes()
            guessed = mimetypes.guess_type(path.name)[0]
            if guessed:
                mime_type = guessed

        if image_bytes is None:
            remote_bytes, remote_mime = self._load_remote_image_bytes(ref, session_state)
            if remote_bytes is not None:
                image_bytes = remote_bytes
                if remote_mime:
                    mime_type = remote_mime

        if image_bytes is None:
            decoded = _decode_bytes_string(ref)
            if decoded is not None:
                image_bytes = decoded

        if image_bytes is None:
            try:
                image_bytes = base64.b64decode(ref, validate=True)
            except Exception as exc:
                raise ValueError("Unsupported image input. Provide file path, bytes string, or base64/data-url image.") from exc

        data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('utf-8')}"
        return data_url, mime_type, len(image_bytes)

    def _extract_response_text(self, response: Any) -> str:
        output_text = _safe_string(getattr(response, "output_text", ""))
        if output_text:
            return output_text

        output_items = getattr(response, "output", None)
        if isinstance(output_items, list):
            chunks: list[str] = []
            for item in output_items:
                content_list = getattr(item, "content", None)
                if not isinstance(content_list, list):
                    continue
                for content_item in content_list:
                    text_value = _safe_string(getattr(content_item, "text", ""))
                    if text_value:
                        chunks.append(text_value)
            if chunks:
                return "\n".join(chunks).strip()

        return ""

    def _request_vision_model(
        self,
        image_refs: list[str],
        user_prompt: str,
        session_state: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]]]:
        if not image_refs:
            raise ValueError("image_refs is empty")
        if self._client is None:
            reason = str(self._client_init_error) if self._client_init_error is not None else "client is unavailable"
            raise ValueError(f"WorkflowImageNode client initialization failed: {reason}")

        user_content: list[dict[str, Any]] = []
        image_metas: list[dict[str, Any]] = []
        for image_ref in image_refs:
            image_url, mime_type, image_bytes = self._prepare_image_data(image_ref, session_state)
            user_content.append({"type": "input_image", "image_url": image_url})
            image_metas.append(
                {
                    "mimeType": mime_type,
                    "byteSize": image_bytes,
                    "source": image_ref,
                }
            )
        user_content.append({"type": "input_text", "text": user_prompt})

        response = self._client.responses.create(
            model=self._model,
            input=[
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": self.SYSTEM_PROMPT}],
                },
                {
                    "role": "user",
                    "content": user_content,
                },
            ],
        )
        content = _safe_string(self._extract_response_text(response))
        if not content:
            raise RuntimeError("WorkflowImageNode received empty VLM response")

        return content, image_metas

    def build_step_output(
        self,
        content: str,
        image_refs: list[str],
        request_text: str,
        image_meta: list[dict[str, Any]],
    ) -> StepRunOutput:
        primary_image_ref = image_refs[0] if image_refs else ""
        primary_image_meta = image_meta[0] if image_meta else {}
        summary = content
        card = {
            "provider": "ark",
            "model": self._model,
            "baseUrl": self._base_url,
            "request": request_text,
            "response": content,
            "image": primary_image_meta,
            "images": image_meta,
            "imageCount": len(image_meta),
        }
        derived = {
            "analysis": content,
            "request": request_text,
            "imageRef": primary_image_ref,
            "imageRefs": image_refs,
            "image": primary_image_meta,
            "images": image_meta,
            "model": self._model,
            "baseUrl": self._base_url,
        }
        return StepRunOutput(summary=summary, card=card, derived=derived)

    def clone(self):
        return self

    @classmethod
    def step_meta(cls) -> dict[str, Any]:
        return {
            "id": cls.STEP_ID,
            "title": cls.TITLE,
            "prompt": cls.PROMPT,
            "dependencies": list(cls.DEPENDENCIES),
            "inputRequired": cls.INPUT_REQUIRED,
            "nodeKind": cls.NODE_KIND,
        }

