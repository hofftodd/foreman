"""pi (https://pi.dev) in non-interactive JSON mode.

pi is a small coding agent: four tools (read, bash, edit, write) and a short system
prompt, about 1.5k tokens of overhead per request against ~7.5k for OpenCode, so
more of a local model's window is left for the task.

The run's model gateway URL is registered as a custom OpenAI-compatible provider
named "foreman" in a generated `models.json`. pi reads that from its agent
directory, which must be writable, so the command copies it from the read-only
control dir into $HOME first.

Everything pi might discover on its own is switched off: no sessions on disk, no
extensions, skills, prompt templates or MCP, no AGENTS.md / CLAUDE.md (the context
pack is what the agent sees, and it is measured against the budget), no
project-local configuration and no startup network calls. pi has no permission
system: the container is the sandbox.

pi exits 0 even when a model call fails, so the output parser reads its JSON event
stream for the outcome.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from foreman.core.models import Project, Task
from foreman.executors.base import ExecutorContext, ExecutorInvocation

PROVIDER = "foreman"
DEFAULT_MAX_OUTPUT = 8192
TOOL_RESULT_LINES = 12

FLAGS = [
    "--mode", "json",
    "--no-session",
    "--offline",
    "--no-extensions",
    "--no-mcp",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--no-context-files",
    "--no-approve",
]  # fmt: skip


def models_config(endpoint: str, model: str, context: ExecutorContext | None) -> dict:
    window = context.context_window if context and context.context_window else None
    max_out = context.max_output_tokens if context and context.max_output_tokens else None
    if max_out is None:
        max_out = min(DEFAULT_MAX_OUTPUT, window // 4) if window else DEFAULT_MAX_OUTPUT
    entry: dict[str, Any] = {"id": model, "name": model, "maxTokens": max_out}
    if window:
        entry["contextWindow"] = window
    return {
        "$schema": "https://pi.dev/schemas/models.schema.json",
        "providers": {
            PROVIDER: {
                "name": "foreman gateway",
                "baseUrl": endpoint,
                "api": "openai-completions",
                # No real key: the gateway adds the node's key on the host side.
                "apiKey": "foreman",
                "models": [entry],
            }
        },
    }


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _clip(text: str, lines: int = TOOL_RESULT_LINES) -> str:
    rows = text.rstrip("\n").splitlines()
    if len(rows) <= lines:
        return "\n".join(rows)
    return "\n".join([*rows[:lines], f"... ({len(rows) - lines} more lines)"])


def _describe_call(name: str, args: Any) -> str:
    if not isinstance(args, dict):
        return name
    if name == "bash":
        return f"$ {args.get('command', '')}"
    path = args.get("path", "")
    if name == "edit":
        return f"edit {path} ({len(args.get('edits') or [])} change(s))"
    if name == "write":
        return f"write {path} ({len(str(args.get('content', '')).splitlines())} lines)"
    if name == "read":
        return f"read {path}"
    return f"{name} {json.dumps(args)[:200]}"


class PiOutput:
    """Renders pi's JSONL events as a readable log and remembers how the run ended."""

    def __init__(self) -> None:
        self._error: str | None = None
        self._settled = False

    def render(self, line: str) -> str | None:
        if not line.startswith("{"):
            return line  # stderr diagnostics are merged into the stream
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return line
        kind = ev.get("type")
        if kind == "message_end":
            msg = ev.get("message") or {}
            if msg.get("role") != "assistant":
                return None
            if msg.get("stopReason") == "error":
                self._error = str(msg.get("errorMessage") or "model call failed")
                return f"model error: {self._error}"
            self._error = None  # a later successful turn supersedes an earlier error
            text = _text(msg.get("content")).strip()
            return text or None
        if kind == "tool_execution_start":
            return _describe_call(str(ev.get("toolName", "")), ev.get("args"))
        if kind == "tool_execution_end":
            result = _text((ev.get("result") or {}).get("content"))
            if ev.get("isError"):
                return f"[{ev.get('toolName')} failed]\n{_clip(result)}"
            return _clip(result) if ev.get("toolName") == "bash" and result.strip() else None
        if kind == "auto_retry_start":
            return f"retrying model call (attempt {ev.get('attempt')}): {ev.get('errorMessage')}"
        if kind == "auto_retry_end" and not ev.get("success"):
            self._error = str(ev.get("finalError") or "model call failed after retries")
            return f"model error: {self._error}"
        if kind == "compaction_start":
            return f"compacting context ({ev.get('reason')})"
        if kind == "agent_settled":
            self._settled = True
            if ev.get("aborted"):
                self._error = self._error or "pi aborted the run"
            return None
        return None

    def failure(self) -> str | None:
        if self._error:
            return f"pi: {self._error}"
        if not self._settled:
            return "pi ended without finishing its run"
        return None


class PiExecutor:
    kind = "pi"
    needs_node = True
    prompt_overhead_tokens = 1500  # measured: ~1.4k (system prompt + 4 tool schemas)
    providers = frozenset({"openai"})  # pi talks to the gateway's chat completions API

    def prepare(
        self,
        project: Project,
        task: Task,
        endpoint: str,
        model: str,
        ctl_dir: Path,
        prompt: str,
        context: ExecutorContext | None = None,
        provider: str = "openai",
        role: str = "",
    ) -> ExecutorInvocation:
        if not endpoint or not model:
            raise ValueError("pi executor needs a model endpoint and a model")
        (ctl_dir / "prompt.md").write_text(prompt)
        (ctl_dir / "pi").mkdir(exist_ok=True)
        (ctl_dir / "pi" / "models.json").write_text(
            json.dumps(models_config(endpoint, model, context), indent=2)
        )
        flags = " ".join(FLAGS)
        return ExecutorInvocation(
            command=[
                "bash",
                "-c",
                'mkdir -p "$HOME/.pi/agent" && cp "$FOREMAN_CTL/pi/models.json" "$HOME/.pi/agent/" '
                '&& export PI_CODING_AGENT_DIR="$HOME/.pi/agent" '
                f'&& exec pi {flags} --provider {PROVIDER} --model "$FOREMAN_MODEL" -- '
                '"$(cat "$FOREMAN_CTL/prompt.md")"',
            ],
            env={"FOREMAN_MODEL": model},
            output=PiOutput(),
        )
