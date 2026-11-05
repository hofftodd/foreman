"""Executor adapters: what they write to the control dir and how they read output."""

from __future__ import annotations

import json

from foreman.core.contextpack import budget_tokens
from foreman.core.models import Project, Task
from foreman.executors import EXECUTORS, ExecutorContext
from foreman.executors.pi import PiExecutor, PiOutput


def test_pi_prepare(tmp_path):
    inv = PiExecutor().prepare(
        Project(name="p", repo_url="x"),
        Task(project_id=1, title="t"),
        "http://foreman-gw:8701/g/tok/v1",
        "qwen3-coder",
        tmp_path,
        "# Task T1\n",
        ExecutorContext(context_window=32768),
    )
    assert (tmp_path / "prompt.md").read_text() == "# Task T1\n"
    cfg = json.loads((tmp_path / "pi" / "models.json").read_text())
    provider = cfg["providers"]["foreman"]
    assert provider["baseUrl"] == "http://foreman-gw:8701/g/tok/v1"
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"] == "foreman"  # never a real key: the gateway adds it
    assert provider["models"] == [
        {"id": "qwen3-coder", "name": "qwen3-coder", "maxTokens": 8192, "contextWindow": 32768}
    ]
    script = inv.command[-1]
    for flag in ("--mode json", "--no-session", "--offline", "--no-context-files", "--no-approve"):
        assert flag in script
    assert 'PI_CODING_AGENT_DIR="$HOME/.pi/agent"' in script
    assert inv.env == {"FOREMAN_MODEL": "qwen3-coder"}
    assert inv.output is not None


def _events(*evs: dict) -> list[str]:
    return [json.dumps(e) for e in evs]


def test_pi_output_renders_and_succeeds():
    out = PiOutput()
    lines = _events(
        {"type": "session", "id": "x"},
        {"type": "tool_execution_start", "toolName": "bash", "args": {"command": "ls"}},
        {
            "type": "tool_execution_end",
            "toolName": "bash",
            "result": {"content": [{"type": "text", "text": "a\nb\n"}]},
            "isError": False,
        },
        {
            "type": "tool_execution_start",
            "toolName": "write",
            "args": {"path": "f", "content": "x"},
        },
        {
            "type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
        },
        {"type": "agent_settled", "aborted": False},
    )
    shown = [r for r in (out.render(line) for line in lines) if r is not None]
    assert shown == ["$ ls", "a\nb", "write f (1 lines)", "Done."]
    assert out.failure() is None
    assert out.render("plain stderr line") == "plain stderr line"


def test_pi_output_reports_model_errors_despite_exit_0():
    out = PiOutput()
    for line in _events(
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
                                            "errorMessage": "400: budget exceeded"}},
        {"type": "agent_settled", "aborted": False},
    ):  # fmt: skip
        out.render(line)
    assert out.failure() == "pi: 400: budget exceeded"
    assert PiOutput().failure() == "pi ended without finishing its run"


def test_budget_subtracts_executor_overhead():
    p = Project(name="p", repo_url="x", context_budget_pct=50)
    assert budget_tokens(p, 32000, "shell") == 16000
    assert budget_tokens(p, 32000, "pi") == (32000 - 1500) // 2
    assert budget_tokens(p, 32000, "opencode") == (32000 - 7500) // 2
    assert budget_tokens(p, 32000, "pi") > budget_tokens(p, 32000, "opencode")
    assert set(EXECUTORS) == {"pi", "opencode", "aider", "shell"}
