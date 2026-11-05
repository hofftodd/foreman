"""Aider (https://aider.chat) as a one-shot, non-interactive executor.

The run's model gateway is passed as an OpenAI-compatible API base. The files
the task may change are added to the chat (Aider creates the ones that don't
exist yet); the context files go in read-only. Foreman commits the result
itself, so Aider's own commits are off.

Aider is optional in the executor image: build it with
``--build-arg WITH_AIDER=1``. Without it, tasks fail with a configuration
error instead of being retried.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from foreman.core.models import Project, Task
from foreman.executors.base import EXIT_CONFIG, ExecutorContext, ExecutorInvocation

GLOB_CHARS = set("*?[")


def _literal(paths: list[str]) -> list[str]:
    """Plain paths only: Aider takes files, not globs."""
    return [p for p in paths if p and not GLOB_CHARS & set(p) and not p.endswith("/")]


class AiderExecutor:
    kind = "aider"
    needs_node = True
    providers = frozenset({"openai"})
    prompt_overhead_tokens = 2500  # estimated: Aider's edit-format system prompt

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
            raise ValueError("aider executor needs a model endpoint and a model")
        (ctl_dir / "prompt.md").write_text(prompt)
        edit = _literal(task.allowed_globs)
        read = [p for p in _literal(task.context_files) if p not in edit]
        args = [
            "--model",
            f"openai/{model}",
            "--yes-always",
            "--no-auto-commits",
            "--no-dirty-commits",
            "--no-gitignore",
            "--no-check-update",
            "--no-show-model-warnings",
            "--analytics-disable",
            "--no-pretty",
            "--no-stream",
            *[a for f in read for a in ("--read", f)],
            "--",
            *edit,
        ]
        cmd = 'aider --message-file "$FOREMAN_CTL/prompt.md" ' + " ".join(map(shlex.quote, args))
        return ExecutorInvocation(
            command=[
                "bash",
                "-c",
                "command -v aider >/dev/null || { echo 'aider is not installed in this image; "
                "rebuild foreman/executor-base with --build-arg WITH_AIDER=1'; "
                f"exit {EXIT_CONFIG}; }}; "
                f"exec {cmd}",
            ],
            # The gateway adds the node's real key host-side.
            env={"OPENAI_API_BASE": endpoint, "OPENAI_API_KEY": "foreman"},
        )
