"""HTTP API + live event stream + static dashboard."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete
from sqlmodel import col, select
from sse_starlette.sse import EventSourceResponse

from foreman import github, gitops
from foreman.api.inbox import router as inbox_router
from foreman.api.schemas import (
    ControlRequest,
    GitHubRepoCreate,
    GitHubTokenUpdate,
    NodeCreate,
    NodeUpdate,
    ProjectCreate,
    ProjectUpdate,
    RetryRequest,
    TaskCreate,
    TaskUpdate,
)
from foreman.api.spec import recheck_task_size
from foreman.api.spec import router as spec_router
from foreman.config import get_settings
from foreman.core import gates, roles, spend, watchdog
from foreman.core.models import (
    Control,
    ControlAction,
    ControlScope,
    Event,
    Gate,
    GateState,
    JobState,
    Node,
    PlannerJob,
    Project,
    ProjectPhase,
    Task,
    TaskRun,
    TaskState,
    TestCase,
    TestRun,
    now,
)
from foreman.core.state import (
    ACTIVE_STATES,
    ESTOP_FLAG,
    GITHUB_TOKEN_KEY,
    emit,
    estop_active,
    github_token,
    heartbeats,
    pending_stops,
    set_app_setting,
    set_flag,
    transition,
)
from foreman.db import control_session, session
from foreman.executors import EXECUTORS
from foreman.executors.shell import role_scripts
from foreman.llm import list_models


def require_token(request: Request) -> None:
    token = get_settings().token
    if not token:
        return
    header = request.headers.get("authorization", "")
    supplied = header.removeprefix("Bearer ").strip() or request.query_params.get("token", "")
    if supplied != token:
        raise HTTPException(401, "invalid or missing token")


router = APIRouter(dependencies=[Depends(require_token)])


# -- helpers ---------------------------------------------------------------


def _get(model: type, id_: int | None) -> Any:
    with session() as s:
        obj = s.get(model, id_)
    if obj is None:
        raise HTTPException(404, f"{model.__name__.lower()} {id_} not found")
    return obj


def _node_out(n: Node, load: dict[int, dict[str, int]] | None = None) -> dict[str, Any]:
    d = n.model_dump(exclude={"api_key"})
    d["has_api_key"] = bool(n.api_key)
    d.update((load or {}).get(n.id or 0, {"active": 0, "waiting": 0}))
    return d


def _node_load() -> dict[int, dict[str, int]]:
    """Per node: work running on it now, and queued tasks that will run on it."""
    from foreman.orchestrator.jobs import job_role
    from foreman.orchestrator.main import node_load
    from foreman.orchestrator.runner import resolve_executor

    out: dict[int, dict[str, int]] = {}
    for node_id, n in node_load().items():
        out.setdefault(node_id, {"active": 0, "waiting": 0})["active"] = n
    with session() as s:
        projects = {p.id: p for p in s.exec(select(Project))}
        queued = s.exec(select(Task).where(Task.state == TaskState.QUEUED)).all()
        jobs = s.exec(select(PlannerJob).where(PlannerJob.state == JobState.QUEUED)).all()
    for t in queued:
        p = projects.get(t.project_id)
        if p is None or p.phase != ProjectPhase.EXECUTE:
            continue
        _, task_node, _ = resolve_executor(p, t)
        if task_node is not None:
            out.setdefault(task_node, {"active": 0, "waiting": 0})["waiting"] += 1
    for j in jobs:
        p = projects.get(j.project_id)
        bound = roles.binding(p, job_role(j.kind)).get("node_id") if p else None
        if isinstance(bound, int):
            out.setdefault(bound, {"active": 0, "waiting": 0})["waiting"] += 1
    return out


def _task_counts(project_id: int | None) -> dict[str, int]:
    with session() as s:
        tasks = s.exec(select(Task).where(Task.project_id == project_id)).all()
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.state.value] = counts.get(t.state.value, 0) + 1
    return counts


def _reopen(project_id: int) -> None:
    """New work in a finished project: back to executing (acceptance runs again)."""
    with session() as s:
        p = s.get(Project, project_id)
        if p is not None and p.phase == ProjectPhase.DONE:
            p.phase, p.acceptance_passed = ProjectPhase.EXECUTE, False
            s.add(p)
            s.commit()
            emit(project_id, None, "state", "phase: execute (new work)", phase="execute")


def _check_bindings(bindings: dict[str, Any]) -> None:
    """Known roles, existing nodes. (Any provider can serve the Tester and Builder: they
    reach it through the model gateway, which holds the key - NFR-5.)"""
    for role, binding in bindings.items():
        if role not in (*roles.BINDING_KEYS, "planner", "executor"):
            raise HTTPException(400, f"unknown role {role!r}")
        node_id = (binding or {}).get("node_id")
        if node_id is None:
            continue
        with session() as s:
            if s.get(Node, node_id) is None:
                raise HTTPException(400, f"{role} node not found")


def _check_deps(project_id: int, deps: list[int], self_id: int | None = None) -> None:
    with session() as s:
        for d in deps:
            t = s.get(Task, d)
            if t is None or t.project_id != project_id or d == self_id:
                raise HTTPException(400, f"invalid dependency {d}")


# -- system ----------------------------------------------------------------


@router.get("/api/system")
def system() -> dict[str, Any]:
    t = time.time()
    return {
        "estop": estop_active(),
        "runtime": get_settings().runtime,
        "heartbeats": {k: round(t - v, 1) for k, v in heartbeats().items()},
        "executors": sorted(EXECUTORS),
        "pending_gates": len(gates.pending()),
    }


# -- settings --------------------------------------------------------------


def _github_token_out() -> dict[str, Any]:
    # The token itself never leaves the server; the last characters tell tokens apart.
    token, source = github_token()
    return {"configured": bool(token), "source": source, "hint": token[-4:] if token else ""}


@router.get("/api/settings/github")
def get_github_token() -> dict[str, Any]:
    return _github_token_out()


@router.put("/api/settings/github")
def put_github_token(body: GitHubTokenUpdate) -> dict[str, Any]:
    set_app_setting(GITHUB_TOKEN_KEY, body.token.strip())
    return _github_token_out()


@router.delete("/api/settings/github")
def delete_github_token() -> dict[str, Any]:
    """Forget the dashboard token; FOREMAN_GITHUB_TOKEN, if set, applies again."""
    set_app_setting(GITHUB_TOKEN_KEY, "")
    return _github_token_out()


def _require_github_token() -> str:
    token, _ = github_token()
    if not token:
        raise HTTPException(400, "no GitHub token configured (Settings)")
    return token


@router.post("/api/settings/github/check")
async def check_github_token() -> dict[str, Any]:
    token = _require_github_token()
    try:
        user = await github.user(token)
    except (github.GitHubError, httpx.HTTPError) as e:
        return {**_github_token_out(), "ok": False, "error": str(e) or type(e).__name__}
    return {**_github_token_out(), "ok": True, **user}


@router.get("/api/github/owners")
async def github_owners() -> list[str]:
    """Accounts a new repository can be created under: the token's user, then its orgs."""
    token = _require_github_token()
    try:
        return await github.owners(token)
    except (github.GitHubError, httpx.HTTPError) as e:
        raise HTTPException(502, str(e) or type(e).__name__) from e


@router.post("/api/github/repos", status_code=201)
async def create_github_repo(body: GitHubRepoCreate) -> dict[str, str]:
    """Create a repository for a new project; the project is then added with its clone_url."""
    token = _require_github_token()
    try:
        return await github.create_repo(
            token, body.owner, body.name, body.private, body.description
        )
    except (github.GitHubError, httpx.HTTPError) as e:
        raise HTTPException(400, str(e) or type(e).__name__) from e


# -- projects --------------------------------------------------------------


@router.get("/api/projects")
def list_projects() -> list[dict[str, Any]]:
    with session() as s:
        projects = s.exec(select(Project).order_by(col(Project.id))).all()
        waiting: dict[int, int] = {}
        for g in s.exec(select(Gate).where(Gate.state == GateState.PENDING)):
            waiting[g.project_id] = waiting.get(g.project_id, 0) + 1
        active = {
            t.project_id: t.title
            for t in s.exec(select(Task).where(col(Task.state).in_(ACTIVE_STATES)))
        }
        coverage: dict[int, float | None] = {}
        for r in s.exec(select(TestRun).where(TestRun.step == "builder").order_by(col(TestRun.id))):
            coverage[r.project_id] = r.coverage  # the latest verified run wins
    return [
        {
            **p.model_dump(),
            "task_counts": _task_counts(p.id),
            "pending_gates": waiting.get(p.id or 0, 0),
            "active_task": active.get(p.id or 0, ""),
            "coverage": coverage.get(p.id or 0),
        }
        for p in projects
    ]


# Tasks that no longer count toward the plan.
_OUT_OF_PLAN = {TaskState.DISCARDED.value, TaskState.SPLIT.value}


@router.get("/api/projects/{project_id}/progress")
def project_progress(project_id: int) -> list[dict[str, float]]:
    """Tasks done vs. tasks planned over time, one point per change."""
    _get(Project, project_id)
    with session() as s:
        tasks = s.exec(select(Task).where(Task.project_id == project_id)).all()
        changes = s.exec(
            select(Event)
            .where(Event.project_id == project_id, Event.kind == "state")
            .order_by(col(Event.id))
        ).all()
    timeline: list[tuple[float, int, str]] = [(t.created_at, t.id or 0, "") for t in tasks]
    timeline += [(e.ts, e.task_id or 0, str(e.data.get("state", ""))) for e in changes]
    timeline.sort(key=lambda x: x[0])
    known = {t.id for t in tasks}
    state: dict[int, str] = {}
    points: list[dict[str, float]] = []
    for ts, task_id, new_state in timeline:
        if task_id not in known:
            continue
        state[task_id] = new_state or state.get(task_id, "queued")
        planned = sum(1 for v in state.values() if v not in _OUT_OF_PLAN)
        done = sum(1 for v in state.values() if v == TaskState.DONE.value)
        point = {"ts": ts, "done": done, "planned": planned}
        if points and points[-1]["done"] == done and points[-1]["planned"] == planned:
            continue
        if points and ts - points[-1]["ts"] < 1e-6:
            points[-1] = point
        else:
            points.append(point)
    return points


@router.post("/api/projects", status_code=201)
async def create_project(body: ProjectCreate) -> Project:
    if body.executor_kind not in EXECUTORS:
        raise HTTPException(400, f"unknown executor {body.executor_kind}")
    bindings = {k: v.model_dump() for k, v in body.role_bindings.items()}
    _check_bindings(bindings)
    p = Project(
        name=body.name,
        repo_url=body.repo_url,
        milestone_branch=body.milestone_branch,
        feature=body.feature,
        phase=ProjectPhase.EXECUTE if body.skip_spec else ProjectPhase.SPECIFY,
        test_cmd=body.test_cmd,
        executor_kind=body.executor_kind,
        network=body.network,
        role_bindings=bindings,
        push_after_merge=body.push_after_merge,
        gates=body.gates,
        watchdog=body.watchdog,
        spend_cap_usd=body.spend_cap_usd,
        min_task_coverage=body.min_task_coverage,
    )
    with session() as s:
        s.add(p)
        s.commit()
        s.refresh(p)
    assert p.id is not None
    pid = p.id
    try:
        default_branch = await run_in_threadpool(
            gitops.clone_project, pid, p.repo_url, p.milestone_branch
        )
    except gitops.GitError as e:
        with session() as s:
            s.delete(s.get(Project, p.id))
            s.commit()
        shutil.rmtree(gitops.project_dir(pid), ignore_errors=True)
        raise HTTPException(400, f"clone failed: {e}") from e
    with session() as s:
        row = s.get(Project, p.id)
        assert row is not None
        row.default_branch = default_branch
        s.add(row)
        s.commit()
        s.refresh(row)
    emit(p.id, None, "system", f"project created from {p.repo_url}")
    return row


@router.get("/api/projects/{project_id}")
def get_project(project_id: int) -> dict[str, Any]:
    p = _get(Project, project_id)
    return {
        **p.model_dump(),
        "task_counts": _task_counts(project_id),
        "watchdog_effective": {k: watchdog.limit(p, k) for k in watchdog.DEFAULTS},
        "gates_effective": {k: gates.enabled(p, k) for k in gates.DEFAULTS},
    }


@router.patch("/api/projects/{project_id}")
def update_project(project_id: int, body: ProjectUpdate) -> Project:
    with session() as s:
        p = s.get(Project, project_id)
        if p is None:
            raise HTTPException(404, "project not found")
        data = body.model_dump(exclude_unset=True)
        if "executor_kind" in data and data["executor_kind"] not in EXECUTORS:
            raise HTTPException(400, "unknown executor")
        if "role_bindings" in data:
            _check_bindings(data["role_bindings"])
        for k, v in data.items():
            setattr(p, k, v)
        s.add(p)
        s.commit()
        s.refresh(p)
    if "spend_cap_usd" in data:
        spend.cap_changed(p)
    return p


@router.delete("/api/projects/{project_id}", status_code=204)
def delete_project(project_id: int) -> None:
    _get(Project, project_id)
    with session() as s:
        active = s.exec(
            select(Task).where(Task.project_id == project_id, col(Task.state).in_(ACTIVE_STATES))
        ).first()
        if active:
            raise HTTPException(409, "project has running tasks; stop it first")
        task_ids = [t.id for t in s.exec(select(Task).where(Task.project_id == project_id))]
        s.exec(delete(Event).where(col(Event.project_id) == project_id))  # type: ignore[call-overload]
        s.exec(delete(TaskRun).where(col(TaskRun.task_id).in_(task_ids)))  # type: ignore[call-overload]
        s.exec(delete(Task).where(col(Task.project_id) == project_id))  # type: ignore[call-overload]
        s.exec(delete(Project).where(col(Project.id) == project_id))  # type: ignore[call-overload]
        s.commit()
    shutil.rmtree(gitops.project_dir(project_id), ignore_errors=True)


# -- tasks -----------------------------------------------------------------


@router.get("/api/projects/{project_id}/tasks")
def list_tasks(project_id: int) -> list[Task]:
    _get(Project, project_id)
    with session() as s:
        return list(
            s.exec(select(Task).where(Task.project_id == project_id).order_by(col(Task.id))).all()
        )


@router.post("/api/projects/{project_id}/tasks", status_code=201)
def create_task(project_id: int, body: TaskCreate) -> Task:
    p = _get(Project, project_id)
    if body.executor_kind and body.executor_kind not in EXECUTORS:
        raise HTTPException(400, f"unknown executor {body.executor_kind}")
    if (body.executor_kind or p.executor_kind) == "shell":
        # Scripted Tester/Builder steps are tested like agent tasks; plain scripts need a command.
        if not body.validation_cmd.strip() and not role_scripts(body.executor_script):
            raise HTTPException(400, "scripted (shell) tasks need a validation command")
    else:
        # Agent tasks: the Tester decides how to test them, from the criteria.
        if body.validation_cmd or body.allowed_globs:
            raise HTTPException(
                400, "agent tasks take a goal and criteria; the Tester writes their tests"
            )
        if not [c for c in body.criteria if c.strip()]:
            raise HTTPException(400, "give the task at least one acceptance criterion")
    _check_deps(project_id, body.deps)
    t = Task(project_id=project_id, **body.model_dump(), origin="manual")
    if p.phase == ProjectPhase.PLAN_REVIEW:
        t.state = TaskState.PROPOSED
    if p.phase == ProjectPhase.DONE:
        _reopen(project_id)
    with session() as s:
        s.add(t)
        s.commit()
        s.refresh(t)
    emit(project_id, t.id, "state", t.state.value, state=t.state.value)
    if t.state == TaskState.PROPOSED:
        assert t.id is not None
        recheck_task_size(t.id)
        return _get(Task, t.id)
    return t


@router.get("/api/tasks/{task_id}")
def get_task(task_id: int) -> dict[str, Any]:
    t = _get(Task, task_id)
    with session() as s:
        runs = s.exec(
            select(TaskRun).where(TaskRun.task_id == task_id).order_by(col(TaskRun.id))
        ).all()
    projects, tasks = pending_stops()
    return {
        **t.model_dump(),
        "runs": [r.model_dump() for r in runs],
        "stop_pending": task_id in tasks or t.project_id in projects or estop_active(),
    }


@router.patch("/api/tasks/{task_id}")
def update_task(task_id: int, body: TaskUpdate) -> Task:
    with session() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise HTTPException(404, "task not found")
        if t.state in ACTIVE_STATES:
            raise HTTPException(409, "task is running; stop or pause it first")
        data = body.model_dump(exclude_unset=True)
        if "deps" in data:
            _check_deps(t.project_id, data["deps"], self_id=task_id)
        for k, v in data.items():
            setattr(t, k, v)
        t.updated_at = now()
        s.add(t)
        s.commit()
        s.refresh(t)
    if t.state == TaskState.PROPOSED:
        recheck_task_size(task_id)
    return _get(Task, task_id)


@router.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: int, body: RetryRequest | None = None) -> Task:
    t = _get(Task, task_id)
    if t.state not in (TaskState.FAILED, TaskState.STOPPED_BY_USER, TaskState.PAUSED):
        raise HTTPException(409, f"cannot retry a task in state {t.state.value}")
    if body and body.reset:
        gitops.reset_task_clone(t.project_id, task_id)
    with session() as s:
        row = s.get(Task, task_id)
        assert row is not None
        row.hold = False
        s.add(row)
        s.commit()
    _reopen(t.project_id)
    transition(task_id, (t.state,), TaskState.QUEUED, "retry requested")
    return _get(Task, task_id)


@router.post("/api/tasks/{task_id}/discard")
def discard_task(task_id: int) -> Task:
    t = _get(Task, task_id)
    if t.state in ACTIVE_STATES:
        raise HTTPException(409, "task is running; stop it first")
    if not transition(task_id, (t.state,), TaskState.DISCARDED, "discarded by user"):
        raise HTTPException(409, f"cannot discard a task in state {t.state.value}")
    return _get(Task, task_id)


# -- test results and coverage ------------------------------------------------------


def _run_out(run: TestRun, cases: list[TestCase] | None = None) -> dict[str, Any]:
    out = run.model_dump()
    if cases is not None:
        out["cases"] = [c.model_dump() for c in cases]
    return out


def _cases(run_id: int | None) -> list[TestCase]:
    with session() as s:
        q = select(TestCase).where(TestCase.test_run_id == run_id).order_by(col(TestCase.id))
        return list(s.exec(q))


@router.get("/api/tasks/{task_id}/tests")
def task_tests(task_id: int) -> dict[str, Any]:
    """The task's test runs (Tester: red, Builder: green), newest first, the latest with
    every case."""
    _get(Task, task_id)
    with session() as s:
        runs = s.exec(
            select(TestRun).where(TestRun.task_id == task_id).order_by(col(TestRun.id).desc())
        ).all()
    return {
        "runs": [_run_out(r, _cases(r.id) if i == 0 else None) for i, r in enumerate(runs)],
    }


@router.get("/api/projects/{project_id}/tests")
def project_tests(project_id: int) -> dict[str, Any]:
    """The latest full-suite result for the project (the last merged task's verify run, or
    the latest acceptance check), with its cases."""
    _get(Project, project_id)
    with session() as s:
        latest = s.exec(
            select(TestRun)
            .where(TestRun.project_id == project_id, TestRun.step == "builder")
            .order_by(col(TestRun.id).desc())
        ).first()
        acceptance = s.exec(
            select(TestRun)
            .where(TestRun.project_id == project_id, TestRun.step == "acceptance")
            .order_by(col(TestRun.id).desc())
        ).first()
    return {
        "suite": _run_out(latest, _cases(latest.id)) if latest else None,
        "acceptance": _run_out(acceptance, _cases(acceptance.id)) if acceptance else None,
    }


@router.get("/api/projects/{project_id}/coverage-trend")
def coverage_trend(project_id: int) -> list[dict[str, Any]]:
    """Coverage and test counts after each verified task, oldest first."""
    _get(Project, project_id)
    with session() as s:
        runs = s.exec(
            select(TestRun)
            .where(TestRun.project_id == project_id, TestRun.step == "builder")
            .order_by(col(TestRun.id))
        ).all()
    return [
        {
            "ts": r.created_at,
            "coverage": r.coverage,
            "tests": r.passed + r.failed + r.errors,
            "passed": r.passed,
            "task_id": r.task_id,
        }
        for r in runs
        if not r.problem
    ]


@router.get("/api/roles")
def list_roles() -> list[dict[str, Any]]:
    """The agents and what each may do (core/roles.py)."""
    return roles.describe()


@router.get("/api/tasks/{task_id}/diff")
async def task_diff(task_id: int) -> dict[str, Any]:
    t = _get(Task, task_id)
    path = gitops.task_dir(t.project_id, task_id)
    text = await run_in_threadpool(gitops.diff, path)
    files = await run_in_threadpool(gitops.changed_files, path) if (path / ".git").exists() else []
    return {"diff": text, "files": files}


# -- nodes -----------------------------------------------------------------


@router.get("/api/nodes")
def list_nodes() -> list[dict[str, Any]]:
    load = _node_load()
    with session() as s:
        return [_node_out(n, load) for n in s.exec(select(Node).order_by(col(Node.id)))]


@router.post("/api/nodes", status_code=201)
def create_node(body: NodeCreate) -> dict[str, Any]:
    n = Node(**body.model_dump())
    n.base_url = n.base_url.rstrip("/")
    with session() as s:
        s.add(n)
        s.commit()
        s.refresh(n)
    return _node_out(n)


@router.patch("/api/nodes/{node_id}")
def update_node(node_id: int, body: NodeUpdate) -> dict[str, Any]:
    with session() as s:
        n = s.get(Node, node_id)
        if n is None:
            raise HTTPException(404, "node not found")
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(n, k, v.rstrip("/") if k == "base_url" else v)
        s.add(n)
        s.commit()
        s.refresh(n)
    return _node_out(n, _node_load())


@router.delete("/api/nodes/{node_id}", status_code=204)
def delete_node(node_id: int) -> None:
    with session() as s:
        n = s.get(Node, node_id)
        if n is None:
            raise HTTPException(404, "node not found")
        s.delete(n)
        s.commit()


@router.post("/api/nodes/{node_id}/check")
async def check_node(node_id: int) -> dict[str, Any]:
    n = _get(Node, node_id)
    healthy, models, error = False, list(n.models), ""
    try:
        found = await list_models(n)
        healthy, models = True, found or models
    except Exception as e:  # noqa: BLE001 - report any failure to the UI
        error = str(e) or type(e).__name__
    with session() as s:
        row = s.get(Node, node_id)
        assert row is not None
        row.healthy, row.models, row.last_checked = healthy, models, now()
        s.add(row)
        s.commit()
        s.refresh(row)
    return {**_node_out(row), "error": error}


# -- controls (pause / resume / stop / E-stop) ------------------------------


@router.post("/api/controls")
def control(body: ControlRequest) -> dict[str, Any]:
    scope = ControlScope(body.scope)
    if scope != ControlScope.GLOBAL and body.target_id is None:
        raise HTTPException(400, "target_id required")
    if scope == ControlScope.GLOBAL and body.action not in ("estop", "clear_estop"):
        raise HTTPException(400, "global scope supports estop / clear_estop")
    if scope != ControlScope.GLOBAL and body.action in ("estop", "clear_estop"):
        raise HTTPException(400, "estop is global")

    if body.action == "clear_estop":
        set_flag(ESTOP_FLAG, "0")
        emit(None, None, "control", "E-stop cleared")
        return {"ok": True}

    if body.action in ("pause", "resume"):
        paused = body.action == "pause"
        if scope == ControlScope.PROJECT:
            with session() as s:
                p = s.get(Project, body.target_id)
                if p is None:
                    raise HTTPException(404, "project not found")
                p.paused = paused
                p.paused_at = now()
                s.add(p)
                s.commit()
            emit(body.target_id, None, "control", f"project {body.action}d")
        else:
            with session() as s:
                t = s.get(Task, body.target_id)
                if t is None:
                    raise HTTPException(404, "task not found")
                t.hold = paused
                s.add(t)
                s.commit()
                state, project_id = t.state, t.project_id
            if not paused and state == TaskState.PAUSED and body.target_id is not None:
                transition(body.target_id, (TaskState.PAUSED,), TaskState.QUEUED, "resumed")
            emit(project_id, body.target_id, "control", f"task {body.action}d")
        return {"ok": True}

    # stop / estop: recorded in control.db (never blocked by foreman.db's write lock) and
    # executed by the supervisor. The orchestrator stops scheduling the target as soon as
    # the row exists. Only reads of foreman.db happen on this path.
    if scope == ControlScope.PROJECT:
        _get(Project, body.target_id)
    elif scope == ControlScope.TASK:
        _get(Task, body.target_id)
    else:
        set_flag(ESTOP_FLAG, "1")
    c = Control(scope=scope, target_id=body.target_id, action=ControlAction(body.action))
    with control_session() as s:
        s.add(c)
        s.commit()
        s.refresh(c)
    # The audit event goes to foreman.db, which might be locked by a hung writer: don't
    # make the Stop response wait for it.
    threading.Thread(
        target=emit,
        daemon=True,
        args=(
            body.target_id if scope == ControlScope.PROJECT else None,
            body.target_id if scope == ControlScope.TASK else None,
            "control",
            f"{body.action} requested ({scope.value})",
        ),
    ).start()
    return {"ok": True, "control_id": c.id}


@router.get("/api/controls/{control_id}")
def get_control(control_id: int) -> Control:
    with control_session() as s:
        c = s.get(Control, control_id)
    if c is None:
        raise HTTPException(404, "control not found")
    return c


# -- events ----------------------------------------------------------------


def _events(project_id: int | None, task_id: int | None, after: int, limit: int) -> list[Event]:
    q = select(Event).where(col(Event.id) > after)
    if project_id is not None:
        q = q.where(Event.project_id == project_id)
    if task_id is not None:
        q = q.where(Event.task_id == task_id)
    with session() as s:
        return list(s.exec(q.order_by(col(Event.id)).limit(limit)).all())


@router.get("/api/events")
def list_events(
    project_id: int | None = None,
    task_id: int | None = None,
    after: int = 0,
    limit: int = Query(500, le=5000),
) -> list[Event]:
    return _events(project_id, task_id, after, limit)


@router.get("/api/stream")
async def stream(
    request: Request,
    project_id: int | None = None,
    task_id: int | None = None,
    after: int = 0,
) -> EventSourceResponse:
    """Server-Sent Events: new Event rows as they are written by any process."""

    async def gen():  # type: ignore[no-untyped-def]
        last = after
        while not await request.is_disconnected():
            rows = await run_in_threadpool(_events, project_id, task_id, last, 500)
            for e in rows:
                last = e.id
                yield {"id": str(e.id), "event": "event", "data": json.dumps(e.model_dump())}
            if not rows:
                await asyncio.sleep(0.5)

    return EventSourceResponse(gen(), ping=15)


# -- static dashboard -------------------------------------------------------


def _web_dist() -> Path:
    env = os.environ.get("FOREMAN_WEB_DIST")
    return Path(env) if env else Path(__file__).resolve().parents[3] / "web" / "dist"


def create_app() -> FastAPI:
    app = FastAPI(title="foreman")
    app.include_router(router)
    app.include_router(spec_router, dependencies=[Depends(require_token)])
    app.include_router(inbox_router, dependencies=[Depends(require_token)])
    dist = _web_dist()
    if dist.is_dir():
        # The dashboard shell is public; every /api call it makes carries the token.
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path.startswith("api/"):
                raise HTTPException(404)
            f = (dist / path).resolve()
            if path and f.is_file() and f.is_relative_to(dist.resolve()):
                return FileResponse(f)
            return FileResponse(dist / "index.html")

    return app
