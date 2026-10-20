"""Parse Claude Code ``--output-format stream-json`` events into per-agent stats.

Cost while the agent runs is an estimate from each assistant message's ``usage`` (deduplicated by
message id, since Claude Code emits one event per content block with the same usage). When the
``result`` event arrives, its ``total_cost_usd`` and ``modelUsage`` replace the estimate: they also
include Claude Code's own background calls (a small Haiku call per session), which the
per-message usage does not show.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# USD per million tokens: input, output, cache read, 5-minute cache write, 1-hour cache write.
# Sonnet 5.5 figures reproduce Claude Code's own costUSD exactly (checked against a probe run).
PRICES = {
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.50, 4.00),
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.50, 4.00),
    "claude-opus-5-5": (4.0, 20.0, 0.40, 5.00, 8.00),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25, 2.00),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125, 0.20),
}
# Unknown models are priced as the most expensive known one, so the budget guard errs on the safe side.
FALLBACK = max(PRICES.values())


def price_for(model: str | None) -> tuple[float, ...]:
    name = (model or "").split("[", 1)[0]
    for key in sorted(PRICES, key=len, reverse=True):
        if name.startswith(key):
            return PRICES[key]
    return FALLBACK


def usage_cost(model: str | None, usage: dict) -> float:
    p_in, p_out, p_read, p_w5, p_w1h = price_for(model)
    cache = usage.get("cache_creation") or {}
    w1h = int(cache.get("ephemeral_1h_input_tokens") or 0)
    w5 = int(cache.get("ephemeral_5m_input_tokens") or 0)
    created = int(usage.get("cache_creation_input_tokens") or 0)
    if w1h + w5 < created:
        w5 += created - w1h - w5
    return (
        int(usage.get("input_tokens") or 0) * p_in
        + int(usage.get("output_tokens") or 0) * p_out
        + int(usage.get("cache_read_input_tokens") or 0) * p_read
        + w5 * p_w5 + w1h * p_w1h
    ) / 1_000_000


TOKEN_KEYS = ("input", "output", "cache_read", "cache_creation")


@dataclass
class StreamStats:
    model: str | None = None
    session_id: str | None = None
    mcp_servers: list[dict] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    messages: dict[str, tuple[str | None, dict]] = field(default_factory=dict)   # id -> (model, usage)
    tool_calls: list[dict] = field(default_factory=list)
    last_text: str = ""
    result: dict | None = None
    api_errors: list[str] = field(default_factory=list)
    events: int = 0
    bad_lines: int = 0
    _pending: dict[str, dict] = field(default_factory=dict)

    def feed_line(self, line: str) -> dict | None:
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self.bad_lines += 1
            return None
        if isinstance(event, dict):
            self.feed(event)
            return event
        return None

    def feed(self, event: dict) -> None:
        self.events += 1
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self.model = event.get("model") or self.model
            self.session_id = event.get("session_id")
            self.mcp_servers = list(event.get("mcp_servers") or [])
            self.tools = list(event.get("tools") or [])
        elif kind == "assistant":
            message = event.get("message") or {}
            if event.get("error"):
                self.api_errors.append(str(event.get("error")))
            mid = message.get("id") or f"anon-{self.events}"
            usage = message.get("usage")
            model = message.get("model")
            if isinstance(usage, dict) and model != "<synthetic>":
                self.messages[mid] = (model or self.model, usage)
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    self.last_text = block["text"]
                elif block.get("type") == "tool_use":
                    row = {"name": block.get("name") or "", "input": block.get("input"), "result": "",
                           "is_error": False}
                    self._pending[block.get("id") or ""] = row
                    self.tool_calls.append(row)
        elif kind == "user":
            message = event.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    row = self._pending.pop(block.get("tool_use_id") or "", None)
                    if row is None:
                        continue
                    row["result"] = _text(block.get("content"))[:6000]
                    row["is_error"] = bool(block.get("is_error"))
        elif kind == "result":
            self.result = event

    # -- derived numbers ---------------------------------------------------------------------

    def estimated_cost(self) -> float:
        return sum(usage_cost(model, usage) for model, usage in self.messages.values())

    def cost(self) -> float:
        if self.result is not None and isinstance(self.result.get("total_cost_usd"), (int, float)):
            return float(self.result["total_cost_usd"])
        return self.estimated_cost()

    def cost_source(self) -> str:
        if self.result is not None and isinstance(self.result.get("total_cost_usd"), (int, float)):
            return "result"
        return "estimate"

    def tokens(self) -> dict[str, int]:
        if self.result is not None and isinstance(self.result.get("modelUsage"), dict):
            out = dict.fromkeys(TOKEN_KEYS, 0)
            for row in self.result["modelUsage"].values():
                out["input"] += int(row.get("inputTokens") or 0)
                out["output"] += int(row.get("outputTokens") or 0)
                out["cache_read"] += int(row.get("cacheReadInputTokens") or 0)
                out["cache_creation"] += int(row.get("cacheCreationInputTokens") or 0)
            if any(out.values()):
                return out
        out = dict.fromkeys(TOKEN_KEYS, 0)
        for _model, usage in self.messages.values():
            out["input"] += int(usage.get("input_tokens") or 0)
            out["output"] += int(usage.get("output_tokens") or 0)
            out["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
            out["cache_creation"] += int(usage.get("cache_creation_input_tokens") or 0)
        return out

    def turns(self) -> int:
        if self.result is not None and isinstance(self.result.get("num_turns"), int):
            return int(self.result["num_turns"])
        return len(self.messages)

    def final_text(self) -> str:
        if self.result is not None and isinstance(self.result.get("result"), str):
            return self.result["result"]
        return self.last_text

    def auth_failed(self) -> bool:
        if any("auth" in err for err in self.api_errors):
            return True
        text = (self.final_text() or "").lower()
        return self.result is not None and bool(self.result.get("is_error")) and "authenticate" in text

    def mcp_status(self, name: str) -> str | None:
        for server in self.mcp_servers:
            if server.get("name") == name:
                return server.get("status")
        return None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") if part.get("text") is not None else part))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return "" if content is None else str(content)
