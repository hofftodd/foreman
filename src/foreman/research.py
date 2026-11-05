"""Web research for the Project Planning Agent on local models.

Claude nodes use Anthropic's own web search and fetch tools (see llm.py). Local
OpenAI-compatible models get two equivalent function tools that Foreman runs
itself, when a search service is configured (``FOREMAN_SEARCH_URL``, a SearXNG
instance with JSON output enabled, e.g. ``http://127.0.0.1:8888``):

- ``web_search(query)``: titles, links and snippets of the top results;
- ``web_fetch(url)``: the text of one public web page.

Fetching only reaches public addresses: localhost, private, link-local and other
reserved networks are refused, so research can't be used to look into your LAN.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

from foreman.config import get_settings

MAX_RESULTS = 6
MAX_PAGE_CHARS = 12_000
MAX_ROUNDS = 8  # tool rounds per planner reply

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web. Returns the top results with title, link and "
            "snippet. Use it to research topics you don't know well.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to search"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Read the text of one public web page (e.g. a search result).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "http(s) URL"}},
                "required": ["url"],
            },
        },
    },
]


def available() -> bool:
    return bool(get_settings().search_url)


async def run_tool(name: str, args: dict[str, Any]) -> str:
    """Run one research tool; errors come back as text the model can read."""
    try:
        if name == "web_search":
            return await search(str(args.get("query", "")))
        if name == "web_fetch":
            return await fetch(str(args.get("url", "")))
        return f"unknown tool {name}"
    except (httpx.HTTPError, ValueError, OSError) as e:
        return f"{name} failed: {e}"


async def search(query: str) -> str:
    if not query.strip():
        raise ValueError("empty query")
    base = get_settings().search_url.rstrip("/")
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{base}/search", params={"q": query, "format": "json"})
        r.raise_for_status()
        results = (r.json().get("results") or [])[:MAX_RESULTS]
    if not results:
        return "no results"
    return "\n\n".join(
        f"{i}. {x.get('title', '').strip()}\n{x.get('url', '')}\n{x.get('content', '').strip()}"
        for i, x in enumerate(results, 1)
    )


def _public(host: str) -> bool:
    """Every address the host resolves to is public."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return True


async def fetch(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("only http(s) URLs")
    if not await asyncio.to_thread(_public, parsed.hostname):
        raise ValueError("only public web addresses can be fetched")
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        r = await client.get(url, headers={"User-Agent": "foreman-planner/1"})
        if r.is_redirect:
            target = r.headers.get("location", "")
            return f"redirects to {target}; fetch that URL if needed"
        r.raise_for_status()
        text = r.text
    if "html" in r.headers.get("content-type", ""):
        text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
        text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_PAGE_CHARS] + (" ..." if len(text) > MAX_PAGE_CHARS else "")
