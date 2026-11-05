"""M4: multi-node scheduling, node health, cloud spend caps."""

from __future__ import annotations

import pytest
from sqlmodel import col, select

from foreman.core import spend
from foreman.core.models import (
    Event,
    Gate,
    GateKind,
    GateState,
    JobState,
    Node,
    PlannerJob,
    Project,
    RunState,
    Task,
    TaskRun,
    TaskState,
)
from foreman.db import session
from foreman.orchestrator.main import Orchestrator, check_nodes, node_load, pick_ready
from tests.fakellm import FakeLLM
from tests.helpers import drive, make_project, make_task, project


@pytest.fixture
def llm():
    server = FakeLLM(host="127.0.0.1", planner=lambda r: "noted").start()
    yield server
    server.stop()


def add_node(client, **kw) -> dict:
    body = {"name": "n", "base_url": "http://127.0.0.1:9/v1", **kw}
    r = client.post("/api/nodes", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def node(node_id: int) -> Node:
    with session() as s:
        n = s.get(Node, node_id)
        assert n is not None
        return n


def events(text: str) -> list[Event]:
    with session() as s:
        return [e for e in s.exec(select(Event).order_by(col(Event.id))) if text in e.message]


def fake_running_run(project_id: int, node_id: int) -> int:
    """Occupy a node slot as if a task attempt were running on it."""
    with session() as s:
        t = Task(project_id=project_id, title="busy", state=TaskState.RUNNING)
        s.add(t)
        s.commit()
        run = TaskRun(task_id=t.id or 0, project_id=project_id, node_id=node_id)
        s.add(run)
        s.commit()
        assert run.id is not None
        return run.id


def finish(run_id: int) -> None:
    with session() as s:
        run = s.get(TaskRun, run_id)
        assert run is not None
        run.state = RunState.SUCCEEDED
        s.add(run)
        s.commit()


def planner_project(client, origin, node_id: int, **kw) -> dict:
    return make_project(
        client,
        origin,
        skip_spec=False,
        role_bindings={"planner": {"node_id": node_id, "model": "fake"}},
        **kw,
    )


def jobs_of(pid: int) -> list[PlannerJob]:
    with session() as s:
        return list(
            s.exec(
                select(PlannerJob).where(PlannerJob.project_id == pid).order_by(col(PlannerJob.id))
            ).all()
        )


# -- node health ------------------------------------------------------------------


async def test_health_checks_mark_nodes_and_announce_changes(client, llm):
    live = add_node(client, name="live", base_url=llm.url)
    dead = add_node(client, name="dead")
    await check_nodes()
    assert node(live["id"]).healthy is True and node(live["id"]).models == ["fake-coder"]
    assert node(dead["id"]).healthy is False
    assert not events("unreachable")  # first check: nothing changed yet

    llm.stop()
    await check_nodes()
    assert node(live["id"]).healthy is False
    assert events("node live is unreachable")
    assert node(live["id"]).models == ["fake-coder"]  # last known list kept


async def test_unhealthy_node_gets_no_tasks(client, origin):
    n = add_node(client)
    p = make_project(
        client,
        origin,
        executor_kind="opencode",
        role_bindings={"executor": {"node_id": n["id"], "model": "m"}},
    )
    make_task(client, p["id"], script="")
    assert len(pick_ready()) == 1  # never checked: given the benefit of the doubt
    await check_nodes()
    assert pick_ready() == []
    client.patch(f"/api/nodes/{n['id']}", json={"base_url": "http://127.0.0.1:9/v2"})
    with session() as s:  # back up
        row = s.get(Node, n["id"])
        assert row is not None
        row.healthy = True
        s.add(row)
        s.commit()
    assert len(pick_ready()) == 1


async def test_tasks_spread_over_nodes_up_to_capacity(client, origin):
    a = add_node(client, name="a", capacity=2)
    b = add_node(client, name="b", capacity=1)
    for i in range(4):
        target = a if i < 3 else b
        p = make_project(
            client,
            origin,
            name=f"p{i}",
            executor_kind="opencode",
            role_bindings={"executor": {"node_id": target["id"], "model": "m"}},
        )
        make_task(client, p["id"], script="")
    nodes = [nid for _, nid, _ in pick_ready()]
    assert sorted(nodes) == [a["id"], a["id"], b["id"]]  # third project on `a` waits


async def test_planner_jobs_share_node_capacity(client, origin, llm):
    n = add_node(client, name="shared", base_url=llm.url, capacity=1)
    p = planner_project(client, origin, n["id"])
    busy = fake_running_run(p["id"], n["id"])
    assert node_load() == {n["id"]: 1}
    r = client.post(f"/api/projects/{p['id']}/spec/messages", json={"content": "hi"})
    assert r.status_code == 201, r.text

    orch = Orchestrator()
    orch.start_jobs()
    assert not orch.jobs  # the node is full
    nodes = {x["id"]: x for x in client.get("/api/nodes").json()}
    assert nodes[n["id"]]["active"] == 1 and nodes[n["id"]]["waiting"] == 1

    finish(busy)
    await drive(orch, lambda: jobs_of(p["id"])[-1].state == JobState.DONE)


def test_node_settings_can_be_edited(client):
    n = add_node(client, capacity=1)
    r = client.patch(
        f"/api/nodes/{n['id']}", json={"capacity": 3, "price_in": 3.0, "price_out": 15.0}
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["capacity"] == 3 and out["price_out"] == 15.0 and "api_key" not in out
    assert client.patch("/api/nodes/999", json={"capacity": 2}).status_code == 404


# -- spend ------------------------------------------------------------------------

# The fake server reports 10 prompt + 5 completion tokens per call: $1 a call here.
PRICE = {"price_in": 100_000.0, "price_out": 0.0}


def test_cost_and_cap_arithmetic():
    n = Node(name="c", base_url="x", price_in=3.0, price_out=15.0)
    assert spend.cost(n, 1_000_000, 100_000) == pytest.approx(4.5)
    assert not spend.priced(Node(name="l", base_url="x"))
    p = Project(name="p", repo_url="x", spend_usd=5, spend_cap_usd=5)
    assert spend.over_cap(p)
    assert not spend.over_cap(Project(name="p", repo_url="x", spend_usd=50))  # no cap


async def test_planner_spend_reaches_cap_and_waits_for_approval(client, origin, llm):
    n = add_node(client, name="cloud", base_url=llm.url, **PRICE)
    p = planner_project(client, origin, n["id"], spend_cap_usd=1.0)
    pid = p["id"]
    orch = Orchestrator()

    client.post(f"/api/projects/{pid}/spec/messages", json={"content": "one"})
    await drive(orch, lambda: jobs_of(pid)[-1].state == JobState.DONE)
    assert project(pid).spend_usd == pytest.approx(1.0)
    [gate] = [g for g in client.get("/api/gates").json() if g["kind"] == "budget"]
    assert "$1.00" in gate["title"]
    assert spend.blocked(pid, node(n["id"]))
    assert spend.blocked(pid, Node(name="local", base_url="x")) == ""  # local is free

    # A second message waits: the job stays queued while the cap holds.
    client.post(f"/api/projects/{pid}/spec/messages", json={"content": "two"})
    for _ in range(5):
        await orch.tick()
    assert jobs_of(pid)[-1].state == JobState.QUEUED

    client.post(f"/api/gates/{gate['id']}/resolve", json={"approve": True})
    await drive(orch, lambda: jobs_of(pid)[-1].state == JobState.DONE)
    assert project(pid).spend_cap_usd == pytest.approx(2.0)
    assert project(pid).spend_usd == pytest.approx(2.0)
    # Reaching the new cap opens a fresh checkpoint.
    budget = [g for g in client.get("/api/gates").json() if g["kind"] == "budget"]
    assert len(budget) == 1 and budget[0]["id"] != gate["id"]


async def test_rejecting_the_budget_pauses_the_project(client, origin):
    p = make_project(client, origin, spend_cap_usd=1.0)
    pid = p["id"]
    spend.charge(pid, Node(name="c", base_url="x", **PRICE), 10, 5)
    [g] = [g for g in client.get("/api/gates").json() if g["kind"] == "budget"]
    client.post(f"/api/gates/{g['id']}/resolve", json={"approve": False})
    await Orchestrator().tick()
    assert project(pid).paused


def test_raising_the_cap_in_settings_clears_the_checkpoint(client, origin):
    p = make_project(client, origin, spend_cap_usd=1.0)
    pid = p["id"]
    spend.charge(pid, Node(name="c", base_url="x", **PRICE), 10, 5)
    spend.charge(pid, Node(name="c", base_url="x", **PRICE), 10, 5)  # no second gate
    with session() as s:
        gs = s.exec(select(Gate).where(Gate.kind == GateKind.BUDGET)).all()
    assert len(gs) == 1 and gs[0].state == GateState.PENDING
    r = client.patch(f"/api/projects/{pid}", json={"spend_cap_usd": 10})
    assert r.status_code == 200 and r.json()["spend_cap_usd"] == 10
    with session() as s:
        g = s.get(Gate, gs[0].id)
        assert g is not None and g.state == GateState.CANCELLED
    # Lowering it below what was spent opens one again.
    client.patch(f"/api/projects/{pid}", json={"spend_cap_usd": 1})
    assert len([g for g in client.get("/api/gates").json() if g["kind"] == "budget"]) == 1


async def test_tasks_on_priced_nodes_wait_at_the_cap(client, origin):
    n = add_node(client, name="cloud-exec", **PRICE)
    p = make_project(
        client,
        origin,
        executor_kind="opencode",
        spend_cap_usd=1.0,
        role_bindings={"executor": {"node_id": n["id"], "model": "m"}},
    )
    make_task(client, p["id"], script="")
    assert len(pick_ready()) == 1
    spend.charge(p["id"], node(n["id"]), 10, 5)
    assert pick_ready() == []


# -- portfolio ----------------------------------------------------------------------


async def test_progress_series_and_portfolio_fields(client, origin):
    # No milestone checkpoint: it would race the assertion on pending_gates below.
    p = make_project(client, origin, gates={"milestone": False})
    a = make_task(client, p["id"], script="true", title="a")
    make_task(client, p["id"], script="true", title="b", deps=[a["id"]])
    gone = make_task(client, p["id"], script="true", title="gone")
    client.post(f"/api/tasks/{gone['id']}/discard")
    orch = Orchestrator()
    await drive(orch, lambda: sum(t.state == TaskState.DONE for t in tasks_of(p["id"])) == 2)

    points = client.get(f"/api/projects/{p['id']}/progress").json()
    assert points[-1]["done"] == 2 and points[-1]["planned"] == 2
    assert max(x["planned"] for x in points) == 3  # before the discard
    assert [x["done"] for x in points] == sorted(x["done"] for x in points)
    assert all(b["ts"] >= a["ts"] for a, b in zip(points, points[1:], strict=False))

    [row] = client.get("/api/projects").json()
    assert row["pending_gates"] == 0 and row["active_task"] == ""


def tasks_of(pid: int) -> list[Task]:
    with session() as s:
        return list(s.exec(select(Task).where(Task.project_id == pid)).all())
