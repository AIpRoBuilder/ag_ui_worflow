# Spatial Temporal Contract Node Descriptor

## Structure
- Subclass `SpatialTemporalContractNode` for nodes that turn dependency or session context into a spatial-temporal contract JSON document.
- Define `STEP_ID`, optional `TITLE`, and any `DEPENDENCIES` required to source the contract description.
- Reuse the built-in `process_operation(dependency_results, session_state)` unless you need a custom request or normalization strategy.
- Override helper methods such as `_build_request_payload`, `_resolve_description`, `_build_generation_user_prompt`, `_generate_contract`, or `_normalize_contract` when specializing behavior.

## Function
- Resolve a scenario description from session state or upstream step outputs.
- Call an OpenAI-compatible model with a packaged markdown system prompt.
- Parse the model response into normalized spatial-temporal contract JSON.
- Return the contract, raw response, and usage metadata inside a `StepRunOutput`.

## Implementation Guide
- Ensure `OPENAI_API_KEY` or `session_state["openaiApiKey"]` is available before execution.
- Provide dependency outputs whose `derived` or `card` fields contain descriptive text if the subclass relies on upstream context.
- Override `_build_request_payload` when additional structured context needs to be passed to the model.
- Set `session_state["spatialTemporalContractPrompt"]` or `session_state["spatialTemporalContractGuidance"]` to inject extra prompt guidance without subclassing.
- Override `_build_generation_user_prompt` to append project-specific instructions while keeping the default OpenAI request, parsing, and normalization flow.
- Override `_generate_contract` when a subclass needs full control over model messages or invocation details.
- Keep any contract post-processing inside `_normalize_contract` or related helpers so the main operation flow stays stable.
- Preserve the JSON-only response contract because downstream consumers expect parseable structured output.

## Example
```python
class SceneContractNode(SpatialTemporalContractNode):
    STEP_ID = "scene_contract"
    TITLE = "Scene Contract"
    DEPENDENCIES = ["scene_outline"]

    def _build_generation_user_prompt(self, request_payload, session_state):
        base_prompt = super()._build_generation_user_prompt(request_payload, session_state)
        return (
            f"{base_prompt}\n\n"
            "Prefer explicit temporal scopes when the scene implies ordered actions."
        )
```