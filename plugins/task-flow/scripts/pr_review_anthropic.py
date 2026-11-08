# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
"""Anthropic Messages adapter; request fields/prices checked 2026-11-08.

Sources: https://platform.claude.com/docs/en/api/messages/create
https://platform.claude.com/docs/en/about-claude/pricing
No tools, caching, streaming, retries, regional routing or extended thinking.
"""

import json
from typing import ClassVar


class Anthropic:
    # Dated snapshot only; no moving model alias. USD per million standard tokens.
    models: ClassVar = {"claude-haiku-4-5-20251001": (1.0, 5.0)}
    hosts = frozenset({"api.anthropic.com"})
    api_version = "2026-10-21"

    def request(
        self, model: str, system: str, data: str, output: int
    ) -> tuple[str, dict]:
        if model not in self.models:
            raise ValueError("unsupported pinned model")
        return "https://api.anthropic.com/v1/messages", {
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": data}],
            "max_tokens": output,
            "stream": False,
        }

    def headers(self, secret: str) -> dict[str, str]:
        return {
            "x-api-key": secret,
            "anthropic-version": self.api_version,
        }

    def parse(self, response: dict) -> tuple[str, dict]:
        if not isinstance(response, dict):
            raise TypeError("invalid provider envelope")
        if (
            response.get("type") != "message"
            or response.get("role") != "assistant"
            or response.get("model") not in self.models
            or response.get("stop_reason") != "end_turn"
            or response.get("stop_details")
            or not isinstance(response.get("content"), list)
            or not response["content"]
            or any(
                not isinstance(b, dict)
                or b.get("type") != "text"
                or not isinstance(b.get("text"), str)
                for b in response["content"]
            )
        ):
            raise ValueError("unavailable provider output")
        raw = response.get("usage", {})
        if not isinstance(raw, dict):
            raise TypeError("invalid usage")
        # Extra nonzero token categories cannot silently become a known zero cost.
        known = {"input_tokens", "output_tokens"}
        neutral = {
            "inference_geo": (None, "global"),
            "service_tier": (None, "standard"),
            "speed": (None, "standard"),
        }

        def zero(value: object) -> bool:
            return (
                value is None
                or type(value) is int
                and value == 0
                or isinstance(value, dict)
                and all(zero(v) for v in value.values())
            )

        extras = {
            k: v
            for k, v in raw.items()
            if k not in known and not zero(v) and not (k in neutral and v in neutral[k])
        }
        usage = {}
        for key in known:
            value = raw.get(key)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("invalid usage")
            usage[key] = value
        if extras:
            usage["cost_known"] = False
        # Ensure response text is concatenated without injecting separators into JSON.
        text = "".join(b["text"] for b in response["content"])
        json.loads(text)
        return text, usage
