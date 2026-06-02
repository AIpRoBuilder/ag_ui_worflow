from __future__ import annotations

import json
import os
from typing import Any

from pydaograph import CStatus, GNode

from ..session import get_bound_workflow_session
from ..types import StepRunOutput
from ._shared import OpenAI, _get_step_output_derived_keys, _normalize_step_input, _safe_string


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