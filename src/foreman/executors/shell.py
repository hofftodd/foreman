"""Runs the task's `executor_script` with bash. For scripted tasks and tests.

The script may also be a JSON object with one script per agent role, e.g.
``{"tester": "...", "builder": "..."}``: the task then runs the Tester -> Builder
pipeline with those scripts standing in for the agents.
"""

from __future__ import annotations

import json
from pathlib import Path

from foreman.core.models import Project, Task
from foreman.executors.base import ExecutorContext, ExecutorInvocation


class ShellExecutor:
    kind = "shell"
    prompt_overhead_tokens = 0
    needs_node = False
    providers: frozenset[str] = frozenset()

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
        (ctl_dir / "prompt.md").write_text(prompt)
        scripts = role_scripts(task.executor_script)
        script = scripts.get(role, "true\n") if scripts else task.executor_script
        (ctl_dir / "script.sh").write_text(script or "true\n")
        return ExecutorInvocation(
            command=["bash", "-c", 'exec bash "$FOREMAN_CTL/script.sh"'],
            env={},
        )


def role_scripts(script: str) -> dict[str, str]:
    """Per-role scripts, if the task's script is a {"tester": ..., "builder": ...} map."""
    text = (script or "").strip()
    if not text.startswith("{"):
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict) or not ({"tester", "builder"} & set(data)):
        return {}
    return {str(k): str(v) for k, v in data.items()}
