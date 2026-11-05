"""Task execution, in-process orchestrator with the local runtime."""

from sqlmodel import select

from foreman import gitops
from foreman.core.models import Event, RunState, TaskRun, TaskState
from foreman.db import session
from foreman.orchestrator.main import Orchestrator, recover
from tests.helpers import TERMINAL, drive, make_project, make_task, task


def _runs(task_id):
    with session() as s:
        return s.exec(select(TaskRun).where(TaskRun.task_id == task_id)).all()


async def test_task_runs_validates_and_merges(client, origin):
    p = make_project(client, origin, test_cmd="test -f README.md")
    t = make_task(
        client,
        p["id"],
        script="echo working; echo 'VALUE = 2' > src/app.py",
        validation="grep -q 'VALUE = 2' src/app.py",
        allowed_globs=["src/*"],
    )
    orch = Orchestrator()
    await drive(orch, lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.DONE, task(t["id"]).reason
    repo = gitops.repo_dir(p["id"])
    assert (repo / "src" / "app.py").read_text() == "VALUE = 2\n"
    (run,) = _runs(t["id"])
    assert run.state == RunState.SUCCEEDED and run.handle.startswith("local:")
    with session() as s:
        logs = [e.message for e in s.exec(select(Event).where(Event.task_id == t["id"]))]
    assert any("working" in m for m in logs)
    assert any("validate passed" in m for m in logs)


async def test_failed_validation_fails_task_and_does_not_merge(client, origin):
    p = make_project(client, origin)
    t = make_task(client, p["id"], script="echo 'VALUE = 3' > src/app.py", validation="false")
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    tk = task(t["id"])
    assert tk.state == TaskState.FAILED and "validate failed" in tk.reason
    assert (gitops.repo_dir(p["id"]) / "src" / "app.py").read_text() == "VALUE = 1\n"


async def test_project_test_regression_fails_task(client, origin):
    p = make_project(client, origin, test_cmd="test -f README.md")
    t = make_task(client, p["id"], script="rm README.md")
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    assert "tests failed" in task(t["id"]).reason


async def test_scope_fence(client, origin):
    p = make_project(client, origin)
    t = make_task(client, p["id"], script="echo x > secrets.txt", allowed_globs=["src/*"])
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    tk = task(t["id"])
    assert tk.state == TaskState.FAILED and "secrets.txt" in tk.reason


async def test_executor_nonzero_exit(client, origin):
    p = make_project(client, origin)
    t = make_task(client, p["id"], script="exit 3")
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    assert "status 3" in task(t["id"]).reason


async def test_timeout(client, origin):
    p = make_project(client, origin)
    t = make_task(client, p["id"], script="sleep 60", timeout_s=10)
    # Shorten after creation (API enforces >= 10s).
    from foreman.core.models import Task

    with session() as s:
        row = s.get(Task, t["id"])
        row.timeout_s = 1
        s.add(row)
        s.commit()
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL, timeout=15)
    assert "timed out" in task(t["id"]).reason


async def test_dependencies_run_in_order(client, origin):
    p = make_project(client, origin)
    a = make_task(client, p["id"], script="echo a > a.txt", title="a")
    b = make_task(
        client, p["id"], script="test -f a.txt && echo b > b.txt", title="b", deps=[a["id"]]
    )
    await drive(Orchestrator(), lambda: task(b["id"]).state in TERMINAL)
    assert task(a["id"]).state == TaskState.DONE
    assert task(b["id"]).state == TaskState.DONE, task(b["id"]).reason


async def test_paused_project_and_held_task_do_not_start(client, origin):
    p = make_project(client, origin)
    t = make_task(client, p["id"], script="true")
    client.post("/api/controls", json={"scope": "project", "target_id": p["id"], "action": "pause"})
    orch = Orchestrator()
    for _ in range(5):
        await orch.tick()
    assert task(t["id"]).state == TaskState.QUEUED
    client.post(
        "/api/controls", json={"scope": "project", "target_id": p["id"], "action": "resume"}
    )
    client.post("/api/controls", json={"scope": "task", "target_id": t["id"], "action": "pause"})
    for _ in range(5):
        await orch.tick()
    assert task(t["id"]).state == TaskState.QUEUED
    client.post("/api/controls", json={"scope": "task", "target_id": t["id"], "action": "resume"})
    await drive(orch, lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.DONE


async def test_pause_running_task_parks_after_step(client, origin, tmp_path):
    p = make_project(client, origin)
    flag = tmp_path / "go"
    t = make_task(client, p["id"], script=f"while [ ! -f {flag} ]; do sleep 0.1; done; echo x > x")
    orch = Orchestrator()
    await drive(
        orch,
        lambda: task(t["id"]).state == TaskState.RUNNING and bool(_runs(t["id"])),
        settle=False,
    )
    client.post("/api/controls", json={"scope": "task", "target_id": t["id"], "action": "pause"})
    flag.write_text("1")
    await drive(orch, lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.PAUSED
    # Resume continues on the same clone and finishes.
    client.post("/api/controls", json={"scope": "task", "target_id": t["id"], "action": "resume"})
    await drive(orch, lambda: task(t["id"]).state == TaskState.DONE)
    assert (gitops.repo_dir(p["id"]) / "x").exists()


async def test_one_active_task_per_project(client, origin):
    p = make_project(client, origin)
    a = make_task(client, p["id"], script="sleep 0.5; echo a > a")
    b = make_task(client, p["id"], script="echo b > b")
    orch = Orchestrator()
    await orch.tick()
    assert task(a["id"]).state == TaskState.RUNNING
    assert task(b["id"]).state == TaskState.QUEUED
    await drive(orch, lambda: task(b["id"]).state in TERMINAL)
    assert task(b["id"]).state == TaskState.DONE


async def test_node_capacity_respected(client, origin):
    """V4.3: with capacity 1, only one opencode task runs on the node at a time."""
    from foreman.orchestrator.main import pick_ready

    n = client.post(
        "/api/nodes", json={"name": "n", "base_url": "http://x/v1", "capacity": 1}
    ).json()
    projects = [
        make_project(
            client,
            origin,
            name=f"p{i}",
            executor_kind="opencode",
            role_bindings={"executor": {"node_id": n["id"], "model": "m"}},
        )
        for i in range(3)
    ]
    for p in projects:
        for _ in range(3):
            make_task(client, p["id"], script="")
    ready = pick_ready()
    assert len(ready) == 1


async def test_opencode_without_node_fails_cleanly(client, origin):
    p = make_project(client, origin, executor_kind="opencode")
    t = make_task(client, p["id"], script="")
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    assert "no node/model configured for the Tester Agent" in task(t["id"]).reason


async def test_retry_keeps_partial_work_and_passes_failure_reason(client, origin):
    p = make_project(client, origin)
    t = make_task(client, p["id"], script="echo 1 >> attempts; test $(wc -l < attempts) -ge 2")
    orch = Orchestrator()
    await drive(orch, lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.FAILED
    client.post(f"/api/tasks/{t['id']}/retry", json={})
    await drive(orch, lambda: task(t["id"]).state in TERMINAL)
    assert task(t["id"]).state == TaskState.DONE, task(t["id"]).reason
    assert task(t["id"]).attempts == 2


async def test_recover_marks_orphaned_runs_failed(client, origin):
    """V9.1: after a crash nothing stays 'running'."""
    from foreman.core.models import Task
    from foreman.core.state import transition

    p = make_project(client, origin)
    t = make_task(client, p["id"], script="true")
    transition(t["id"], (TaskState.QUEUED,), TaskState.RUNNING)
    with session() as s:
        s.add(TaskRun(task_id=t["id"], project_id=p["id"]))
        s.commit()
    recover()
    with session() as s:
        assert s.get(Task, t["id"]).state == TaskState.FAILED
    (run,) = _runs(t["id"])
    assert run.state == RunState.INTERRUPTED
