from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydaograph import CStatus, GNode

from ...session import get_node_workflow_session
from ...tools import node_main_utility, node_subclass_implementation
from ...workflow_types import StepRunOutput, step_output_text
from .._shared import (
    _apply_node_descriptor_attributes,
    _build_node_descriptor_meta,
    _get_step_output_derived_keys,
    bootstrap_package_root,
)

ROOT_DIR = bootstrap_package_root(__file__)


class SpatialTemporalContractNode(GNode):
    """Generate a spatial-temporal contract JSON from dependency or session context."""

    STEP_ID = ""
    TITLE = "SpatialTemporal Contract"
    PROMPT = "Generates a spatial-temporal contract from dependency output or session state."
    DEPENDENCIES: list[str] = []
    INPUT_REQUIRED = False
    DESCRIPTOR_PROMPT_FILE = str(ROOT_DIR / "nodes" / "spatial_temporal_contract_node" / "descriptor_prompt.md")

    OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
    OPENAI_MODEL_ENV = "OPENAI_MODEL"
    DEFAULT_OPENAI_MODEL = "deepseek-V4"
    SYSTEM_PROMPT_FILE = "spatial_temporal_contract_system_prompt.md"

    def __init__(self) -> None:
        super().__init__()
        self.setName(self.STEP_ID)
        self.setWaitForInput(False)
        _apply_node_descriptor_attributes(self, self.DESCRIPTOR_PROMPT_FILE)

    def run(self) -> CStatus:
        session = get_node_workflow_session(self)
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

    def clone(self):
        return self

    @classmethod
    def meta_node_kind(cls) -> str:
        return "SpatialTemporalContractNode"

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

    @node_main_utility
    def process_operation(
        self,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> StepRunOutput:
        description = self._resolve_description(dependency_results, session_state)
        request_payload = self._build_request_payload(description, dependency_results)
        contract, raw_response, model_name, usage = self._generate_contract(
            request_payload,
            session_state,
        )
        response_json = json.dumps(contract, ensure_ascii=False, indent=2)

        card = {
            "title": self.TITLE or self.STEP_ID or "SpatialTemporal Contract",
            "response": response_json,
            "contract": contract,
            "model": model_name,
        }
        if usage is not None:
            card["usage"] = usage

        derived = {
            "spatialTemporalContract": contract,
            "spatialTemporalContractJson": response_json,
            "objectCount": self._count_items(contract.get("objects")),
            "relationCount": self._count_items(contract.get("relations")),
            "model": model_name,
            "rawResponse": raw_response,
        }
        if usage is not None:
            derived["usage"] = usage

        return StepRunOutput(card=card, derived=derived)

    def _build_request_payload(
        self,
        description: Any,
        dependency_results: dict[str, StepRunOutput],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "description": self._coerce_json_value(description),
            "returnJsonOnly": True,
        }
        dependency_payload = self._serialize_dependency_results(dependency_results)
        if dependency_payload:
            payload["dependencyContext"] = dependency_payload
        return payload

    def _resolve_description(
        self,
        dependency_results: dict[str, StepRunOutput],
        session_state: dict[str, Any],
    ) -> Any:
        state_description = session_state.get("spatialTemporalContractDescription")
        if self._has_content(state_description):
            return state_description

        for output in dependency_results.values():
            derived_description = output.derived.get("spatialTemporalContractDescription")
            if self._has_content(derived_description):
                return derived_description

            card_description = output.card.get("spatialTemporalContractDescription")
            if self._has_content(card_description):
                return card_description

            fallback_text = step_output_text(output)
            if fallback_text:
                return fallback_text

        raise RuntimeError(
            "No description was found for SpatialTemporalContractNode. "
            "Provide it in session_state['spatialTemporalContractDescription'] "
            "or in an upstream step output."
        )

    @node_subclass_implementation
    def _generate_contract(
        self,
        request_payload: dict[str, Any],
        session_state: dict[str, Any],
    ) -> tuple[dict[str, Any], str, str, dict[str, Any] | None]:
        """Generate the contract using the default OpenAI chat completion flow.

        Subclasses can override this method when they need full control over the
        model invocation, or override ``_build_generation_user_prompt`` to only
        customize the prompt text while preserving the parsing pipeline.
        """
        api_key = self._resolve_api_key(session_state)
        model_name = self._resolve_model(session_state)
        system_prompt = self._load_system_prompt()
        client = self._create_openai_client(api_key)

        completion = client.chat.completions.create(
            model=model_name,
            messages=self._build_generation_messages(
                request_payload,
                session_state,
                system_prompt,
            ),
        )

        raw_response = self._extract_completion_text(completion)
        contract = self._parse_contract_json(raw_response)
        usage = self._extract_usage(completion)
        return self._normalize_contract(contract), raw_response, model_name, usage

    def _build_generation_messages(
        self,
        request_payload: dict[str, Any],
        session_state: dict[str, Any],
        system_prompt: str,
    ) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": self._build_generation_user_prompt(
                    request_payload,
                    session_state,
                ),
            },
        ]

    def _build_generation_user_prompt(
        self,
        request_payload: dict[str, Any],
        session_state: dict[str, Any],
    ) -> str:
        guidance_prompt = self._resolve_generation_guidance(session_state)
        prompt_parts: list[str] = []
        if guidance_prompt:
            prompt_parts.append(
                "Additional guidance for spatial-temporal contract generation:\n"
                f"{guidance_prompt}"
            )

        prompt_parts.append(
            "Generate a spatial-temporal contract JSON for the following input. "
            "Return valid JSON only.\n"
            f"{json.dumps(request_payload, ensure_ascii=False, indent=2)}"
        )
        return "\n\n".join(prompt_parts)

    def _resolve_generation_guidance(self, session_state: dict[str, Any]) -> str:
        return self._as_text(
            session_state.get("spatialTemporalContractPrompt")
            or session_state.get("spatialTemporalContractGuidance")
        )

    def _resolve_api_key(self, session_state: dict[str, Any]) -> str:
        state_key = self._as_text(
            session_state.get("spatialTemporalContractApiKey")
            or session_state.get("openaiApiKey")
            or session_state.get(self.OPENAI_API_KEY_ENV)
        )
        env_key = self._as_text(os.getenv(self.OPENAI_API_KEY_ENV, ""))
        api_key = state_key or env_key
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not configured. Set it in the environment or session_state['openaiApiKey']."
            )
        return api_key

    def _resolve_model(self, session_state: dict[str, Any]) -> str:
        return (
            self._as_text(
                session_state.get("spatialTemporalContractModel")
                or session_state.get("openaiModel")
                or session_state.get(self.OPENAI_MODEL_ENV)
            )
            or self._as_text(os.getenv(self.OPENAI_MODEL_ENV, ""))
            or self.DEFAULT_OPENAI_MODEL
        )

    def _load_system_prompt(self) -> str:
        prompt_path = ROOT_DIR / self.SYSTEM_PROMPT_FILE
        if not prompt_path.exists():
            raise FileNotFoundError(f"system prompt not found at {prompt_path}")
        return prompt_path.read_text(encoding="utf-8").strip()

    def _create_openai_client(self, api_key: str) -> Any:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "The openai package is required to use SpatialTemporalContractNode. Install the project dependencies first."
            ) from exc
        return OpenAI(api_key=api_key)

    def _extract_completion_text(self, completion: Any) -> str:
        choices = getattr(completion, "choices", None)
        if not isinstance(choices, list) or not choices:
            raise ValueError("OpenAI returned no completion choices")

        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)

        if isinstance(content, str) and content.strip():
            return content.strip()

        if isinstance(content, list):
            fragments: list[str] = []
            for item in content:
                if isinstance(item, str) and item.strip():
                    fragments.append(item.strip())
                    continue

                text_value = getattr(item, "text", None)
                if isinstance(text_value, str) and text_value.strip():
                    fragments.append(text_value.strip())
            if fragments:
                return "\n".join(fragments)

        raise ValueError("OpenAI returned an empty completion message")

    def _parse_contract_json(self, raw_response: str) -> dict[str, Any]:
        cleaned = raw_response.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        candidates = [cleaned]
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            fragment = cleaned[start:end + 1]
            if fragment not in candidates:
                candidates.append(fragment)

        decoder = json.JSONDecoder()
        for candidate in candidates:
            try:
                parsed, _ = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

        preview = cleaned[:400]
        raise ValueError(f"OpenAI response was not valid JSON: {preview}")

    def _normalize_contract(self, contract: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(contract)
        normalized.setdefault("contractVersion", "1.0")
        normalized.setdefault("model", "entity-relation-graph")
        normalized.setdefault("mediaType", "scenario")
        if not isinstance(normalized.get("objects"), list):
            normalized["objects"] = []
        if not isinstance(normalized.get("relations"), list):
            normalized["relations"] = []
        return normalized

    def _serialize_dependency_results(
        self,
        dependency_results: dict[str, StepRunOutput],
    ) -> dict[str, Any]:
        serialized: dict[str, Any] = {}
        for step_id, output in dependency_results.items():
            serialized[step_id] = {
                "card": self._make_json_safe(output.card),
                "derived": self._make_json_safe(output.derived),
            }
        return serialized

    def _coerce_json_value(self, value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return ""
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return stripped
        if isinstance(value, (dict, list, tuple)):
            return self._make_json_safe(value)
        if isinstance(value, (int, float, bool)):
            return value
        return self._as_text(value)

    def _extract_usage(self, completion: Any) -> dict[str, Any] | None:
        usage = getattr(completion, "usage", None)
        if usage is None:
            return None
        if hasattr(usage, "model_dump"):
            data = usage.model_dump(exclude_none=True)
            return data if isinstance(data, dict) and data else None

        usage_dict: dict[str, Any] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage, key, None)
            if value is not None:
                usage_dict[key] = value
        return usage_dict or None

    def _count_items(self, value: Any) -> int:
        return len(value) if isinstance(value, list) else 0

    def _has_content(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, dict)):
            return bool(value)
        return True

    def _make_json_safe(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                self._as_text(key): self._make_json_safe(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._make_json_safe(item) for item in value]
        if isinstance(value, tuple):
            return [self._make_json_safe(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return self._as_text(value)

    def _as_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        return str(value).strip()