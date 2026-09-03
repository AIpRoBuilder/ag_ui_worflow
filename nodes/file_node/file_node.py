from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydaograph import CStatus, GNode

from ...session import get_node_workflow_session
from ...tools import node_main_utility
from ...workflow_types import StepRunOutput
from .._shared import (
    _apply_node_descriptor_attributes,
    _build_node_descriptor_meta,
    _decode_bytes_string,
    _get_step_output_derived_keys,
    _safe_string,
)


class WorkflowFileNode(GNode):
    INPUT_REQUIRED = True
    STEP_ID = ""
    TITLE = ""
    PROMPT = ""
    DEPENDENCIES: list[str] = []
    DESCRIPTOR_PROMPT_FILE = "descriptor_prompt.md"

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
        _apply_node_descriptor_attributes(self, self.DESCRIPTOR_PROMPT_FILE)

    def _input_handler(self, user_input: str) -> CStatus:
        session = get_node_workflow_session(self)
        session.pending_inputs[self.STEP_ID] = user_input
        return CStatus()

    def run(self) -> CStatus:
        session = get_node_workflow_session(self)
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
        session = get_node_workflow_session(self)
        session.step_states[self.STEP_ID] = state

    def card_payload(self, output: StepRunOutput) -> dict[str, Any]:
        return output.card

    def get_derived_keys(self) -> list[str]:
        return _get_step_output_derived_keys(self, self.STEP_ID)

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

            content_b64 = value.get("bytes") or value.get("contentBase64")
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

            candidate = Path(raw).expanduser()
            if candidate.exists() and candidate.is_file():
                return {
                    "fileName": candidate.name or "uploaded_file",
                    "bytes": candidate.read_bytes(),
                }

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

    @node_main_utility
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
                        "path": str(target_path),
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
                    "path": location,
                    "byteSize": len(data),
                }
            )

        return saved

    def build_step_output(self, saved_files: list[dict[str, Any]]) -> StepRunOutput:
        locations = [
            _safe_string(item.get("path"))
            for item in saved_files
            if _safe_string(item.get("path"))
        ]
        file_names = [
            _safe_string(item.get("fileName"))
            for item in saved_files
            if _safe_string(item.get("fileName"))
        ]
        saved_files = [{"fileName": file_names[i], "path": locations[i]} for i in range(len(saved_files))]
        card = {
            "fileCount": len(saved_files),
            "files": saved_files,
        }
        derived = {
            "savedFiles": saved_files,
            "fileCount": len(saved_files),
        }
        return StepRunOutput(card=card, derived=derived)

    def clone(self):
        return self

    @classmethod
    def meta_node_kind(cls) -> str:
        return "WorkflowFileNode"

    @classmethod
    def step_meta(cls) -> dict[str, Any]:
        return {
            "id": cls.STEP_ID,
            "title": cls.TITLE,
            "prompt": cls.PROMPT,
            "dependencies": list(cls.DEPENDENCIES),
            "inputRequired": cls.INPUT_REQUIRED,
            "metaNodeKind": cls.meta_node_kind(),
            **_build_node_descriptor_meta(cls, cls.DESCRIPTOR_PROMPT_FILE),
        }