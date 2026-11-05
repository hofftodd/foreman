"""Web research for the Project Planning Agent (Claude server tools, local tools)."""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from foreman import research
from foreman.core.models import Node
from foreman.llm import stream_chat
from tests.fakellm import FakeLLM


@pytest.fixture
def llm():
    server = FakeLLM(host="127.0.0.1", planner=lambda r: "Based on my research: use SQLite.")
    server.start()
    yield server
    server.stop()


@pytest.fixture
def searx(monkeypatch):
    """A stand-in SearXNG: /search?format=json returns two results."""
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def do_GET(self):  # noqa: N802
            seen.append(self.path)
            body = json.dumps(
                {
                    "results": [
                        {"title": "SQLite", "url": "https://sqlite.org", "content": "embedded"},
                        {"title": "Postgres", "url": "https://postgresql.org", "content": "server"},
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("FOREMAN_SEARCH_URL", f"http://127.0.0.1:{server.server_address[1]}")
    yield seen
    server.shutdown()


async def test_local_planner_searches_through_foreman(llm, searx):
    node = Node(name="local", base_url=llm.url)
    found: list[str] = []
    result = await stream_chat(
        node,
        "m",
        "S",
        [{"role": "user", "content": "storage?"}],
        research=True,
        on_research=found.append,
    )
    assert result.text == "Based on my research: use SQLite."
    assert found == ["searched: x"] and "q=x" in searx[0]
    first, second = llm.requests[-2], llm.requests[-1]
    assert {t["function"]["name"] for t in first["tools"]} == {"web_search", "web_fetch"}
    tool_msg = second["messages"][-1]
    assert tool_msg["role"] == "tool" and "https://sqlite.org" in tool_msg["content"]


async def test_no_search_service_means_no_tools(llm, monkeypatch):
    monkeypatch.delenv("FOREMAN_SEARCH_URL", raising=False)
    node = Node(name="local", base_url=llm.url)
    await stream_chat(node, "m", "S", [{"role": "user", "content": "x"}], research=True)
    assert "tools" not in llm.requests[-1]


async def test_claude_gets_server_side_research_tools_and_resumes_paused_turns(llm):
    node = Node(
        name="claude", provider="anthropic", base_url=llm.url.removesuffix("/v1"), api_key="k"
    )
    llm.anthropic_stops = ["pause_turn"]
    result = await stream_chat(
        node, "claude-opus-5-5", "S", [{"role": "user", "content": "x"}], research=True
    )
    first, second = llm.requests[-2], llm.requests[-1]
    assert [t["type"] for t in first["tools"]] == ["web_search_20260209", "web_fetch_20260209"]
    # The paused turn goes back as the assistant's message, with no extra user turn.
    assert [m["role"] for m in second["messages"]] == ["user", "assistant"]
    assert result.text.count("Based on my research") == 2 and result.stop_reason == "end_turn"


async def test_planner_without_research_has_no_tools(llm):
    node = Node(
        name="claude", provider="anthropic", base_url=llm.url.removesuffix("/v1"), api_key="k"
    )
    await stream_chat(node, "claude-opus-5-5", "S", [{"role": "user", "content": "x"}])
    assert "tools" not in llm.requests[-1]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8700/api/nodes",
        "http://localhost/",
        "http://10.0.0.5/x",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
    ],
)
async def test_fetch_refuses_private_addresses(url):
    out = await research.run_tool("web_fetch", {"url": url})
    assert out.startswith("web_fetch failed:")
