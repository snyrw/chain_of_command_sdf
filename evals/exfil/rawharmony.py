"""Inspect model provider that sends gpt-oss a hand-rendered harmony prompt via OpenRouter's /completions.

Why: the chat-completions route applies gpt-oss's chat template server-side, which always prepends a fixed
"You are ChatGPT..." system message and files our system prompt under developer "# Instructions". Here the prompt is
rendered locally with the same tinker renderer SDF training uses (`gpt_oss_no_sysprompt`), so the format can match
training, and the raw completion is split into the analysis channel (stored as reasoning) and the final channel.

    get_model("rawharmony/openai/gpt-oss-120b", header="none"|"structural"|"default", provider="deepinfra/bf16")
    (task.py does this; forward with -T header=... -T provider=...)

`header` is what goes before the developer message:
  none        nothing: the exact training format (no system message at all)
  structural  a system message with only the reasoning level and channel list, no identity line
  default     the stock "You are ChatGPT..." header (tinker's gpt_oss_<effort>_reasoning renderer)
Reasoning level (low|medium|high) comes from --reasoning-effort and is only expressible in the last two modes.

The provider must return special tokens (<|channel|> ...) in the completion text. DeepInfra, Cerebras and CoreWeave do;
some others strip them or return only the final answer, in which case this raises instead of guessing.
"""

import os
import re
from typing import Any

import httpx
from inspect_ai.model import (
    ChatMessage,
    ContentReasoning,
    ContentText,
    GenerateConfig,
    ModelAPI,
    ModelCall,
    ModelOutput,
    ModelUsage,
    modelapi,
)
from inspect_ai.model._model import RetryDecision
from inspect_ai.tool import ToolChoice, ToolInfo
from tinker_cookbook import renderers
from tinker_cookbook.tokenizer_utils import get_tokenizer

URL = "https://openrouter.ai/api/v1/completions"
HF_MODEL = "openai/gpt-oss-120b"
HEADERS = ("none", "structural", "default")

_CHANNEL = re.compile(r"<\|channel\|>(\w+)<\|message\|>(.*?)(?=<\|end\|>|<\|start\|>|<\|return\|>|<\|call\|>|<\|channel\|>|$)", re.S)


class RawHarmonyError(Exception):
    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


def split_channels(text: str) -> tuple[str, str]:
    """(analysis, final) from raw completion text that starts right after '<|start|>assistant'."""
    if "<|channel|>" not in text:
        raise RawHarmonyError(None, "completion has no <|channel|> markers: this provider strips special tokens or "
                              f"drops the reasoning. Start of text: {text[:120]!r}")
    parts = _CHANNEL.findall(text)
    analysis = "\n".join(body.strip() for ch, body in parts if ch in ("analysis", "commentary"))
    final = "\n".join(body.strip() for ch, body in parts if ch == "final")
    return analysis, final


@modelapi(name="rawharmony")
class RawHarmonyAPI(ModelAPI):
    def __init__(
        self,
        model_name: str,
        base_url: str | None = None,
        api_key: str | None = None,
        config: GenerateConfig = GenerateConfig(),
        header: str = "default",
        provider: str = "deepinfra/bf16",
        **model_args: Any,
    ) -> None:
        super().__init__(model_name, base_url, api_key, ["OPENROUTER_API_KEY"], config)
        if header not in HEADERS:
            raise ValueError(f"header must be one of {HEADERS}, got {header!r}")
        if model_args:
            raise ValueError(f"unknown model args: {sorted(model_args)}")
        self.header, self.provider = header, provider
        self.api_key = self.api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is not set")
        self.tokenizer = get_tokenizer(HF_MODEL)
        self.body_renderer = renderers.get_renderer("gpt_oss_no_sysprompt", self.tokenizer)

    def render(self, input: list[ChatMessage], effort: str) -> str:
        messages = [{"role": m.role, "content": m.text} for m in input]
        body = self.tokenizer.decode(self.body_renderer.build_generation_prompt(messages).to_ints())
        if self.header == "none":
            return body
        if self.header == "structural":
            return (f"<|start|>system<|message|>Reasoning: {effort}\n\n# Valid channels: analysis, commentary, final. "
                    f"Channel must be included for every message.<|end|>{body}")
        full = renderers.get_renderer(f"gpt_oss_{effort}_reasoning", self.tokenizer)
        return self.tokenizer.decode(full.build_generation_prompt(messages).to_ints())

    async def generate(
        self, input: list[ChatMessage], tools: list[ToolInfo], tool_choice: ToolChoice, config: GenerateConfig
    ) -> tuple[ModelOutput, ModelCall]:
        if tools:
            raise ValueError("rawharmony does not support tools")
        effort = config.reasoning_effort or "medium"
        if effort not in ("low", "medium", "high"):
            raise ValueError(f"reasoning_effort must be low|medium|high, got {effort!r}")
        request: dict[str, Any] = {
            "model": self.model_name,
            "prompt": self.render(input, effort),
            "max_tokens": config.max_tokens or 8000,
            "provider": {"order": [self.provider], "allow_fallbacks": False},
        }
        if config.temperature is not None:
            request["temperature"] = config.temperature
        if config.top_p is not None:
            request["top_p"] = config.top_p
        if config.seed is not None:
            request["seed"] = config.seed

        async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
            resp = await client.post(URL, json=request, headers={"Authorization": f"Bearer {self.api_key}"})
        if resp.status_code != 200:
            raise RawHarmonyError(resp.status_code, f"OpenRouter {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        choice = data["choices"][0]
        analysis, final = split_channels(choice.get("text") or "")

        content = ([ContentReasoning(reasoning=analysis)] if analysis else []) + [ContentText(text=final)]
        output = ModelOutput.from_content(
            model=self.model_name, content=content,
            stop_reason="max_tokens" if choice.get("finish_reason") == "length" else "stop",
        )
        if usage := data.get("usage"):
            output.usage = ModelUsage(
                input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            )
        return output, ModelCall.create(request=request, response=data)

    def should_retry(self, ex: Exception) -> bool | RetryDecision:
        if isinstance(ex, RawHarmonyError):
            return ex.status in (408, 429, 500, 502, 503, 504)
        return isinstance(ex, httpx.TransportError)

    def connection_key(self) -> str:
        return f"rawharmony/{self.provider}"
