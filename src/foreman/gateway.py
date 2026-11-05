"""Model gateway: executors reach their model through the orchestrator.

Each task run gets a secret URL, ``/g/<token>/v1``, configured as the
executor's OpenAI-compatible base URL. The gateway forwards requests to the
run's node and, on the way:

- refuses calls once the run is stopped, finished or tripped;
- counts model requests and tokens against the watchdog budgets (FR-5.1);
- spots the agent returning the same response over and over (FR-5.2);
- adds the node's API key itself, so keys never enter the sandbox (NFR-5);
- charges priced nodes to the project and refuses calls past its spend cap.

OpenAI-compatible nodes are reached at ``/g/<token>/v1/chat/completions``;
Claude nodes (provider "anthropic") at ``/g/<token>/v1/messages``, the native
Messages API, with the key sent as ``x-api-key`` from here.

When a limit is hit it records the reason on the run (``TaskRun.watchdog``)
and answers with an error; the runner sees the trip and kills the container.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

import httpx
from sqlalchemy import update
from sqlmodel import col, select
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from foreman.core import spend, watchdog
from foreman.core.contextpack import estimate_tokens
from foreman.core.models import Node, Project, RunState, Task, TaskRun, TaskState
from foreman.core.state import ACTIVE_STATES, emit, stop_requested
from foreman.db import session

log = logging.getLogger("foreman.gateway")

HOP_HEADERS = {
    "host",
    "content-length",
    "authorization",
    "x-api-key",
    "connection",
    "accept-encoding",
    "transfer-encoding",
}


def new_token() -> str:
    return secrets.token_urlsafe(24)


def trip(run_id: int, reason: str) -> bool:
    """Record a watchdog trip on a run (first reason wins)."""
    with session() as s:
        res = s.exec(  # type: ignore[call-overload]
            update(TaskRun)
            .where(col(TaskRun.id) == run_id, col(TaskRun.watchdog) == "")
            .values(watchdog=reason)
        )
        s.commit()
        run = s.get(TaskRun, run_id)
    if res.rowcount and run is not None:
        emit(run.project_id, run.task_id, "watchdog", f"watchdog: {reason}", run_id=run_id)
        return True
    return False


class _RunInfo:
    def __init__(self, run: TaskRun, task: Task, project: Project, node: Node):
        self.run, self.task, self.project, self.node = run, task, project, node


def _lookup(token: str) -> _RunInfo | None:
    with session() as s:
        run = s.exec(select(TaskRun).where(TaskRun.gateway_token == token)).first()
        if run is None or run.node_id is None:
            return None
        task = s.get(Task, run.task_id)
        project = s.get(Project, run.project_id)
        node = s.get(Node, run.node_id)
    if task is None or project is None or node is None:
        return None
    return _RunInfo(run, task, project, node)


def _refusal(info: _RunInfo | None) -> str:
    if info is None:
        return "unknown run"
    run, task = info.run, info.task
    if run.state != RunState.RUNNING or task.state not in ACTIVE_STATES:
        return "this run is no longer active"
    if run.watchdog:
        return f"stopped by the watchdog: {run.watchdog}"
    if task.state == TaskState.STOPPED_BY_USER or stop_requested(
        run.project_id, run.task_id, run.started_at
    ):
        return "stopped by the user"
    return ""


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": f"foreman gateway: {message}", "type": "foreman_gateway"}},
        status_code=status,
    )


class Gateway:
    def __init__(self) -> None:
        self.signatures: dict[int, list[str]] = defaultdict(list)
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=900.0, write=60.0, pool=30.0)
        )
        self.app = Starlette(
            routes=[
                Route("/g/{token}/{path:path}", self.proxy, methods=["GET", "POST"]),
                Route("/health", lambda r: JSONResponse({"ok": True})),
            ]
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    # -- accounting -----------------------------------------------------------

    def _count_request(self, info: _RunInfo) -> str:
        """Count one model call; returns a trip reason if over budget."""
        with session() as s:
            run = s.get(TaskRun, info.run.id)
            assert run is not None
            run.model_requests += 1
            s.add(run)
            s.commit()
            n = run.model_requests
        cap = watchdog.limit(info.project, "max_requests")
        if n > cap:
            return f"model request budget exceeded ({cap} calls)"
        return ""

    def _record(
        self,
        info: _RunInfo,
        tokens_in: int,
        tokens_out: int,
        content: str,
        tool_calls: list[dict[str, Any]],
        billed_in: float | None = None,
    ) -> None:
        run_id = info.run.id
        assert run_id is not None
        with session() as s:
            run = s.get(TaskRun, run_id)
            assert run is not None
            run.tokens_in += tokens_in
            run.tokens_out += tokens_out
            s.add(run)
            s.commit()
            total = run.tokens_in + run.tokens_out
        spend.charge(
            info.run.project_id,
            info.node,
            round(billed_in if billed_in is not None else tokens_in),
            tokens_out,
        )
        cap = watchdog.limit(info.project, "max_tokens")
        if total > cap:
            trip(run_id, f"token budget exceeded ({total:,} > {cap:,})")
            return
        sigs = self.signatures[run_id]
        sigs.append(watchdog.response_signature(content, tool_calls))
        n = watchdog.limit(info.project, "repeat_limit")
        if watchdog.repeated(sigs, n):
            trip(run_id, f"the model returned the same response {n} times in a row")

    # -- proxy ----------------------------------------------------------------

    async def proxy(self, request: Request) -> Response:
        try:
            return await self._proxy(request)
        except httpx.HTTPError as e:
            return _error(502, f"cannot reach the model node: {type(e).__name__}: {e}")

    async def _proxy(self, request: Request) -> Response:
        token = request.path_params["token"]
        path = request.path_params["path"]
        info = _lookup(token)
        refusal = _refusal(info)
        if refusal:
            return _error(403, refusal)
        assert info is not None and info.run.id is not None
        claude = info.node.provider == "anthropic"
        base = info.node.base_url.rstrip("/")
        if claude:
            # Claude's base URL is the API root (https://api.anthropic.com); keep our /v1.
            upstream = f"{base.removesuffix('/v1')}/{path.lstrip('/')}"
        else:
            # The executor's base URL is .../g/<token>/v1; the node's base URL already ends
            # in the API version (e.g. http://host:8080/v1), so drop ours.
            upstream = f"{base}/{path.removeprefix('v1/').lstrip('/')}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
        if info.node.api_key:
            if claude:
                headers["x-api-key"] = info.node.api_key
            else:
                headers["Authorization"] = f"Bearer {info.node.api_key}"

        if request.method == "GET":
            r = await self.client.get(upstream, headers=headers, params=request.query_params)
            return Response(
                r.content, status_code=r.status_code, media_type=r.headers.get("content-type")
            )

        body = await request.body()
        endpoint = "messages" if claude else "chat/completions"
        is_chat = path.rstrip("/").endswith(endpoint)
        if not is_chat:
            r = await self.client.post(upstream, headers=headers, content=body)
            return Response(
                r.content, status_code=r.status_code, media_type=r.headers.get("content-type")
            )

        reason = spend.blocked(info.run.project_id, info.node) or self._count_request(info)
        if reason:
            trip(info.run.id, reason)
            return _error(429, reason)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return _error(400, "invalid JSON")
        prompt_estimate = estimate_tokens(json.dumps(payload.get("messages", [])))
        if claude:
            _normalize_claude(payload)
            return await self._claude(info, upstream, headers, payload, prompt_estimate)
        if payload.get("stream"):
            payload.setdefault("stream_options", {})["include_usage"] = True
            return await self._stream(info, upstream, headers, payload, prompt_estimate)

        r = await self.client.post(upstream, headers=headers, json=payload)
        if r.status_code < 400:
            data = r.json()
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            usage = data.get("usage") or {}
            content = msg.get("content") or ""
            self._record(
                info,
                usage.get("prompt_tokens") or prompt_estimate,
                usage.get("completion_tokens") or estimate_tokens(content),
                content,
                msg.get("tool_calls") or [],
            )
        return Response(
            r.content, status_code=r.status_code, media_type=r.headers.get("content-type")
        )

    async def _stream(
        self,
        info: _RunInfo,
        upstream: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        prompt_estimate: int,
    ) -> Response:
        req = self.client.build_request("POST", upstream, headers=headers, json=payload)
        resp = await self.client.send(req, stream=True)
        if resp.status_code >= 400:
            content = await resp.aread()
            await resp.aclose()
            return Response(
                content, status_code=resp.status_code, media_type=resp.headers.get("content-type")
            )

        async def relay() -> AsyncIterator[bytes]:
            content: list[str] = []
            calls: dict[int, dict[str, Any]] = {}
            usage: dict[str, int] = {}
            started = time.monotonic()
            buf = b""
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        _parse_sse_line(line, content, calls, usage)
            finally:
                await resp.aclose()
                text = "".join(content)
                tool_calls = [calls[i] for i in sorted(calls)]
                self._record(
                    info,
                    usage.get("prompt_tokens") or prompt_estimate,
                    usage.get("completion_tokens")
                    or estimate_tokens(text + json.dumps(tool_calls)),
                    text,
                    tool_calls,
                )
                log.debug("run %s call took %.1fs", info.run.id, time.monotonic() - started)

        return StreamingResponse(
            relay(),
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "text/event-stream"),
        )

    async def _claude(
        self,
        info: _RunInfo,
        upstream: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        prompt_estimate: int,
    ) -> Response:
        """Forward one Messages API call, streamed or not, and account for it."""

        def record(blocks: list[dict[str, Any]], usage: dict[str, Any]) -> None:
            text, calls = _claude_blocks(blocks)
            tokens_in, tokens_out, billed = _claude_usage(usage)
            if not tokens_in:
                tokens_in = billed = prompt_estimate
            self._record(
                info,
                tokens_in,
                tokens_out or estimate_tokens(text + json.dumps(calls)),
                text,
                calls,
                billed_in=billed,
            )

        if not payload.get("stream"):
            r = await self.client.post(upstream, headers=headers, json=payload)
            if r.status_code < 400:
                data = r.json()
                record(data.get("content") or [], data.get("usage") or {})
            return Response(
                r.content, status_code=r.status_code, media_type=r.headers.get("content-type")
            )

        req = self.client.build_request("POST", upstream, headers=headers, json=payload)
        resp = await self.client.send(req, stream=True)
        if resp.status_code >= 400:
            content = await resp.aread()
            await resp.aclose()
            return Response(
                content, status_code=resp.status_code, media_type=resp.headers.get("content-type")
            )

        async def relay() -> AsyncIterator[bytes]:
            acc = _ClaudeStream()
            buf = b""
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        acc.feed(line)
            finally:
                await resp.aclose()
                record(acc.result(), acc.usage)

        return StreamingResponse(
            relay(),
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "text/event-stream"),
        )


def _normalize_claude(payload: dict[str, Any]) -> None:
    """Adapt a coding agent's Messages request to current Claude models.

    Agents written for older models send fixed thinking budgets and sampling
    parameters, which current models reject with a 400. Adaptive thinking (the
    models' default) replaces both.
    """
    for key in ("temperature", "top_p", "top_k"):
        payload.pop(key, None)
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") in ("enabled", "disabled"):
        payload["thinking"] = {"type": "adaptive"}


def _claude_usage(usage: dict[str, Any]) -> tuple[int, int, float]:
    """(input tokens, output tokens, input tokens as billed) from a Messages usage block.

    Cache writes bill at 1.25x the input price and cache reads at 0.1x.
    """
    plain = int(usage.get("input_tokens") or 0)
    written = int(usage.get("cache_creation_input_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    return (
        plain + written + read,
        int(usage.get("output_tokens") or 0),
        (plain + 1.25 * written + 0.1 * read),
    )


def _claude_blocks(blocks: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Text and tool calls (in the OpenAI shape the repeat detector uses)."""
    text = "".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
    calls = [
        {"function": {"name": b.get("name", ""), "arguments": json.dumps(b.get("input"))}}
        for b in blocks
        if b.get("type") == "tool_use"
    ]
    return text, calls


class _ClaudeStream:
    """Accumulates a streamed Messages response (server-sent events)."""

    def __init__(self) -> None:
        self.blocks: dict[int, dict[str, Any]] = {}
        self.usage: dict[str, Any] = {}

    def feed(self, line: bytes) -> None:
        text = line.decode(errors="replace").strip()
        if not text.startswith("data:"):
            return
        try:
            event = json.loads(text[5:].strip())
        except json.JSONDecodeError:
            return
        kind = event.get("type")
        if kind == "message_start":
            self.usage.update((event.get("message") or {}).get("usage") or {})
        elif kind == "message_delta":
            self.usage.update({k: v for k, v in (event.get("usage") or {}).items() if v})
        elif kind == "content_block_start":
            block = dict(event.get("content_block") or {})
            block["_json"] = ""
            self.blocks[event.get("index", 0)] = block
        elif kind == "content_block_delta":
            block = self.blocks.setdefault(event.get("index", 0), {"_json": ""})
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif delta.get("type") == "input_json_delta":
                block["_json"] += delta.get("partial_json", "")

    def result(self) -> list[dict[str, Any]]:
        out = []
        for i in sorted(self.blocks):
            block = dict(self.blocks[i])
            raw = block.pop("_json", "")
            if block.get("type") == "tool_use" and raw:
                try:
                    block["input"] = json.loads(raw)
                except json.JSONDecodeError:
                    block["input"] = raw
            out.append(block)
        return out


def _parse_sse_line(
    line: bytes, content: list[str], calls: dict[int, dict[str, Any]], usage: dict[str, int]
) -> None:
    text = line.decode(errors="replace").strip()
    if not text.startswith("data:"):
        return
    data = text[5:].strip()
    if not data or data == "[DONE]":
        return
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return
    if chunk.get("usage"):
        usage.update({k: v for k, v in chunk["usage"].items() if isinstance(v, int)})
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or {}
        if delta.get("content"):
            content.append(delta["content"])
        for tc in delta.get("tool_calls") or []:
            slot = calls.setdefault(tc.get("index", 0), {"function": {"name": "", "arguments": ""}})
            fn = tc.get("function") or {}
            slot["function"]["name"] += fn.get("name") or ""
            slot["function"]["arguments"] += fn.get("arguments") or ""


async def serve(gateway: Gateway, host: str, port: int) -> tuple[Any, int]:
    """Start the gateway on the running event loop; returns (server, bound port)."""
    import asyncio

    import uvicorn

    config = uvicorn.Config(gateway.app, host=host, port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        if task.done():
            task.result()  # surfaces bind errors
        await asyncio.sleep(0.02)
    bound = server.servers[0].sockets[0].getsockname()[1] if server.servers else port
    return server, bound
