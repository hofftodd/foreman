from foreman.executors.aider import AiderExecutor
from foreman.executors.base import (
    EXIT_CONFIG,
    Executor,
    ExecutorContext,
    ExecutorInvocation,
    OutputParser,
)
from foreman.executors.opencode import OpenCodeExecutor
from foreman.executors.pi import PiExecutor
from foreman.executors.shell import ShellExecutor

EXECUTORS: dict[str, Executor] = {
    e.kind: e for e in (PiExecutor(), OpenCodeExecutor(), AiderExecutor(), ShellExecutor())
}


def get_executor(kind: str) -> Executor:
    try:
        return EXECUTORS[kind]
    except KeyError:
        raise ValueError(f"unknown executor {kind!r}; known: {sorted(EXECUTORS)}") from None


__all__ = [
    "EXECUTORS",
    "EXIT_CONFIG",
    "Executor",
    "ExecutorContext",
    "ExecutorInvocation",
    "OutputParser",
    "get_executor",
]
