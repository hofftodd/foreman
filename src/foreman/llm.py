"""Streaming chat calls for the planner/reviewer roles.

Two providers:

- ``openai``: any OpenAI-compatible ``/chat/completions`` endpoint. This is how
  local servers (llama.cpp, LM Studio, Ollama, MLX, vLLM) are reached.
- ``anthropic``: Claude through the official Anthropic SDK.

Thinking models served by llama.cpp, vLLM or Ollama stream their reasoning in a
separate delta field; it goes to ``on_reasoning`` and never into the reply text.

With ``research=True`` (the Project Planning Agent only) the model can search and
read the web: Claude through Anthropic's server-side web search/fetch tools, local
models through Foreman's own tools (research.py) when a search service is set.
Each search or fetch is reported to ``on_research``.

Calls stream, and ``should_abort`` is polled while tokens arrive: when it
returns true the stream is closed (which stops generation on the server) and
``Aborted`` is raised. This is how Stop / E-stop cancel an in-flight planner
call (FR-8.2).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx

from foreman import research as web
from foreman.core.contextpack import estimate_tokens
from foreman.core.models import Node

# Claude models that accept server-side refusal fallbacks ("default" routing).
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that take output_config.effort. Planning is the work where quality matters
# most, so it runs at "high" (Opus 5.5 and Haiku 5.5 default to "medium").
_EFFORT_MODELS = {
    "claude-fable-5-1",
    "claude-fable-5",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-5-5",
}
PLANNER_EFFORT = "high"
# Anthropic's server-side research tools (run on Anthropic's side, results inline).
CLAUDE_RESEARCH_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5},
]
MAX_CONTINUATIONS = 5  # resumes of a paused server-tool turn

ABORT_POLL_S = 0.5

# Output budget for Claude. Local nodes get whatever their context window leaves after
# the prompt: a thinking model can reason for tens of thousands of tokens before it
# writes a word, and a fixed cap cuts it off with nothing to show.
ANTHROPIC_MAX_TOKENS = 64000  # streamed; adaptive thinking shares this budget
MIN_OUTPUT_TOKENS = 4096
# The prompt estimate is ~4 chars/token; real tokenizers differ, so keep headroom.
PROMPT_HEADROOM = 1.15
# Delta fields that carry reasoning: llama.cpp/DeepSeek, then vLLM/Ollama/OpenRouter.
REASONING_FIELDS = ("reasoning_content", "reasoning")


class LLMError(RuntimeError):
    pass


class Aborted(Exception):
    """The caller asked to stop while the model was generating."""


@dataclass
class ChatResult:
    text: str
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    max_tokens: int = 0
    reasoning_chars: int = 0

    @property
    def truncated(self) -> bool:
        """Generation hit max_tokens (or the end of the context window)."""
        return self.stop_reason in ("length", "max_tokens")


TextCallback = Callable[[str], None]
AbortCheck = Callable[[], bool]
ResearchCallback = Callable[[str], None]  # e.g. "searched: rust async runtimes"


class _AbortGate:
    """Throttles should_abort (it reads a database) to once per ABORT_POLL_S."""

    def __init__(self, check: AbortCheck | None):
        self.check = check
        self.last = 0.0

    def __call__(self, force: bool = False) -> None:
        if self.check is None:
            return
        now = time.monotonic()
        if force or now - self.last >= ABORT_POLL_S:
            self.last = now
            if self.check():
                raise Aborted()


async def stream_chat(
    node: Node,
    model: str,
    system: str,
    messages: list[dict[str, str]],
    *,
    max_tokens: int | None = None,
    on_text: TextCallback | None = None,
    on_reasoning: TextCallback | None = None,
    should_abort: AbortCheck | None = None,
    research: bool = False,
    on_research: ResearchCallback | None = None,
) -> ChatResult:
    """Run one chat completion, streaming text to ``on_text``.

    ``messages`` alternate user/assistant with plain-text content. ``max_tokens``
    defaults to `output_budget`.
    """
    gate = _AbortGate(should_abort)
    gate(force=True)
    if max_tokens is None:
        max_tokens = output_budget(node, system, messages)
    if node.provider == "anthropic":
        result = await _anthropic(
            node, model, system, messages, max_tokens, on_text, gate, research, on_research
        )
    elif node.provider == "openai":
        result = await _openai(
            node,
            model,
            system,
            messages,
            max_tokens,
            on_text,
            on_reasoning,
            gate,
            research and web.available(),
            on_research,
        )
    else:
        raise LLMError(f"unknown provider {node.provider!r}")
    result.max_tokens = max_tokens
    return result


def output_budget(node: Node, system: str, messages: list[dict[str, str]]) -> int:
    """Output tokens for one call: the rest of a local node's context window."""
    if node.provider == "anthropic":
        return ANTHROPIC_MAX_TOKENS
    prompt = estimate_tokens(system) + sum(estimate_tokens(m["content"]) for m in messages)
    return max(MIN_OUTPUT_TOKENS, node.context_window - int(prompt * PROMPT_HEADROOM))


async def _openai(
    node: Node,
    model: str,
    system: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    on_text: TextCallback | None,
    on_reasoning: TextCallback | None,
    gate: _AbortGate,
    research: bool = False,
    on_research: ResearchCallback | None = None,
) -> ChatResult:
    convo: list[dict[str, Any]] = [{"role": "system", "content": system}, *messages]
    total = ChatResult(text="")
    for _ in range(web.MAX_ROUNDS if research else 1):
        result, calls = await _openai_round(
            node, model, convo, max_tokens, on_text, on_reasoning, gate, research
        )
        total.text += result.text
        total.stop_reason = result.stop_reason
        total.input_tokens += result.input_tokens
        total.output_tokens += result.output_tokens
        total.reasoning_chars += result.reasoning_chars
        if not calls:
            return total
        # Run the research tools and hand the results back for the next round.
        convo.append({"role": "assistant", "content": result.text or None, "tool_calls": calls})
        for call in calls:
            fn = call["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if on_research:
                what = args.get("query") or args.get("url") or ""
                on_research(f"{'searched' if fn['name'] == 'web_search' else 'read'}: {what}")
            output = await web.run_tool(fn["name"], args)
            convo.append({"role": "tool", "tool_call_id": call["id"], "content": output})
    return total


async def _openai_round(
    node: Node,
    model: str,
    convo: list[dict[str, Any]],
    max_tokens: int,
    on_text: TextCallback | None,
    on_reasoning: TextCallback | None,
    gate: _AbortGate,
    research: bool,
) -> tuple[ChatResult, list[dict[str, Any]]]:
    """One streamed completion; returns the result and any tool calls it made."""
    headers = {"Authorization": f"Bearer {node.api_key}"} if node.api_key else {}
    body: dict[str, Any] = {
        "model": model,
        "messages": convo,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if research:
        body["tools"] = web.TOOLS
    parts: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    result = ChatResult(text="")
    # Local models can take minutes to read a long prompt before the first token.
    timeout = httpx.Timeout(connect=10.0, read=900.0, write=60.0, pool=10.0)
    async with (
        httpx.AsyncClient(timeout=timeout) as client,
        client.stream(
            "POST", f"{node.base_url.rstrip('/')}/chat/completions", json=body, headers=headers
        ) as resp,
    ):
        if resp.status_code >= 400:
            detail = (await resp.aread()).decode(errors="replace")[:500]
            raise LLMError(f"{node.name}: HTTP {resp.status_code}: {detail}")
        async for line in resp.aiter_lines():
            gate()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("usage"):
                result.input_tokens = chunk["usage"].get("prompt_tokens", 0) or 0
                result.output_tokens = chunk["usage"].get("completion_tokens", 0) or 0
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                for name in REASONING_FIELDS:
                    thought = delta.get(name)
                    if thought:
                        result.reasoning_chars += len(thought)
                        if on_reasoning:
                            on_reasoning(thought)
                        break
                text = delta.get("content")
                if text:
                    parts.append(text)
                    if on_text:
                        on_text(text)
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(
                        tc.get("index", 0),
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["function"]["name"] += fn.get("name") or ""
                    slot["function"]["arguments"] += fn.get("arguments") or ""
                if choice.get("finish_reason"):
                    result.stop_reason = choice["finish_reason"]
    result.text = "".join(parts)
    tool_calls = [calls[i] for i in sorted(calls)]
    for i, call in enumerate(tool_calls):
        call["id"] = call["id"] or f"call_{i}"
    return result, tool_calls


async def _anthropic(
    node: Node,
    model: str,
    system: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    on_text: TextCallback | None,
    gate: _AbortGate,
    research: bool = False,
    on_research: ResearchCallback | None = None,
) -> ChatResult:
    client = anthropic.AsyncAnthropic(
        api_key=node.api_key or None,
        base_url=node.base_url or None,
        max_retries=2,
    )
    convo: list[dict[str, Any]] = list(messages)
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": convo,
        "thinking": {"type": "adaptive"},
    }
    if research:
        params["tools"] = CLAUDE_RESEARCH_TOOLS
    if model in _EFFORT_MODELS:
        params["output_config"] = {"effort": PLANNER_EFFORT}
    if model in _FALLBACK_MODELS:
        # On a safety decline, re-run on Anthropic's recommended fallback model.
        params["fallbacks"] = "default"
        params["betas"] = [_FALLBACK_BETA]
    total = ChatResult(text="")
    try:
        for _ in range(MAX_CONTINUATIONS + 1):
            async with client.beta.messages.stream(**params) as stream:
                async for text in stream.text_stream:
                    gate()
                    if on_text:
                        on_text(text)
                message = await stream.get_final_message()
            if message.stop_reason == "refusal":
                raise LLMError(f"{model} declined the request")
            for block in message.content:
                if block.type == "server_tool_use" and on_research:
                    args = block.input if isinstance(block.input, dict) else {}
                    what = args.get("query") or args.get("url") or ""
                    on_research(f"{'searched' if block.name == 'web_search' else 'read'}: {what}")
            total.text += "".join(b.text for b in message.content if b.type == "text")
            total.stop_reason = message.stop_reason or ""
            total.input_tokens += message.usage.input_tokens
            total.output_tokens += message.usage.output_tokens
            if message.stop_reason != "pause_turn":
                break
            # A long server-side research turn paused: send it back to resume (no extra
            # user message; the API continues from the trailing tool use).
            convo.append({"role": "assistant", "content": [b.to_dict() for b in message.content]})
    except anthropic.APIConnectionError as e:
        raise LLMError(f"{node.name}: cannot reach the Claude API: {e}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"{node.name}: Claude API error {e.status_code}: {e.message}") from e
    finally:
        await client.close()
    return total


async def list_models(node: Node) -> list[str]:
    """Model ids the node serves (health check)."""
    if node.provider == "anthropic":
        claude = anthropic.AsyncAnthropic(
            api_key=node.api_key or None,
            base_url=node.base_url or None,
            max_retries=0,
            timeout=10.0,
        )
        try:
            page = await claude.models.list(limit=100)
            return [m.id for m in page.data]
        finally:
            await claude.close()
    headers = {"Authorization": f"Bearer {node.api_key}"} if node.api_key else {}
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{node.base_url.rstrip('/')}/models", headers=headers)
        r.raise_for_status()
        return [m.get("id") for m in r.json().get("data", []) if m.get("id")]
