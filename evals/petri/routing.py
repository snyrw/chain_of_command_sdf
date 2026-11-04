"""Model roles and OpenRouter provider pinning for the Petri audits.

Prices are $/M tokens (input/output) from OpenRouter's /models/<id>/endpoints on 2026-11-01; recheck before a big run.

Target, Kimi K2.6. Moonshot ships K2.6 in native INT4, so INT4 endpoints serve the weights as released; fp8/fp4/bf16
endpoints are re-quantizations of them and cost more. Cheapest INT4 first:
    chutes/int4 0.39/1.80 -> inceptron/int4 0.68/3.50 -> parasail/int4 0.75/3.50 -> venice/int4 0.75/3.50
The `quantizations` filter keeps a failover from landing on an "unknown"-precision endpoint.

Auditor + judge, GLM 5.2. Z.AI serves fp8 natively (z-ai/fp8, 1.40/4.40), so fp8 is the quality floor here; the fp4
endpoints (wafer, relace, deepinfra, ...) are cheaper but off that floor and excluded by the filter. Cheapest fp8 first:
    morph/fp8 0.07/1.40 -> streamlake/fp8 0.56/1.75 -> novita/fp8 0.65/2.04 -> siliconflow/fp8 0.70/2.20
Morph is the cheapest by a wide margin, including its cache-read price (0.069), so prompt caching buys nothing there. It
is a small provider with no track record in this repo: smoke-test one audit before a full run (see README).

`allow_fallbacks` stays on so one provider's outage doesn't kill a multi-hour sweep; `quantizations` bounds what a
fallback can be. Do NOT add `require_parameters`: the judge (inspect_scout) always sends `parallel_tool_calls=False`,
which no OpenRouter endpoint advertises, so it 404s with "No endpoints found" at the "Filter by Parameters" step. Every
endpoint listed above does advertise `tools` and `reasoning`, which is what the audits need.
"""

from inspect_ai.model import GenerateConfig, Model, get_model

TARGET_MODEL = "openrouter/moonshotai/kimi-k2.6"
AUDIT_MODEL = "openrouter/z-ai/glm-5.2"

TARGET_PROVIDER = {
    "order": ["chutes/int4", "inceptron/int4", "parasail/int4", "venice/int4"],
    "allow_fallbacks": True,
    "quantizations": ["int4"],
}

GLM_PROVIDER = {
    "order": ["morph/fp8", "streamlake/fp8", "novita/fp8", "siliconflow/fp8"],
    "allow_fallbacks": True,
    "quantizations": ["fp8", "bf16"],
}

# Shown to the auditor as the target's name. The notice inside the target's context says "Kimi K2.6" too.
TARGET_DISPLAY_NAME = "Kimi K2.6"


def target_model(temperature: float = 1.0, max_tokens: int = 16384) -> Model:
    """Kimi K2.6 in thinking mode (the default), so the judge and you can read its reasoning."""
    return get_model(
        TARGET_MODEL,
        config=GenerateConfig(temperature=temperature, max_tokens=max_tokens),
        provider=TARGET_PROVIDER,
    )


def auditor_model(reasoning_effort: str = "medium") -> Model:
    return get_model(
        AUDIT_MODEL,
        config=GenerateConfig(reasoning_effort=reasoning_effort, max_tokens=16384),
        provider=GLM_PROVIDER,
    )


def judge_model(reasoning_effort: str = "high") -> Model:
    return get_model(
        AUDIT_MODEL,
        config=GenerateConfig(reasoning_effort=reasoning_effort, max_tokens=32768),
        provider=GLM_PROVIDER,
    )


def model_roles(
    *,
    auditor_effort: str = "medium",
    judge_effort: str = "high",
    realism: bool = False,
    temperature: float = 1.0,
) -> dict[str, Model]:
    roles = {
        "auditor": auditor_model(auditor_effort),
        "target": target_model(temperature),
        "judge": judge_model(judge_effort),
    }
    if realism:
        roles["realism"] = auditor_model("low")
    return roles
