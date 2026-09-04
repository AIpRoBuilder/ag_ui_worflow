# Spatial Temporal Contract Node Descriptor

## Structure
- Subclass `SpatialTemporalContractNode` for nodes that turn dependency or session context into a spatial-temporal contract JSON document.
- Define `STEP_ID`, optional `TITLE`, and any `DEPENDENCIES` required to source the contract description.
- Implement `process_operation(dependency_results, session_state)` on the subclass when you need custom generation behavior.
- Keep the full request construction, model call, and `StepRunOutput` assembly inside `process_operation` instead of spreading subclass logic across helper overrides.

## Function
- Resolve a scenario description from session state or upstream step outputs.
- Call an OpenAI-compatible model and has to use `self._load_system_prompt()` as the system prompt.
- Parse the model response into normalized spatial-temporal contract JSON.
- Return the contract, raw response, and usage metadata inside a `StepRunOutput`.

## Implementation Guide
- Ensure `OPENAI_API_KEY` or `session_state["openaiApiKey"]` is available before execution.
- Provide dependency outputs whose `derived` or `card` fields contain descriptive text if the subclass relies on upstream context.
- Start `process_operation` by resolving the description from `session_state` or dependency outputs, then build the request payload locally in that same method.
- When invoking the model in a subclass, always load the system prompt through `self._load_system_prompt()` and pass it as the system message.
- Set `session_state["spatialTemporalContractPrompt"]` or `session_state["spatialTemporalContractGuidance"]` to inject extra user-prompt guidance without changing the system prompt source.
- Keep JSON parsing, normalization, and card or derived output assembly in `process_operation` so the subclass remains self-contained for code generation.
- Preserve the JSON-only response contract because downstream consumers expect parseable structured output.

## Example
```python
import json

from ag_ui_workflow import SpatialTemporalContractNode, StepRunOutput, step_output_text


class SceneContractNode(SpatialTemporalContractNode):
    STEP_ID = "scene_contract"
    TITLE = "Scene Contract"
    DEPENDENCIES = ["scene_outline"]

    def process_operation(self, dependency_results, session_state):
        source = dependency_results["scene_outline"]
        description = source.derived.get("spatialTemporalContractDescription") or step_output_text(source)
        request_payload = {
            "description": self._coerce_json_value(description),
            "returnJsonOnly": True,
            "style": "Prefer explicit temporal scopes when the scene implies ordered actions.",
        }
        model_name = (
            session_state.get("spatialTemporalContractModel")
            or session_state.get("openaiModel")
            or self.DEFAULT_OPENAI_MODEL
        )

        client = self._create_openai_client(session_state["openaiApiKey"])
        completion = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": self._load_system_prompt()},
                {
                    "role": "user",
                    "content": "Generate a spatial-temporal contract JSON. Return valid JSON only.\n"
                    f"{json.dumps(request_payload, ensure_ascii=False, indent=2)}",
                },
            ],
        )
        raw_response = self._extract_completion_text(completion)
        contract = self._normalize_contract(self._parse_contract_json(raw_response))
        response_json = json.dumps(contract, ensure_ascii=False, indent=2)
        return StepRunOutput(
            card={"title": self.TITLE, "response": response_json, "contract": contract},
            derived={"spatialTemporalContract": contract, "spatialTemporalContractJson": response_json},
        )
```