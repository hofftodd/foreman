"""Live smoke test on Claude: one small project from idea to done, through the API.

Runs against a Foreman that is already up (`foreman up`), with real models:
Opus 5.5 plans (Project and Task Planner), Sonnet 5.5 writes the tests and takes
escalations, Haiku 5.5 writes the code.
The project gets a spend cap, so a runaway costs at most that much.

    export ANTHROPIC_API_KEY=sk-ant-...
    uv run foreman up &                      # in another terminal
    uv run python scripts/claude_smoke.py    # --cap 3 --url http://127.0.0.1:8700

By default it creates a throwaway local git repository to work in; pass
--repo to use your own (Foreman never pushes unless you approve the milestone).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from foreman.claude_setup import API_ROOT, NODES

IDEA = """Build a tiny Python command-line tool, `wordfreq`, that prints the N most
common words in a text file (case-insensitive, punctuation ignored), with unit
tests using only the standard library (unittest). Keep it small: two or three
modules at most. Please don't ask me questions: make sensible assumptions, then
write the requirements, design and acceptance criteria, all in this reply."""

NUDGE = """Please write any planning documents that are still missing (requirements,
design, acceptance criteria) now."""


class Foreman:
    def __init__(self, url: str) -> None:
        token = os.environ.get("FOREMAN_TOKEN", "")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = httpx.Client(base_url=url, headers=headers, timeout=60)

    def call(self, method: str, path: str, **kw: Any) -> Any:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise SystemExit(f"{method} {path}: {r.status_code} {r.text}")
        return r.json() if r.content else None

    def project(self, pid: int) -> dict[str, Any]:
        return self.call("GET", f"/api/projects/{pid}")

    def wait_jobs(self, pid: int, timeout: float = 900) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jobs = self.call("GET", f"/api/projects/{pid}/jobs")
            latest = max(jobs, key=lambda j: j["id"]) if jobs else None
            if latest and latest["state"] not in ("queued", "running"):
                return latest
            time.sleep(3)
        raise SystemExit("timed out waiting for the planner")


def local_repo() -> str:
    """A bare repository with one commit, standing in for GitHub."""
    root = Path(tempfile.mkdtemp(prefix="foreman-smoke-"))
    bare, seed = root / "wordfreq.git", root / "seed"
    git = ["git", "-c", "user.name=smoke", "-c", "user.email=smoke@local"]
    subprocess.run([*git, "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    subprocess.run([*git, "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "README.md").write_text("# wordfreq\n")
    subprocess.run([*git, "add", "-A"], cwd=seed, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=seed, check=True)
    subprocess.run([*git, "push", "-q", str(bare), "main"], cwd=seed, check=True)
    return str(bare)


def ensure_nodes(f: Foreman, key: str, api_root: str) -> dict[str, dict[str, Any]]:
    """The three Claude nodes, created or updated through the API."""
    existing = {n["name"]: n for n in f.call("GET", "/api/nodes")}
    out = {}
    for spec in NODES:
        body = {
            "base_url": api_root,
            "api_key": key,
            "capacity": spec.capacity,
            "price_in": spec.price_in,
            "price_out": spec.price_out,
            "context_window": 1_000_000,
        }
        if spec.name in existing:
            node = f.call("PATCH", f"/api/nodes/{existing[spec.name]['id']}", json=body)
        else:
            node = f.call(
                "POST", "/api/nodes", json={**body, "name": spec.name, "provider": "anthropic"}
            )
        checked = f.call("POST", f"/api/nodes/{node['id']}/check")
        if not checked["healthy"]:
            raise SystemExit(f"{spec.name}: the Claude API rejected the key: {checked['error']}")
        for role in spec.roles:
            out[role] = node
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8700")
    ap.add_argument("--repo", help="git URL to work in (default: a throwaway local repo)")
    ap.add_argument("--cap", type=float, default=3.0, help="spend cap in USD (default 3)")
    ap.add_argument("--idea", default=IDEA)
    ap.add_argument("--timeout", type=float, default=3600, help="seconds for the build")
    ap.add_argument("--api-root", default=API_ROOT, help=argparse.SUPPRESS)  # for dry runs
    args = ap.parse_args()

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        sys.exit("set ANTHROPIC_API_KEY first")
    f = Foreman(args.url)
    nodes = ensure_nodes(f, key, args.api_root)
    print("nodes: " + ", ".join(f"{r}={n['name']}" for r, n in nodes.items()))

    roles = {
        role: {"node_id": nodes[role]["id"], "model": spec.model}
        for spec in NODES
        for role in spec.roles
    }
    p = f.call(
        "POST",
        "/api/projects",
        json={
            "name": f"claude-smoke-{time.strftime('%H%M%S')}",
            "repo_url": args.repo or local_repo(),
            "executor_kind": "opencode",
            "role_bindings": roles,
            "spend_cap_usd": args.cap,
            "gates": {"milestone": False},  # don't push anywhere
        },
    )
    pid = p["id"]
    print(f"project {pid}: {args.url}/projects/{pid}  (spend cap ${args.cap:.2f})")

    # Phase 1: spec, plan, validation, acceptance command.
    message = args.idea
    for _ in range(3):
        f.call("POST", f"/api/projects/{pid}/spec/messages", json={"content": message})
        job = f.wait_jobs(pid)
        if job["state"] != "done":
            sys.exit(f"planner failed: {job.get('error')}")
        spec = f.call("GET", f"/api/projects/{pid}/spec")
        docs = spec.get("docs") or {}
        wanted = ("requirements", "design", "acceptance")
        missing = [d for d in wanted if not docs.get(d, "").strip()]
        if not missing:
            break
        message = NUDGE
    else:
        sys.exit("the planner didn't produce all documents; see the dashboard")
    print("planning documents ready")

    # Breakdown, then plan approval.
    f.call("POST", f"/api/projects/{pid}/spec/approve")
    f.wait_jobs(pid)
    project = f.project(pid)
    if project["phase"] != "plan_review":
        sys.exit(f"breakdown stopped in phase {project['phase']}; see the dashboard")
    tasks = f.call("GET", f"/api/projects/{pid}/tasks")
    print(f"plan: {len(tasks)} tasks")
    for t in tasks:
        print(f"  - {t['title']}")
    f.call("POST", f"/api/projects/{pid}/plan/approve")

    # Execution: Haiku codes, Opus reviews after each task.
    deadline = time.monotonic() + args.timeout
    last = ""
    while time.monotonic() < deadline:
        project = f.project(pid)
        tasks = f.call("GET", f"/api/projects/{pid}/tasks")
        gates = f.call("GET", "/api/gates", params={"project_id": pid})
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t["state"]] = counts.get(t["state"], 0) + 1
        line = (
            f"phase {project['phase']} | "
            + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
            + f" | spent ${project['spend_usd']:.2f}"
        )
        if line != last:
            print(line)
            last = line
        if project["phase"] == "done":
            break
        if gates:
            print("waiting on you: " + "; ".join(g["title"] for g in gates))
            print(f"decide in the inbox ({args.url}/inbox), then rerun or watch the dashboard")
            break
        time.sleep(5)

    project = f.project(pid)
    print(
        f"\nresult: phase {project['phase']}, spent ${project['spend_usd']:.2f} "
        f"of ${project['spend_cap_usd']:.2f}"
    )
    sys.exit(0 if project["phase"] == "done" else 1)


if __name__ == "__main__":
    main()
