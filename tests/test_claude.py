"""Claude nodes as executors: through the gateway on the native Messages API."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from foreman.core.models import Node, Project, Task, TaskRun, TaskState
from foreman.db import session
from foreman.executors import EXECUTORS
from foreman.gateway import Gateway, new_token, serve
from foreman.orchestrator.main import Orchestrator
from tests.fakellm import FakeLLM
from tests.helpers import TERMINAL, drive, make_project, make_task, project, task


@pytest.fixture
def llm():
    server = FakeLLM(host="127.0.0.1", planner=lambda r: "claude says hi").start()
    yield server
    server.stop()


@pytest.fixture
async def gw():
    gateway = Gateway()
    server, port = await serve(gateway, "127.0.0.1", 0)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await gateway.aclose()


def claude_run(llm: FakeLLM, **project_kw) -> TaskRun:
    with session() as s:
        node = Node(
            name="claude",
            provider="anthropic",
            base_url=llm.url.removesuffix("/v1"),  # the API root, like api.anthropic.com
            api_key="sk-real-key",
            price_in=1.0,
            price_out=5.0,
        )
        p = Project(name="p", repo_url="x", **project_kw)
        s.add(node)
        s.add(p)
        s.commit()
        t = Task(project_id=p.id, title="t", state=TaskState.RUNNING)
        s.add(t)
        s.commit()
        run = TaskRun(
            task_id=t.id, project_id=p.id, node_id=node.id, model="m", gateway_token=new_token()
        )
        s.add(run)
        s.commit()
        s.refresh(run)
        return run


def messages(base: str, run: TaskRun, body: dict) -> httpx.Response:
    return httpx.post(
        f"{base}/g/{run.gateway_token}/v1/messages",
        json={"model": "claude-haiku-5-5", "max_tokens": 1000, **body},
        headers={"x-api-key": "whatever-the-agent-has", "anthropic-version": "2023-06-01"},
        timeout=10,
    )


def reload(run: TaskRun) -> TaskRun:
    with session() as s:
        r = s.get(TaskRun, run.id)
        assert r is not None
        return r


@pytest.mark.parametrize("stream", [False, True])
async def test_messages_forwarded_with_host_side_key(gw, llm, stream):
    run = claude_run(llm)
    r = await asyncio.to_thread(
        messages,
        gw,
        run,
        {
            "stream": stream,
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 2048},
            "temperature": 0.2,
        },
    )
    assert r.status_code == 200, r.text
    assert "claude says hi" in r.text
    sent, headers = llm.requests[-1], llm.headers[-1]
    assert headers["x-api-key"] == "sk-real-key"  # replaced on the host
    assert "Authorization" not in headers
    assert headers["anthropic-version"] == "2023-06-01"  # the agent's headers pass through
    # Old-model parameters that current Claude models reject are adapted.
    assert sent["thinking"] == {"type": "adaptive"} and "temperature" not in sent
    row = reload(run)
    assert row.model_requests == 1
    assert (row.tokens_in, row.tokens_out) == (10, 5)  # from the Messages usage block


async def test_tool_use_counts_and_charges(gw, llm):
    run = claude_run(llm, spend_cap_usd=100.0)
    body = {
        "stream": True,
        "messages": [{"role": "user", "content": "write it"}],
        "tools": [{"name": "write", "input_schema": {"type": "object"}}],
    }
    r = await asyncio.to_thread(messages, gw, run, body)
    events = [json.loads(x[5:]) for x in r.text.splitlines() if x.startswith("data:")]
    assert any(e.get("delta", {}).get("type") == "input_json_delta" for e in events)
    p = project(run.project_id)
    assert p.spend_usd == pytest.approx((10 * 1.0 + 5 * 5.0) / 1_000_000)


def test_cache_tokens_are_billed_at_their_rates():
    from foreman.gateway import _claude_usage

    usage = {
        "input_tokens": 100,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 10_000,
        "output_tokens": 7,
    }
    tokens_in, tokens_out, billed = _claude_usage(usage)
    assert tokens_in == 11_100 and tokens_out == 7
    assert billed == pytest.approx(100 + 1250 + 1000)


def test_opencode_uses_its_anthropic_provider(tmp_path):
    inv = EXECUTORS["opencode"].prepare(
        Project(name="p", repo_url="x"),
        Task(project_id=1, title="t"),
        "http://gw/g/tok/v1",
        "claude-haiku-5-5",
        tmp_path,
        "go",
        provider="anthropic",
    )
    cfg = json.loads((tmp_path / "opencode.json").read_text())
    assert cfg["model"] == "anthropic/claude-haiku-5-5"
    assert cfg["provider"] == {
        "anthropic": {"options": {"baseURL": "http://gw/g/tok/v1", "apiKey": "foreman"}}
    }
    assert inv.env["FOREMAN_MODEL"] == "anthropic/claude-haiku-5-5"
    assert "sk-" not in json.dumps(cfg) + json.dumps(inv.env)


async def test_claude_node_can_be_bound_as_executor_but_not_for_aider(client, origin, llm):
    n = client.post(
        "/api/nodes",
        json={"name": "claude", "provider": "anthropic", "base_url": llm.url, "api_key": "k"},
    ).json()
    p = make_project(
        client,
        origin,
        executor_kind="aider",
        watchdog={"escalation": True, "split": False},
        role_bindings={"executor": {"node_id": n["id"], "model": "claude-haiku-5-5"}},
    )
    t = make_task(client, p["id"], script="")
    await drive(Orchestrator(), lambda: task(t["id"]).state in (*TERMINAL, TaskState.BLOCKED))
    row = task(t["id"])
    assert row.state == TaskState.BLOCKED
    assert "aider executor can't use node claude" in row.reason


def test_claude_nodes_setup_binds_roles_and_is_idempotent(client, origin):
    from foreman.claude_setup import setup

    p = make_project(client, origin)  # shell executor
    nodes = setup("sk-test", project_id=p["id"])
    assert {r: n.name for r, n in nodes.items()} == {
        "project_planner": "claude-opus",
        "task_planner": "claude-opus",
        "tester": "claude-sonnet",
        "escalation": "claude-sonnet",
        "builder": "claude-haiku",
    }
    assert nodes["builder"].price_in == 0.10 and nodes["project_planner"].price_out == 20.0
    row = project(p["id"])
    assert row.executor_kind == "opencode"
    assert row.role_bindings["builder"] == {
        "node_id": nodes["builder"].id,
        "model": "claude-haiku-5-5",
    }
    assert row.role_bindings["tester"]["model"] == "claude-sonnet-5-5"
    again = setup("sk-rotated")
    assert again["project_planner"].id == nodes["project_planner"].id  # updated, not duplicated
    listed = client.get("/api/nodes").json()
    assert len(listed) == 3 and not any("api_key" in n for n in listed)
    with pytest.raises(ValueError):
        setup("")


def test_claude_nodes_cli_needs_the_key_in_the_environment(monkeypatch, capsys):
    import sys

    from foreman import cli

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["foreman", "claude-nodes"])
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        cli.main()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    cli.main()
    out = capsys.readouterr().out
    assert "claude-haiku" in out and "sk-secret-value" not in out
