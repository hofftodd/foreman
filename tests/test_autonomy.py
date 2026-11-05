"""M3: watchdog, escalation ladder, replanning, checkpoints, milestone completion."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlmodel import col, select

from foreman import gitops
from foreman.core.models import (
    Gate,
    GateKind,
    GateState,
    PlanRevision,
    ProjectPhase,
    RevisionState,
    SpecMessage,
    Task,
    TaskRun,
    TaskState,
)
from foreman.core.watchdog import failure_signature, repeated
from foreman.db import session
from foreman.gateway import trip
from foreman.orchestrator.main import Orchestrator
from tests.conftest import git
from tests.fakellm import FakeLLM, system_text
from tests.helpers import TERMINAL, drive, make_task, project, task


class Planner:
    """Scripted planner: answers splits and plan reviews from queues."""

    def __init__(self) -> None:
        self.splits: list[Any] = []
        self.reviews: list[Any] = []
        self.chat = "ok"  # Project Planner turns
        self.calls: list[str] = []

    def __call__(self, req: dict[str, Any]) -> str:
        sys = system_text(req)
        if "Split it into" in sys:
            self.calls.append("split")
            out = self.splits.pop(0) if self.splits else "[]"
        elif "reviewing the plan" in sys:
            self.calls.append("review")
            out = self.reviews.pop(0) if self.reviews else {"summary": "on track", "ops": []}
        else:
            self.calls.append("other")
            out = self.chat
        return out if isinstance(out, str) else json.dumps(out)


@pytest.fixture
def planner():
    return Planner()


@pytest.fixture
def llm(planner):
    server = FakeLLM(host="127.0.0.1", planner=planner).start()
    yield server
    server.stop()


def auto_project(client, origin, llm=None, **kw) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "auto",
        "repo_url": str(origin),
        "executor_kind": "shell",
        "skip_spec": True,
        "watchdog": {"max_retries": 1, "replan": False, **kw.pop("watchdog", {})},
        "gates": kw.pop("gates", {"milestone": False}),
        **kw,
    }
    if llm is not None:
        n = client.post("/api/nodes", json={"name": "planner", "base_url": llm.url}).json()
        body["role_bindings"] = {"planner": {"node_id": n["id"], "model": "fake"}}
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def tasks_of(pid: int) -> list[Task]:
    with session() as s:
        return list(s.exec(select(Task).where(Task.project_id == pid).order_by(col(Task.id))).all())


def gates_of(pid: int, kind: GateKind | None = None) -> list[Gate]:
    with session() as s:
        q = select(Gate).where(Gate.project_id == pid)
        if kind:
            q = q.where(Gate.kind == kind)
        return list(s.exec(q.order_by(col(Gate.id))).all())


def runs_of(task_id: int) -> list[TaskRun]:
    with session() as s:
        return list(
            s.exec(
                select(TaskRun).where(TaskRun.task_id == task_id).order_by(col(TaskRun.id))
            ).all()
        )


# -- watchdog -------------------------------------------------------------------


def test_failure_signature_ignores_noise():
    a = "FAILED test_x.py::test_a - assert 3 == 4\n1 failed in 0.52s at /tmp/pytest-12/x"
    b = "FAILED test_x.py::test_a - assert 7 == 9\n1 failed in 1.03s at /tmp/pytest-99/y"
    c = "FAILED test_x.py::test_b - KeyError\n1 failed in 0.4s"
    assert failure_signature(a) == failure_signature(b) != failure_signature(c)
    assert repeated(["x", "x", "x"], 3) and not repeated(["x", "y", "x"], 3)
    assert not repeated(["", "", ""], 3) and not repeated(["x", "x"], 3)


async def test_watchdog_trip_kills_the_running_agent(client, origin):
    p = auto_project(client, origin, watchdog={"escalation": False})
    t = make_task(client, p["id"], script="sleep 300")
    orch = Orchestrator()
    await drive(
        orch, lambda: bool(runs_of(t["id"])) and bool(runs_of(t["id"])[-1].handle), settle=False
    )
    trip(runs_of(t["id"])[-1].id, "model request budget exceeded (60 calls)")
    await drive(orch, lambda: task(t["id"]).state in TERMINAL, timeout=15)
    assert task(t["id"]).state == TaskState.FAILED
    assert "watchdog: model request budget exceeded" in task(t["id"]).reason
    assert runs_of(t["id"])[-1].failure_kind == "watchdog"


async def test_diff_size_limit(client, origin):
    p = auto_project(client, origin, watchdog={"escalation": False, "max_diff_lines": 10})
    t = make_task(client, p["id"], script="seq 1 50 > big.txt")
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL)
    assert "diff too large: 50 lines changed (limit 10)" in task(t["id"]).reason


# -- escalation -------------------------------------------------------------------


async def test_retries_then_blocks_without_planner(client, origin):
    p = auto_project(client, origin)
    # Output that really differs each time, so the failures are not "the same failure".
    t = make_task(client, p["id"], script="head -c 12 /dev/urandom | base64 | tr -d 0-9; exit 1")
    orch = Orchestrator()
    await drive(orch, lambda: task(t["id"]).state == TaskState.BLOCKED)
    assert len(runs_of(t["id"])) == 2  # first try + max_retries=1
    (gate,) = gates_of(p["id"], GateKind.ESCALATION)
    assert gate.task_id == t["id"] and "stuck" in gate.title

    # Approve = try again (fresh attempts), which blocks again here.
    client.post(f"/api/gates/{gate.id}/resolve", json={"approve": True})
    await drive(
        orch, lambda: len(runs_of(t["id"])) == 4 and task(t["id"]).state == TaskState.BLOCKED
    )
    gate2 = gates_of(p["id"], GateKind.ESCALATION)[-1]
    client.post(f"/api/gates/{gate2.id}/resolve", json={"approve": False})
    await drive(orch, lambda: task(t["id"]).state == TaskState.DISCARDED)


async def test_config_errors_block_immediately(client, origin):
    p = auto_project(client, origin, executor_kind="opencode")
    t = make_task(client, p["id"], script="")
    await drive(Orchestrator(), lambda: task(t["id"]).state == TaskState.BLOCKED)
    assert len(runs_of(t["id"])) == 1
    assert "configuration problem" in task(t["id"]).reason


async def test_same_failure_repeating_skips_retries(client, origin):
    p = auto_project(client, origin, watchdog={"max_retries": 5, "repeat_limit": 2})
    t = make_task(client, p["id"], script="echo 'AssertionError: 1 != 2'; exit 1")
    await drive(Orchestrator(), lambda: task(t["id"]).state == TaskState.BLOCKED)
    assert len(runs_of(t["id"])) == 2  # not 6: the failure repeated
    assert "same failure keeps repeating" in task(t["id"]).reason


async def test_split_after_retries(client, origin, llm, planner):
    p = auto_project(client, origin, llm)
    a = make_task(client, p["id"], script="exit 1", title="Too much at once")
    b = make_task(client, p["id"], script="true", title="Afterwards", deps=[a["id"]])
    planner.splits.append(
        [
            {
                "key": "S1",
                "title": "First half",
                "criteria": ["it works"],
                "deps": [],
            },
            {
                "key": "S2",
                "title": "Second half",
                "criteria": ["it works"],
                "deps": ["S1"],
            },
        ]
    )
    orch = Orchestrator()
    await drive(orch, lambda: task(a["id"]).state == TaskState.SPLIT)
    children = [t for t in tasks_of(p["id"]) if t.parent_id == a["id"]]
    assert [c.key for c in children] == [f"T{a['id']}.1", f"T{a['id']}.2"]
    assert children[1].deps == [children[0].id]
    assert sorted(task(b["id"]).deps) == sorted(c.id for c in children)  # rewired
    await drive(orch, lambda: task(b["id"]).state in TERMINAL)
    assert task(b["id"]).state == TaskState.DONE
    assert planner.calls.count("split") == 1


async def test_unusable_split_blocks(client, origin, llm, planner):
    p = auto_project(client, origin, llm)
    t = make_task(client, p["id"], script="exit 1")
    planner.splits += ["not json"] * 3
    await drive(Orchestrator(), lambda: task(t["id"]).state == TaskState.BLOCKED)
    assert "could not split" in task(t["id"]).reason


# -- checkpoints ---------------------------------------------------------------------


async def test_task_merge_checkpoint(client, origin):
    p = auto_project(client, origin, gates={"task_merge": True, "milestone": False})
    t = make_task(client, p["id"], script="echo hi > merged.txt")
    orch = Orchestrator()
    await drive(orch, lambda: task(t["id"]).state == TaskState.AWAITING_MERGE)
    assert not (gitops.repo_dir(p["id"]) / "merged.txt").exists()
    (gate,) = gates_of(p["id"], GateKind.TASK_MERGE)
    assert client.get("/api/system").json()["pending_gates"] == 1
    client.post(f"/api/gates/{gate.id}/resolve", json={"approve": True})
    await drive(orch, lambda: task(t["id"]).state == TaskState.DONE)
    assert (gitops.repo_dir(p["id"]) / "merged.txt").exists()
    # Deciding twice is refused.
    r = client.post(f"/api/gates/{gate.id}/resolve", json={"approve": False})
    assert r.status_code == 409


async def test_rejected_merge_fails_the_task(client, origin):
    p = auto_project(client, origin, gates={"task_merge": True}, watchdog={"escalation": False})
    t = make_task(client, p["id"], script="echo hi > x.txt")
    orch = Orchestrator()
    await drive(orch, lambda: task(t["id"]).state == TaskState.AWAITING_MERGE)
    (gate,) = gates_of(p["id"], GateKind.TASK_MERGE)
    client.post(f"/api/gates/{gate.id}/resolve", json={"approve": False, "note": "ugly"})
    await drive(orch, lambda: task(t["id"]).state == TaskState.FAILED)
    assert "merge rejected by user: ugly" in task(t["id"]).reason


def test_inbox_lists_pending_with_names(client, origin):
    from foreman.core import gates

    p = auto_project(client, origin)
    gates.open_gate(p["id"], GateKind.MILESTONE, "Ship it?")
    rows = client.get("/api/gates").json()
    assert rows[0]["title"] == "Ship it?" and rows[0]["project_name"] == "auto"
    assert client.get("/api/gates?state=all").json()


# -- replanning -------------------------------------------------------------------------


async def test_replan_after_task_applies_ops(client, origin, llm, planner):
    p = auto_project(client, origin, llm, watchdog={"replan": True})
    a = make_task(client, p["id"], script="true", title="A")
    b = make_task(client, p["id"], script="true", title="B", deps=[a["id"]])
    keys = {t.id: t.key for t in tasks_of(p["id"])}
    assert keys == {a["id"]: "", b["id"]: ""}
    with session() as s:  # give them keys like a decomposed plan
        for t in s.exec(select(Task).where(Task.project_id == p["id"])):
            t.key = "TA" if t.id == a["id"] else "TB"
            s.add(t)
        s.commit()
    from foreman.core.models import Project as ProjectRow

    with session() as s:
        row = s.get(ProjectRow, p["id"])
        row.baseline_tasks = 2
        s.add(row)
        s.commit()
    planner.reviews.append(
        {
            "summary": "B is redundant; add docs",
            "ops": [
                {"op": "remove", "key": "TB", "reason": "A already covers it"},
                {
                    "op": "add",
                    "task": {
                        "key": "TC",
                        "title": "Docs",
                        "criteria": ["it works"],
                        "deps": ["TA"],
                    },
                    "reason": "users need docs",
                },
            ],
        }
    )
    orch = Orchestrator()
    await drive(
        orch,
        lambda: any(t.key == "TC" and t.state == TaskState.DONE for t in tasks_of(p["id"])),
        timeout=40,
    )
    assert task(b["id"]).state == TaskState.DISCARDED
    assert "removed by replanning: A already covers it" in task(b["id"]).reason
    with session() as s:
        rev = s.exec(select(PlanRevision).where(PlanRevision.project_id == p["id"])).first()
    assert rev is not None and rev.state == RevisionState.APPLIED and len(rev.ops) == 2
    tasks_md = gitops.read_repo_file(p["id"], "specs/001-initial/tasks.md")
    assert "## TC: Docs" in tasks_md and "State: discarded" not in tasks_md


async def test_replan_growth_cap_needs_approval(client, origin, llm, planner):
    p = auto_project(client, origin, llm, watchdog={"replan": True, "growth_cap_pct": 50})
    a = make_task(client, p["id"], script="true", title="A")
    adds = [
        {
            "op": "add",
            "task": {
                "key": f"N{i}",
                "title": f"New {i}",
                "criteria": ["it works"],
            },
            "reason": "needed",
        }
        for i in range(2)
    ]
    planner.reviews.append({"summary": "two more", "ops": adds})
    orch = Orchestrator()
    await drive(orch, lambda: bool(gates_of(p["id"], GateKind.PLAN_REVISION)))
    assert not any(t.key.startswith("N") for t in tasks_of(p["id"]))
    (gate,) = gates_of(p["id"], GateKind.PLAN_REVISION)
    assert "cap 0" in gate.detail or "cap" in gate.detail
    client.post(f"/api/gates/{gate.id}/resolve", json={"approve": True})
    await drive(orch, lambda: sum(t.key.startswith("N") for t in tasks_of(p["id"])) == 2)
    assert task(a["id"]).state == TaskState.DONE


async def test_reviewer_cannot_touch_finished_tasks(client, origin, llm, planner):
    p = auto_project(client, origin, llm, watchdog={"replan": True})
    a = make_task(client, p["id"], script="true", title="A")
    with session() as s:
        t = s.get(Task, a["id"])
        t.key = "TA"
        s.add(t)
        s.commit()
    bad = {"summary": "x", "ops": [{"op": "remove", "key": "TA", "reason": "no"}]}
    planner.reviews += [bad, bad]
    orch = Orchestrator()
    await drive(orch, lambda: planner.calls.count("review") == 2)
    await drive(orch, lambda: task(a["id"]).state == TaskState.DONE)
    with session() as s:
        assert s.exec(select(PlanRevision)).all() == []


async def test_spec_change_is_always_gated(client, origin, llm, planner):
    p = auto_project(client, origin, llm, watchdog={"replan": True})
    gitops.write_and_commit(p["id"], {"specs/001-initial/spec.md": "# Spec\n- FR-1 x"}, "spec")
    make_task(client, p["id"], script="true")
    planner.reviews.append(
        {
            "summary": "spec gap",
            "ops": [],
            "spec_change": {"reason": "no auth", "proposal": "Add FR-2 login"},
        }
    )
    orch = Orchestrator()
    await drive(orch, lambda: bool(gates_of(p["id"], GateKind.SPEC_CHANGE)))
    (gate,) = gates_of(p["id"], GateKind.SPEC_CHANGE)
    # Approved: the Project Planning Agent (not Foreman, not the Task Planner) edits the
    # documents. This older project keeps its original spec.md name.
    planner.chat = "Done.\n<<<FILE specs/001-initial/spec.md\n# Spec\n- FR-1 x\n- FR-2 login\n>>>"
    client.post(f"/api/gates/{gate.id}/resolve", json={"approve": True})
    await drive(
        orch,
        lambda: "FR-2 login" in gitops.read_repo_file(p["id"], "specs/001-initial/spec.md"),
    )
    with session() as s:
        asked = s.exec(select(SpecMessage).where(SpecMessage.role == "user")).all()
    assert "Add FR-2 login" in asked[-1].content


# -- milestone ------------------------------------------------------------------------------


async def test_acceptance_pass_completes_and_pushes(client, origin):
    p = auto_project(client, origin, gates={"milestone": True})
    client.patch(f"/api/projects/{p['id']}", json={"acceptance_cmd": "test -f done.txt"})
    make_task(client, p["id"], script="echo ok > done.txt")
    orch = Orchestrator()
    await drive(orch, lambda: project(p["id"]).phase == ProjectPhase.DONE)
    assert project(p["id"]).acceptance_passed
    (gate,) = gates_of(p["id"], GateKind.MILESTONE)
    client.post(f"/api/gates/{gate.id}/resolve", json={"approve": True})
    await drive(orch, lambda: gates_of(p["id"], GateKind.MILESTONE)[0].handled_at is not None)
    # The milestone branch is now on the origin (a local bare repo here: no PR).
    assert "foreman/m1" in git("branch", "--list", "foreman/m1", cwd=origin)


async def test_acceptance_failure_goes_to_replanner_then_user(client, origin, llm, planner):
    p = auto_project(client, origin, llm, watchdog={"replan": True, "max_acceptance_failures": 1})
    client.patch(f"/api/projects/{p['id']}", json={"acceptance_cmd": "test -f never.txt"})
    make_task(client, p["id"], script="true")
    orch = Orchestrator()
    # Reviews: after the task (no ops), after the acceptance failure (no ops).
    await drive(orch, lambda: bool(gates_of(p["id"], GateKind.ESCALATION)), timeout=40)
    gate = gates_of(p["id"], GateKind.ESCALATION)[-1]
    assert gate.task_id is None and "Acceptance" in gate.title
    assert project(p["id"]).phase == ProjectPhase.EXECUTE
    assert planner.calls.count("review") >= 2


def test_gate_state_enum_values():
    assert GateState.PENDING.value == "pending"


def test_new_gate_sends_push_notification(client, origin, monkeypatch):
    import http.server
    import threading

    got: list[tuple[str, dict[str, str]]] = []
    done = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            got.append((body, dict(self.headers.items())))
            self.send_response(200)
            self.end_headers()
            done.set()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("FOREMAN_NTFY_URL", f"http://127.0.0.1:{server.server_address[1]}/topic")
    monkeypatch.setenv("FOREMAN_PUBLIC_URL", "http://foreman.lan:8700")
    from foreman.core import gates

    p = auto_project(client, origin)
    gates.open_gate(p["id"], GateKind.MILESTONE, "Ship it?", "all green")
    assert done.wait(5)
    body, headers = got[0]
    assert body == "Ship it?\nall green"
    assert (
        headers["Title"] == "foreman: auto" and headers["Click"] == "http://foreman.lan:8700/inbox"
    )
    server.shutdown()


async def test_project_stop_kills_running_acceptance(client, origin):
    from concurrent.futures import ThreadPoolExecutor

    from foreman.core.models import Control, ControlAction, ControlScope
    from foreman.db import control_session
    from foreman.sandbox import group_alive
    from foreman.supervisor.main import apply_control

    p = auto_project(client, origin)
    client.patch(f"/api/projects/{p['id']}", json={"acceptance_cmd": "sleep 300"})
    make_task(client, p["id"], script="true")
    orch = Orchestrator()
    await drive(orch, lambda: bool(project(p["id"]).acceptance_handle), settle=False)
    handle = project(p["id"]).acceptance_handle
    pgid = int(handle.split(":")[1])
    with control_session() as s:
        c = Control(scope=ControlScope.PROJECT, target_id=p["id"], action=ControlAction.STOP)
        s.add(c)
        s.commit()
        s.refresh(c)
    with ThreadPoolExecutor(2) as pool:
        apply_control(c, pool)
    await drive(orch, lambda: not group_alive(pgid), timeout=10)
    assert project(p["id"]).phase == ProjectPhase.EXECUTE  # not marked done


async def test_new_task_reopens_a_finished_project(client, origin):
    p = auto_project(client, origin)
    make_task(client, p["id"], script="true")
    orch = Orchestrator()
    await drive(orch, lambda: project(p["id"]).phase == ProjectPhase.DONE)
    t = make_task(client, p["id"], script="echo more > more.txt")
    assert project(p["id"]).phase == ProjectPhase.EXECUTE
    await drive(
        orch,
        lambda: (
            task(t["id"]).state == TaskState.DONE and project(p["id"]).phase == ProjectPhase.DONE
        ),
    )
