"""Executor interface: turns a task into a command plus control files.

Executors write whatever they need (prompt, tool config) into the run's
control directory, which is mounted read-only into the container at
$FOREMAN_CTL, and return the command to run in the task clone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from foreman.core.models import Project, Task

# Exit status an executor's command uses for "this can't work as configured"
# (e.g. its tool isn't installed): the task blocks instead of being retried.
EXIT_CONFIG = 78


@dataclass(frozen=True)
class ExecutorContext:
    """What an executor may want to know about the model it runs against."""

    context_window: int | None = None
    max_output_tokens: int | None = None


class OutputParser(Protocol):
    """Turns an executor's raw stdout into what the dashboard shows, and spots a run
    that failed even though the process exited 0. One instance per run."""

    def render(self, line: str) -> str | None:
        """The text to show for one raw line, or None to show nothing."""
        ...

    def failure(self) -> str | None:
        """Why the run failed, if its output says so; checked after a 0 exit."""
        ...


@dataclass
class ExecutorInvocation:
    command: list[str]
    env: dict[str, str]
    # None: show output as-is and trust the exit status.
    output: OutputParser | None = field(default=None)


class Executor(Protocol):
    kind: str
    needs_node: bool
    # Tokens the agent itself puts in every model request (system prompt, tool schemas),
    # measured through the gateway. Comes off the window before the context budget.
    prompt_overhead_tokens: int
    # Node providers whose API the executor's tool can speak (through the gateway).
    providers: frozenset[str]

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
        """`endpoint` is the base URL the executor must use (the run's model gateway
        URL, ending in /v1); it carries no credentials. `provider` says which API it
        speaks: "openai" (chat completions) or "anthropic" (Claude Messages). `role` is
        the agent being run ("tester" / "builder"; "" for a single-step scripted task).
        `context` describes the model's window and output limit."""
        ...
