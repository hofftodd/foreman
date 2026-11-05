from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from foreman.core.models import Project, Task, TaskState
from foreman.db import session


def make_project(client: Any, origin: Any, **kw: Any) -> dict[str, Any]:
    body = {
        "name": "demo",
        "repo_url": str(origin),
        "executor_kind": "shell",
        "skip_spec": True,
        # M1-style behaviour: a failed attempt just fails.
        "watchdog": {"escalation": False},
        **kw,
    }
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def make_task(
    client: Any, project_id: int, script: str, validation: str = "true", **kw: Any
) -> dict[str, Any]:
    """A scripted (shell) task; for agent projects, an agent task with criteria instead
    (agents' tests come from the Tester, not a command)."""
    kind = kw.get("executor_kind") or client.get(f"/api/projects/{project_id}").json().get(
        "executor_kind"
    )
    body: dict[str, Any] = {"title": kw.pop("title", "t"), **kw}
    if kind == "shell":
        body |= {"executor_script": script, "validation_cmd": validation}
    else:
        body.setdefault("criteria", ["it works"])
        body.pop("allowed_globs", None)
    r = client.post(f"/api/projects/{project_id}/tasks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def task(task_id: int) -> Task:
    with session() as s:
        t = s.get(Task, task_id)
        assert t is not None
        return t


def project(project_id: int) -> Project:
    with session() as s:
        p = s.get(Project, project_id)
        assert p is not None
        return p


TERMINAL = {
    TaskState.DONE,
    TaskState.FAILED,
    TaskState.STOPPED_BY_USER,
    TaskState.PAUSED,
    TaskState.DISCARDED,
}


async def drive(
    orch: Any, until: Callable[[], bool], timeout: float = 30, settle: bool = True
) -> None:
    """Tick an in-process orchestrator until `until()` is true.

    With `settle`, also wait for in-flight runners to finish their bookkeeping.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await orch.tick()
        if until():
            if settle:
                pending = [*orch.running.values(), *getattr(orch, "jobs", {}).values()]
                await asyncio.gather(*pending, return_exceptions=True)
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting for condition")


def wait_for(pred: Callable[[], bool], timeout: float = 20, interval: float = 0.1) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(interval)
    raise AssertionError("timed out waiting for condition")
