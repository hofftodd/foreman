"""OpenCode (https://opencode.ai) in non-interactive `run` mode.

For OpenAI-compatible nodes the run's model gateway URL is registered as a
provider named "foreman" in a generated config file. For Claude nodes,
OpenCode's built-in Anthropic provider is pointed at the gateway instead, which
adds the real API key on the host. The prompt is passed as the run message.
"""

from __future__ import annotations

import json
from pathlib import Path

from foreman.core.models import Project, Task
from foreman.executors.base import ExecutorContext, ExecutorInvocation

PROVIDER = "foreman"


def model_ref(model: str, provider: str = "openai") -> str:
    return f"anthropic/{model}" if provider == "anthropic" else f"{PROVIDER}/{model}"


def opencode_config(endpoint: str, model: str, provider: str = "openai") -> dict:
    # No real API key: the gateway adds the node's key on the host side.
    options: dict[str, str] = {"baseURL": endpoint, "apiKey": "foreman"}
    if provider == "anthropic":
        providers: dict = {"anthropic": {"options": options}}
    else:
        providers = {
            PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "foreman gateway",
                "options": options,
                "models": {model: {"name": model}},
            }
        }
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": providers,
        # Only the provider Foreman configured; nothing else is reachable anyway.
        "enabled_providers": [next(iter(providers))],
        "model": model_ref(model, provider),
        "autoupdate": False,
        "share": "disabled",
        # Unattended run inside a sandbox: allow edits and shell, nothing from the web.
        "permission": {"edit": "allow", "bash": "allow", "webfetch": "deny"},
    }


class OpenCodeExecutor:
    kind = "opencode"
    prompt_overhead_tokens = 7500  # measured: ~7.4k (system prompt + 9 tool schemas)
    needs_node = True
    providers = frozenset({"openai", "anthropic"})

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
            raise ValueError("opencode executor needs a model endpoint and a model")
        (ctl_dir / "prompt.md").write_text(prompt)
        (ctl_dir / "opencode.json").write_text(
            json.dumps(opencode_config(endpoint, model, provider), indent=2)
        )
        return ExecutorInvocation(
            command=[
                "bash",
                "-c",
                'export OPENCODE_CONFIG="$FOREMAN_CTL/opencode.json"; '
                'exec opencode run --model "$FOREMAN_MODEL" "$(cat "$FOREMAN_CTL/prompt.md")"',
            ],
            env={"FOREMAN_MODEL": model_ref(model, provider)},
        )
