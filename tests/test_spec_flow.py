"""Phase 1 end to end with a scripted planner model (validation.md V2.x, V3.x, E2E-1)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest
from sqlmodel import col, select

from foreman import gitops
from foreman.core.models import (
    Event,
    JobState,
    PlannerJob,
    ProjectPhase,
    SpecMessage,
    Task,
    TaskState,
)
from foreman.db import session
from foreman.orchestrator.main import Orchestrator, recover
from tests.conftest import git
from tests.fakellm import FakeLLM, last_user_text, system_text
from tests.helpers import TERMINAL, drive, project

FEATURE = "specs/001-initial"

REQUIREMENTS = """# Requirements: greeter
- FR-1.1 Greeting a name gives "hello, <name>".
- FR-1.2 A command line prints the greeting.
"""
DESIGN = "# Design\nA greeting part and a command-line part that uses it.\n"
ACCEPTANCE = "# Acceptance criteria\n- AC-1 (FR-1.1) Greeting bob gives hello, bob\n"

CHAT_REPLY = f"""I drafted everything.

<<<FILE {FEATURE}/requirements.md
{REQUIREMENTS}>>>
<<<FILE {FEATURE}/design.md
{DESIGN}>>>
<<<FILE {FEATURE}/acceptance-criteria.md
{ACCEPTANCE}>>>
Anything else?"""

GOOD_TASKS = [
    {
        "key": "T1",
        "title": "Write greeter",
        "goal": "Greet a person by name.",
        "req_refs": ["FR-1.1"],
        "criteria": ["Greeting bob gives hello, bob"],
        "deps": [],
    },
    {
        "key": "T2",
        "title": "Write CLI",
        "goal": "Print the greeting from the command line.",
        "req_refs": ["FR-1.2"],
        "criteria": ["Running it with bob prints hello, bob"],
        "deps": ["T1"],
    },
]
TOO_BIG = [{**GOOD_TASKS[0], "criteria": [f"criterion {i}" for i in range(7)]}]
PLANNED = ["SETUP", "ACCEPT", "T1", "T2", "DOCS"]  # with the standard tasks


def scripted(chat: str = CHAT_REPLY, decompose: list[Any] | Callable[[int], Any] = GOOD_TASKS):
    calls = {"decompose": 0}

    def planner(req: dict[str, Any]) -> str:
        if "reviewing the plan" in system_text(req):
            return json.dumps({"summary": "on track", "ops": []})
        if "break the project into small, ordered tasks" in system_text(req):
            calls["decompose"] += 1
            out = decompose(calls["decompose"]) if callable(decompose) else decompose
            return out if isinstance(out, str) else "```json\n" + json.dumps(out) + "\n```"
        return chat

    return planner


@pytest.fixture
def llm():
    server = FakeLLM(host="127.0.0.1", planner=scripted()).start()
    yield server
    server.stop()


def spec_project(client, origin, llm, **kw) -> dict[str, Any]:
    n = client.post(
        "/api/nodes", json={"name": "planner", "base_url": llm.url, "context_window": 32768}
    ).json()
    body = {
        "name": "greeter",
        "repo_url": str(origin),
        "executor_kind": "shell",
        "role_bindings": {
            "project_planner": {"node_id": n["id"], "model": "fake"},
            "task_planner": {"node_id": n["id"], "model": "fake"},
        },
        **kw,
    }
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["phase"] == "specify"
    return p


def job_states(pid: int) -> list[JobState]:
    with session() as s:
        return [
            j.state
            for j in s.exec(
                select(PlannerJob).where(PlannerJob.project_id == pid).order_by(col(PlannerJob.id))
            )
        ]


async def chat(client, orch, pid: int, text: str) -> None:
    r = client.post(f"/api/projects/{pid}/spec/messages", json={"content": text})
    assert r.status_code == 201, r.text
    await drive(orch, lambda: job_states(pid)[-1] not in (JobState.QUEUED, JobState.RUNNING))


async def test_chat_writes_and_commits_documents(client, origin, llm):
    p = spec_project(client, origin, llm)
    orch = Orchestrator()
    await chat(client, orch, p["id"], "Build a greeter.")
    assert job_states(p["id"]) == [JobState.DONE]

    spec = client.get(f"/api/projects/{p['id']}/spec").json()
    assert spec["docs"]["requirements"].strip() == REQUIREMENTS.strip()
    assert spec["docs"]["design"].strip() == DESIGN.strip()
    assert spec["paths"]["acceptance"] == f"{FEATURE}/acceptance-criteria.md"
    roles = [m["role"] for m in spec["messages"]]
    assert roles == ["user", "assistant"]
    reply = spec["messages"][1]
    assert f"[wrote {FEATURE}/requirements.md]" in reply["content"]
    assert f"{FEATURE}/acceptance-criteria.md" in reply["files"]

    repo = gitops.repo_dir(p["id"])
    log = git("log", "--format=%s", cwd=repo)
    assert "spec: I drafted everything." in log
    # The planner saw the user's message.
    assert last_user_text(llm.requests[-1]) == "Build a greeter."


async def test_second_turn_sees_current_documents(client, origin, llm):
    p = spec_project(client, origin, llm)
    orch = Orchestrator()
    await chat(client, orch, p["id"], "Build a greeter.")
    await chat(client, orch, p["id"], "Looks good.")
    sys = system_text(llm.requests[-1])
    assert "FR-1.1 Greeting a name" in sys  # docs are in the system prompt
    roles = [m["role"] for m in llm.requests[-1]["messages"] if m["role"] != "system"]
    assert roles == ["user", "assistant", "user"]


def test_cannot_chat_while_planner_busy(client, origin, llm):
    p = spec_project(client, origin, llm)
    client.post(f"/api/projects/{p['id']}/spec/messages", json={"content": "a"})
    r = client.post(f"/api/projects/{p['id']}/spec/messages", json={"content": "b"})
    assert r.status_code == 409


async def test_user_can_edit_documents(client, origin, llm):
    p = spec_project(client, origin, llm)
    r = client.put(f"/api/projects/{p['id']}/spec/docs/design", json={"content": "# My design"})
    assert r.status_code == 200
    docs = client.get(f"/api/projects/{p['id']}/spec").json()["docs"]
    assert docs["design"] == "# My design\n"
    assert (
        client.put(f"/api/projects/{p['id']}/spec/docs/tasks", json={"content": "x"}).status_code
        == 400
    )


async def test_spec_approval_requires_the_three_documents(client, origin, llm):
    p = spec_project(client, origin, llm)
    r = client.post(f"/api/projects/{p['id']}/spec/approve")
    assert r.status_code == 400 and "missing documents" in r.json()["detail"]
    for name in ("requirements", "design"):
        client.put(f"/api/projects/{p['id']}/spec/docs/{name}", json={"content": "x"})
    r = client.post(f"/api/projects/{p['id']}/spec/approve")
    assert r.status_code == 400 and "acceptance" in r.json()["detail"]
    client.put(f"/api/projects/{p['id']}/spec/docs/acceptance", json={"content": "x"})
    # No acceptance command needed: the Tester writes the acceptance tests later.
    assert client.post(f"/api/projects/{p['id']}/spec/approve").status_code == 200


async def test_full_flow_spec_to_done(client, origin, llm):
    """E2E-1 with the shell executor: chat -> approve -> tasks -> approve -> run -> done."""
    p = spec_project(client, origin, llm)
    pid = p["id"]
    orch = Orchestrator()
    await chat(client, orch, pid, "Build a greeter.")

    assert client.post(f"/api/projects/{pid}/spec/approve").status_code == 200
    assert project(pid).phase == ProjectPhase.DECOMPOSING
    assert project(pid).spec_commit
    await drive(orch, lambda: project(pid).phase != ProjectPhase.DECOMPOSING)
    assert project(pid).phase == ProjectPhase.PLAN_REVIEW

    with session() as s:
        tasks = s.exec(select(Task).where(Task.project_id == pid).order_by(col(Task.id))).all()
    assert [t.key for t in tasks] == PLANNED
    assert all(t.state == TaskState.PROPOSED for t in tasks)
    by_key = {t.key: t for t in tasks}
    assert by_key["T2"].deps == [by_key["T1"].id] and by_key["T1"].deps == [by_key["SETUP"].id]
    assert by_key["T1"].criteria == ["Greeting bob gives hello, bob"]
    assert by_key["T1"].description == "Greet a person by name."
    assert [t.kind for t in tasks] == ["setup", "acceptance", "feature", "feature", "docs"]
    assert by_key["T1"].size_violations == [] and by_key["T1"].pack_tokens > 0
    tasks_md = client.get(f"/api/projects/{pid}/spec").json()["docs"]["tasks"]
    assert "## T1: Write greeter" in tasks_md and "Depends on: T1" in tasks_md
    assert "Done when:\n- Greeting bob gives hello, bob" in tasks_md

    # Proposed tasks never run before approval.
    for _ in range(3):
        await orch.tick()
    assert all(t.state == TaskState.PROPOSED for t in [task_row(t.id) for t in tasks])

    # The scripted shell executor stands in for the agents: T1 writes a file.
    client.patch(
        f"/api/tasks/{by_key['T1'].id}",
        json={"executor_kind": "shell", "executor_script": "echo hi > greet.txt"},
    )
    r = client.post(f"/api/projects/{pid}/plan/approve")
    assert r.status_code == 200 and r.json()["queued"] == len(PLANNED)
    assert project(pid).phase == ProjectPhase.EXECUTE
    await drive(orch, lambda: task_row(by_key["DOCS"].id).state in TERMINAL, timeout=60)
    assert all(task_row(t.id).state == TaskState.DONE for t in tasks)
    assert (gitops.repo_dir(pid) / "greet.txt").exists()


def task_row(task_id: int) -> Task:
    with session() as s:
        t = s.get(Task, task_id)
        assert t is not None
        return t


async def approved(client, orch, pid: int) -> None:
    await chat(client, orch, pid, "Build a greeter.")
    assert client.post(f"/api/projects/{pid}/spec/approve").status_code == 200
    await drive(orch, lambda: project(pid).phase != ProjectPhase.DECOMPOSING)


async def test_oversized_task_is_sent_back_to_be_split(client, origin, llm):
    llm.planner = scripted(decompose=lambda n: TOO_BIG if n == 1 else GOOD_TASKS)
    p = spec_project(client, origin, llm)
    await approved(client, Orchestrator(), p["id"])
    retry = last_user_text(llm.requests[-1])
    assert "7 criteria (limit 6)" in retry and "COMPLETE corrected" in retry
    with session() as s:
        keys = [t.key for t in s.exec(select(Task).where(Task.project_id == p["id"]))]
    assert keys == PLANNED


async def test_persistent_violations_fail_the_breakdown(client, origin, llm):
    llm.planner = scripted(decompose=TOO_BIG)
    p = spec_project(client, origin, llm)
    pid = p["id"]
    await approved(client, Orchestrator(), pid)
    assert project(pid).phase == ProjectPhase.SPECIFY  # nothing too big is ever proposed
    assert job_states(pid)[-1] == JobState.FAILED


async def test_user_edits_are_size_checked(client, origin, llm):
    p = spec_project(client, origin, llm)
    pid = p["id"]
    await approved(client, Orchestrator(), pid)
    with session() as s:
        t = s.exec(select(Task).where(Task.project_id == pid, Task.key == "T1")).one()
    many = [f"criterion {i}" for i in range(7)]
    edited = client.patch(f"/api/tasks/{t.id}", json={"criteria": many}).json()
    assert "7 criteria (limit 6); split it" in edited["size_violations"]
    r = client.post(f"/api/projects/{pid}/plan/approve")
    assert r.status_code == 400 and "size limits" in r.json()["detail"]
    fixed = client.patch(f"/api/tasks/{t.id}", json={"criteria": many[:2]}).json()
    assert fixed["size_violations"] == []
    assert client.post(f"/api/projects/{pid}/plan/approve").status_code == 200


async def test_unparseable_breakdown_returns_to_specify(client, origin, llm):
    llm.planner = scripted(decompose=lambda n: "Sorry, I can't do JSON.")
    p = spec_project(client, origin, llm)
    pid = p["id"]
    await approved(client, Orchestrator(), pid)
    assert project(pid).phase == ProjectPhase.SPECIFY
    assert job_states(pid)[-1] == JobState.FAILED
    with session() as s:
        last = s.exec(
            select(SpecMessage)
            .where(SpecMessage.project_id == pid)
            .order_by(col(SpecMessage.id).desc())
        ).first()
    assert last is not None and "Task breakdown did not complete" in last.content


async def test_reopen_and_redecompose(client, origin, llm):
    p = spec_project(client, origin, llm)
    pid = p["id"]
    orch = Orchestrator()
    await approved(client, orch, pid)
    assert client.post(f"/api/projects/{pid}/plan/redecompose").status_code == 200
    await drive(orch, lambda: project(pid).phase == ProjectPhase.PLAN_REVIEW)
    with session() as s:
        assert len(s.exec(select(Task).where(Task.project_id == pid)).all()) == len(PLANNED)
    assert client.post(f"/api/projects/{pid}/spec/reopen").status_code == 200
    assert project(pid).phase == ProjectPhase.SPECIFY
    with session() as s:
        assert s.exec(select(Task).where(Task.project_id == pid)).all() == []


async def test_stop_aborts_planner_mid_stream_and_applies_nothing(client, origin, llm):
    """FR-8.2 for planner calls: Stop closes the model stream; no files are written."""
    llm.planner = scripted(chat=CHAT_REPLY + "\n" + "x" * 20000)
    llm.chunk_delay = 0.02
    p = spec_project(client, origin, llm)
    pid = p["id"]
    orch = Orchestrator()
    client.post(f"/api/projects/{pid}/spec/messages", json={"content": "go"})
    await drive(
        orch, lambda: bool(llm.requests) and job_states(pid)[-1] == JobState.RUNNING, settle=False
    )
    client.post("/api/controls", json={"scope": "project", "target_id": pid, "action": "stop"})
    await drive(orch, lambda: job_states(pid)[-1] == JobState.STOPPED)
    assert llm.disconnected.wait(5)
    assert not (gitops.repo_dir(pid) / FEATURE / "requirements.md").exists()


async def test_missing_planner_binding_fails_the_job(client, origin):
    r = client.post("/api/projects", json={"name": "x", "repo_url": str(origin)})
    pid = r.json()["id"]
    orch = Orchestrator()
    await chat(client, orch, pid, "hi")
    with session() as s:
        job = s.exec(select(PlannerJob).where(PlannerJob.project_id == pid)).one()
    assert job.state == JobState.FAILED and "no planner" in job.error


def test_bindings_must_name_existing_nodes(client, origin):
    r = client.post(
        "/api/projects",
        json={
            "name": "x",
            "repo_url": str(origin),
            "role_bindings": {"escalation": {"node_id": 999, "model": "m"}},
        },
    )
    assert r.status_code == 400 and "escalation node not found" in r.json()["detail"]


async def test_tasks_do_not_run_before_execute_phase(client, origin, llm):
    p = spec_project(client, origin, llm)
    t = client.post(
        f"/api/projects/{p['id']}/tasks", json={"title": "early", "validation_cmd": "true"}
    ).json()
    orch = Orchestrator()
    for _ in range(3):
        await orch.tick()
    assert task_row(t["id"]).state == TaskState.QUEUED


def test_recover_fails_interrupted_jobs(client, origin, llm):
    p = spec_project(client, origin, llm)
    with session() as s:
        s.add(PlannerJob(project_id=p["id"], kind="decompose", state=JobState.RUNNING))
        s.commit()
    from foreman.core.models import Project as ProjectRow

    with session() as s:
        row = s.get(ProjectRow, p["id"])
        row.phase = ProjectPhase.DECOMPOSING
        s.add(row)
        s.commit()
    recover()
    assert job_states(p["id"]) == [JobState.FAILED]
    assert project(p["id"]).phase == ProjectPhase.SPECIFY


async def test_failure_while_applying_results_is_reported(client, origin, llm, monkeypatch):
    p = spec_project(client, origin, llm)

    def broken(*a, **k):
        raise gitops.GitError("disk full")

    monkeypatch.setattr(gitops, "write_and_commit", broken)
    await chat(client, Orchestrator(), p["id"], "Build a greeter.")
    with session() as s:
        job = s.exec(select(PlannerJob).where(PlannerJob.project_id == p["id"])).one()
        last = s.exec(
            select(SpecMessage)
            .where(SpecMessage.project_id == p["id"])
            .order_by(col(SpecMessage.id).desc())
        ).first()
    assert job.state == JobState.FAILED and "disk full" in job.error
    assert last is not None and "planner error" in last.content


async def test_paused_project_holds_planner_jobs(client, origin, llm):
    p = spec_project(client, origin, llm)
    client.post("/api/controls", json={"scope": "project", "target_id": p["id"], "action": "pause"})
    client.post(f"/api/projects/{p['id']}/spec/messages", json={"content": "hi"})
    orch = Orchestrator()
    for _ in range(3):
        await orch.tick()
        await asyncio.sleep(0.2)  # let anything that was started actually run
    assert job_states(p["id"]) == [JobState.QUEUED]
    client.post(
        "/api/controls", json={"scope": "project", "target_id": p["id"], "action": "resume"}
    )
    await drive(orch, lambda: job_states(p["id"]) == [JobState.DONE])


async def test_plan_approval_rejects_dependencies_on_dropped_tasks(client, origin, llm):
    p = spec_project(client, origin, llm)
    pid = p["id"]
    await approved(client, Orchestrator(), pid)
    with session() as s:
        t1 = s.exec(select(Task).where(Task.project_id == pid, Task.key == "T1")).one()
    client.post(f"/api/tasks/{t1.id}/discard")
    r = client.post(f"/api/projects/{pid}/plan/approve")
    assert r.status_code == 400 and "dropped tasks: T2" in r.json()["detail"]


async def test_runs_to_done_without_any_input_when_optional_gates_are_off(client, origin, llm):
    """V7.1: after spec approval, the tasks run, are reviewed, pass acceptance and the
    project finishes without a single checkpoint."""
    three = [
        *GOOD_TASKS,
        {"key": "T3", "title": "Third", "goal": "g", "criteria": ["c"], "deps": ["T2"]},
    ]
    llm.planner = scripted(decompose=three)
    p = spec_project(client, origin, llm, gates={"plan_approval": False, "milestone": False})
    pid = p["id"]
    client.patch(f"/api/projects/{pid}", json={"acceptance_cmd": "test -f c.txt"})
    orch = Orchestrator()
    await chat(client, orch, pid, "Build it.")
    assert client.post(f"/api/projects/{pid}/spec/approve").status_code == 200
    await drive(orch, lambda: project(pid).phase == ProjectPhase.EXECUTE)
    # The shell executor stands in for the agents: each feature task writes its file.
    files = {"T1": "a.txt", "T2": "b.txt", "T3": "c.txt"}
    with session() as s:
        for t in s.exec(select(Task).where(Task.project_id == pid)):
            t.executor_script = f"echo ok > {files[t.key]}" if t.key in files else "true"
            s.add(t)
        s.commit()
    await drive(orch, lambda: project(pid).phase == ProjectPhase.DONE, timeout=90)
    with session() as s:
        from foreman.core.models import Gate, PlanRevision

        assert s.exec(select(Gate).where(Gate.project_id == pid)).all() == []
        reviews = s.exec(select(PlanRevision).where(PlanRevision.project_id == pid)).all()
    assert len(reviews) == 6  # one after each task (3 planned + setup, acceptance, docs)
    assert project(pid).acceptance_passed and project(pid).baseline_tasks == 6


async def test_thinking_model_streams_reasoning_and_gets_room_to_think(client, origin, llm):
    llm.reasoning = "Let me think about the greeter. " * 40
    p = spec_project(client, origin, llm)
    await chat(client, Orchestrator(), p["id"], "Build a greeter.")
    assert job_states(p["id"]) == [JobState.DONE]
    reply = client.get(f"/api/projects/{p['id']}/spec").json()["messages"][1]["content"]
    assert "Let me think" not in reply  # reasoning is never part of the reply
    with session() as s:
        thinking = s.exec(select(Event).where(Event.kind == "planner_thinking")).all()
    assert "".join(e.message for e in thinking) == llm.reasoning
    # The output budget is what the 32k context leaves, not a fixed 16k.
    assert 16000 < llm.requests[-1]["max_tokens"] < 32768


async def test_planner_cut_off_while_thinking_says_so(client, origin, llm):
    llm.reasoning = "Hmm. " * 20
    llm.planner = lambda req: ""
    llm.finish = "length"
    p = spec_project(client, origin, llm)
    await chat(client, Orchestrator(), p["id"], "Build a greeter.")
    with session() as s:
        job = s.exec(select(PlannerJob).where(PlannerJob.project_id == p["id"])).one()
    assert job.state == JobState.FAILED
    assert "output budget thinking without answering" in job.error
