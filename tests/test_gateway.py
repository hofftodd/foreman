"""Model gateway: accounting, budgets, repeat detection, refusals (FR-5.1/5.2, NFR-5)."""

from __future__ import annotations

import json

import httpx
import pytest

from foreman.core.models import Node, Project, RunState, Task, TaskRun, TaskState
from foreman.core.state import transition
from foreman.db import session
from foreman.gateway import Gateway, new_token, serve
from tests.fakellm import FakeLLM


@pytest.fixture
def llm():
    server = FakeLLM(host="127.0.0.1", planner=lambda r: "same answer").start()
    yield server
    server.stop()


@pytest.fixture
async def gw():
    gateway = Gateway()
    server, port = await serve(gateway, "127.0.0.1", 0)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await gateway.aclose()


def make_run(llm: FakeLLM, watchdog: dict | None = None, api_key: str = "") -> TaskRun:
    with session() as s:
        node = Node(name="n", base_url=llm.url, api_key=api_key)
        p = Project(name="p", repo_url="x", watchdog=watchdog or {})
        s.add(node)
        s.add(p)
        s.commit()
        t = Task(project_id=p.id, title="t", validation_cmd="true", state=TaskState.RUNNING)
        s.add(t)
        s.commit()
        run = TaskRun(
            task_id=t.id, project_id=p.id, node_id=node.id, model="m", gateway_token=new_token()
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        return run


def chat(base: str, run: TaskRun, stream: bool = False, content: str = "hi") -> httpx.Response:
    return httpx.post(
        f"{base}/g/{run.gateway_token}/v1/chat/completions",
        json={"model": "m", "stream": stream, "messages": [{"role": "user", "content": content}]},
        timeout=10,
    )


def reload(run: TaskRun) -> TaskRun:
    with session() as s:
        r = s.get(TaskRun, run.id)
        assert r is not None
        return r


async def test_forwards_and_counts(gw, llm):
    run = make_run(llm)
    r = await _a(chat, gw, run)
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "same answer"
    r = await _a(chat, gw, run, True)
    assert r.status_code == 200
    streamed = "".join(
        json.loads(line[5:])["choices"][0]["delta"].get("content") or ""
        for line in r.text.splitlines()
        if line.startswith("data:") and "[DONE]" not in line and json.loads(line[5:]).get("choices")
    )
    assert streamed == "same answer"  # relayed intact
    row = reload(run)
    assert row.model_requests == 2
    assert row.tokens_in == 20 and row.tokens_out == 10  # usage from the server
    # Streaming requests ask the server for usage.
    assert llm.requests[-1]["stream_options"] == {"include_usage": True}


async def _a(fn, *args):
    import asyncio

    return await asyncio.to_thread(fn, *args)


async def test_api_key_added_host_side(gw, llm):
    run = make_run(llm, api_key="secret-node-key")
    r = await _a(
        lambda: httpx.post(
            f"{gw}/g/{run.gateway_token}/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": "Bearer whatever-the-agent-sends"},
            timeout=10,
        )
    )
    assert r.status_code == 200
    assert llm.headers[-1]["Authorization"] == "Bearer secret-node-key"


async def test_unknown_token_and_inactive_run_are_refused(gw, llm):
    run = make_run(llm)
    r = await _a(chat, gw, TaskRun(task_id=0, project_id=0, gateway_token="nope"))
    assert r.status_code == 403
    transition(run.task_id, (TaskState.RUNNING,), TaskState.STOPPED_BY_USER, "stop")
    r = await _a(chat, gw, run)
    assert r.status_code == 403 and "no longer active" in r.text


async def test_request_budget_trips_watchdog(gw, llm):
    run = make_run(llm, {"max_requests": 2, "repeat_limit": 99})
    for _ in range(2):
        assert (await _a(chat, gw, run)).status_code == 200
    r = await _a(chat, gw, run)
    assert r.status_code == 429 and "request budget" in r.text
    assert "request budget exceeded" in reload(run).watchdog
    # Once tripped, everything is refused.
    assert (await _a(chat, gw, run)).status_code == 403


async def test_token_budget_trips_watchdog(gw, llm):
    run = make_run(llm, {"max_tokens": 20, "repeat_limit": 99})
    await _a(chat, gw, run)
    await _a(chat, gw, run)
    assert "token budget exceeded" in reload(run).watchdog


@pytest.mark.parametrize("stream", [False, True])
async def test_repeated_responses_trip_watchdog(gw, llm, stream):
    run = make_run(llm, {"repeat_limit": 3})
    for _ in range(3):
        await _a(chat, gw, run, stream)
    assert "same response 3 times" in reload(run).watchdog


async def test_varied_responses_do_not_trip(gw, llm):
    answers = iter(["a", "b", "c", "d"])
    llm.planner = lambda r: next(answers)
    run = make_run(llm, {"repeat_limit": 3})
    for _ in range(4):
        await _a(chat, gw, run)
    assert reload(run).watchdog == ""


async def test_unreachable_node_is_a_clear_502(gw):
    with session() as s:
        node = Node(name="dead", base_url="http://127.0.0.1:9/v1")
        p = Project(name="p", repo_url="x")
        s.add(node)
        s.add(p)
        s.commit()
        t = Task(project_id=p.id, title="t", state=TaskState.RUNNING)
        s.add(t)
        s.commit()
        run = TaskRun(task_id=t.id, project_id=p.id, node_id=node.id, gateway_token=new_token())
        s.add(run)
        s.commit()
        s.refresh(run)
    r = await _a(chat, gw, run)
    assert r.status_code == 502 and "cannot reach" in r.text


async def test_models_passthrough(gw, llm):
    run = make_run(llm)
    r = await _a(lambda: httpx.get(f"{gw}/g/{run.gateway_token}/v1/models", timeout=10))
    assert r.status_code == 200 and r.json()["data"][0]["id"] == "fake-coder"
    assert reload(run).model_requests == 0  # only chat calls count


async def test_priced_node_charges_and_stops_at_cap(gw, llm):
    run = make_run(llm, {"repeat_limit": 99})
    with session() as s:
        node = s.get(Node, run.node_id)
        p = s.get(Project, run.project_id)
        assert node is not None and p is not None
        node.price_in = 100_000.0  # 10 prompt tokens a call -> $1
        p.spend_cap_usd = 1.0
        s.add(node)
        s.add(p)
        s.commit()
    assert (await _a(chat, gw, run)).status_code == 200
    with session() as s:
        p = s.get(Project, run.project_id)
        assert p is not None and p.spend_usd == pytest.approx(1.0)
    r = await _a(chat, gw, run)
    assert r.status_code == 429 and "spend cap reached" in r.text
    assert reload(run).watchdog.startswith("spend cap reached")


def test_budget_failure_requeues_without_counting():
    from foreman.orchestrator.escalation import after_failure

    with session() as s:
        p = Project(name="p", repo_url="x")
        s.add(p)
        s.commit()
        t = Task(project_id=p.id, title="t", state=TaskState.FAILED)
        s.add(t)
        s.commit()
        run = TaskRun(task_id=t.id, project_id=p.id, failure_kind="budget", state=RunState.FAILED)
        s.add(run)
        s.commit()
        tid, rid = t.id, run.id
    d = after_failure(tid, rid)
    assert d is not None and d.action == "retry"
    with session() as s:
        t = s.get(Task, tid)
        assert t is not None and t.state == TaskState.QUEUED and t.level_failures == 0
