"""Milestone completion: when every task is done, run the acceptance command.

- passes  -> the project is done; with the ``milestone`` checkpoint (default on)
             the user approves, then the milestone branch is pushed and a GitHub
             pull request is opened (if the repo is on GitHub and a token is set);
- fails   -> the replanner gets the failure output and may add tasks, up to
             ``max_acceptance_failures`` times; then an inbox item asks the user.

The check runs in the same sandbox as tasks, on a fresh clone of the milestone
branch, and only once per commit.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
from sqlmodel import col, select

from foreman import gitops, images, sandbox, testresults
from foreman.config import get_settings
from foreman.core import gates, watchdog
from foreman.core.models import (
    GateKind,
    JobState,
    PlannerJob,
    Project,
    ProjectPhase,
    Task,
    TaskState,
)
from foreman.core.state import emit, estop_active, github_token, stop_requested
from foreman.db import session
from foreman.sandbox import Runtime, StepSpec, kill_handle

ACCEPTANCE_RUNNER = "tests/acceptance/run.sh"

log = logging.getLogger("foreman.milestone")

FINISHED = (TaskState.DONE, TaskState.DISCARDED, TaskState.SPLIT)
ACCEPTANCE_TIMEOUT_S = 1800


def ready_for_acceptance(project: Project) -> bool:
    """All tasks finished, nothing pending, and not yet checked at this commit."""
    assert project.id is not None
    if project.phase != ProjectPhase.EXECUTE or project.paused:
        return False
    with session() as s:
        tasks = s.exec(select(Task).where(Task.project_id == project.id)).all()
        busy_job = s.exec(
            select(PlannerJob).where(
                PlannerJob.project_id == project.id,
                col(PlannerJob.state).in_((JobState.QUEUED, JobState.RUNNING)),
            )
        ).first()
    if not tasks or any(t.state not in FINISHED for t in tasks):
        return False
    if not any(t.state == TaskState.DONE for t in tasks) or busy_job is not None:
        return False
    if gates.pending(project.id):
        return False
    return project.acceptance_commit != gitops.head_commit(project.id)


def acceptance_command(project: Project) -> str:
    """The definition of done: the project's acceptance command, else the Tester's
    acceptance runner, else the project test command."""
    if project.acceptance_cmd:
        return project.acceptance_cmd
    assert project.id is not None
    if (gitops.repo_dir(project.id) / ACCEPTANCE_RUNNER).is_file():
        return f"sh {ACCEPTANCE_RUNNER}"
    return project.test_cmd


def _store(project: Project, head: str, results: Path, clone: Path) -> None:
    """Record the acceptance results, when the runner wrote any."""
    from foreman.orchestrator.runner import store_results

    res = testresults.collect(results, clone)
    if res.cases:
        assert project.id is not None
        store_results(project.id, None, None, "acceptance", head, res)


async def run_acceptance(project_id: int, runtime: Runtime) -> None:
    with session() as s:
        project = s.get(Project, project_id)
        assert project is not None
    cmd = acceptance_command(project)
    head = gitops.head_commit(project_id)
    started = time.time()
    if not cmd:
        emit(project_id, None, "system", "all tasks done; no acceptance command is set")
        _record(project_id, head, True)
        await _passed(project_id)
        return
    clone = await asyncio.to_thread(
        gitops.prepare_acceptance_clone, project_id, project.milestone_branch
    )
    ctl = get_settings().runs_dir / f"acceptance-{project_id}"
    ctl.mkdir(parents=True, exist_ok=True)
    results = ctl / "results"
    shutil.rmtree(results, ignore_errors=True)
    results.mkdir()
    image = ""
    if runtime.name == "docker":
        try:
            image = await images.ensure_image(
                project_id, clone, on_handle=lambda h: _set_handle(project_id, h)
            )
        except images.ImageBuildError as e:
            _set_handle(project_id, "")
            if estop_active() or stop_requested(project_id, 0, started):
                emit(project_id, None, "system", "acceptance check stopped")
                return
            _record(project_id, head, False)
            emit(project_id, None, "test", f"acceptance: {e}", step="acceptance", passed=False)
            _failed(project_id, f"{e}\n{e.output}")
            return
    spec = StepSpec(
        name=f"foreman-acceptance{project_id}-{int(time.time() * 1000) % 10**8}",
        workspace=clone,
        ctl_dir=ctl,
        command=["bash", "-lc", cmd],
        labels={"foreman.project": str(project_id), "foreman.acceptance": "1"},
        network=project.network,
        image=image,
        results_dir=results,
    )
    if runtime.name == "docker" and project.network != "open":
        try:
            await asyncio.to_thread(sandbox.ensure_exec_network)
        except sandbox.IsolationError as e:
            _record(project_id, head, False)
            emit(project_id, None, "test", f"acceptance: {e}", step="acceptance", passed=False)
            _failed(project_id, str(e))
            return
    emit(project_id, None, "test", f"acceptance: $ {cmd}", step="acceptance")
    proc = await runtime.start(spec)
    _set_handle(project_id, proc.handle)
    tail: list[str] = []
    try:

        async def pump() -> int:
            async for line in proc.lines():
                tail.append(line)
                del tail[:-200]
            return await proc.wait()

        code = await asyncio.wait_for(pump(), ACCEPTANCE_TIMEOUT_S)
    except TimeoutError:
        await asyncio.to_thread(kill_handle, proc.handle)
        code = -1
        tail.append(f"acceptance timed out after {ACCEPTANCE_TIMEOUT_S}s")
    finally:
        _set_handle(project_id, "")
    if estop_active() or stop_requested(project_id, 0, started):
        emit(project_id, None, "system", "acceptance check stopped")
        return
    output = "\n".join(tail)
    emit(project_id, None, "test", output[-4000:] or "(no output)", step="acceptance")
    _store(project, head, results, clone)
    if code == 0:
        _record(project_id, head, True)
        emit(project_id, None, "test", "acceptance passed", step="acceptance", passed=True)
        await _passed(project_id)
    else:
        _record(project_id, head, False)
        emit(
            project_id,
            None,
            "test",
            f"acceptance failed (exit {code})",
            step="acceptance",
            passed=False,
        )
        _failed(project_id, output)


def _set_handle(project_id: int, handle: str) -> None:
    with session() as s:
        p = s.get(Project, project_id)
        if p is not None:
            p.acceptance_handle = handle
            s.add(p)
            s.commit()


def _record(project_id: int, head: str, passed: bool) -> None:
    with session() as s:
        p = s.get(Project, project_id)
        assert p is not None
        p.acceptance_commit, p.acceptance_passed = head, passed
        if not passed:
            p.acceptance_failures += 1
        s.add(p)
        s.commit()


def _failed(project_id: int, output: str) -> None:
    from foreman.orchestrator.replan import queue_review

    with session() as s:
        project = s.get(Project, project_id)
        assert project is not None
    if project.acceptance_failures <= watchdog.limit(
        project, "max_acceptance_failures"
    ) and queue_review(
        project_id, "the acceptance tests failed after all tasks were done", output=output
    ):
        return
    gates.open_gate(
        project_id,
        GateKind.ESCALATION,
        "Acceptance tests keep failing",
        f"All tasks are done but `{project.acceptance_cmd}` fails "
        f"({project.acceptance_failures} time(s)).\n\n{output[-2000:]}\n\n"
        "Approve to let the replanner try again, or reject to leave it for you to fix.",
        payload={"acceptance": True},
    )


async def _passed(project_id: int) -> None:
    with session() as s:
        project = s.get(Project, project_id)
        assert project is not None
        project.phase = ProjectPhase.DONE
        s.add(project)
        s.commit()
    emit(project_id, None, "state", "phase: done", phase="done")
    if gates.enabled(project, "milestone"):
        gates.open_gate(
            project_id,
            GateKind.MILESTONE,
            f"{project.name}: milestone complete",
            f"Every task is done and the acceptance command passes on "
            f"{project.milestone_branch}.\n\nApprove to push the branch"
            + (" and open a pull request." if gitops.github_repo(project.repo_url) else "."),
        )
    else:
        await asyncio.to_thread(publish, project_id)


def publish(project_id: int) -> str:
    """Push the milestone branch and open a GitHub PR. Returns the PR URL (or "")."""
    with session() as s:
        project = s.get(Project, project_id)
        assert project is not None
    token, _ = github_token()
    try:
        gitops.push(project_id, project.repo_url, project.milestone_branch)
        emit(project_id, None, "system", f"pushed {project.milestone_branch}")
    except gitops.GitError as e:
        emit(project_id, None, "system", f"push failed: {e}")
        return ""
    repo = gitops.github_repo(project.repo_url)
    if repo is None or not token:
        emit(
            project_id,
            None,
            "system",
            "no pull request opened (not a GitHub repository or no GitHub token)",
        )
        return ""
    owner, name = repo
    body: dict[str, Any] = {
        "title": f"{project.name}: {project.feature}",
        "head": project.milestone_branch,
        "base": project.default_branch,
        "body": f"Built by foreman from `specs/{project.feature}/`.\n\n"
        f"All tasks are done and `{project.acceptance_cmd}` passes.",
    }
    try:
        r = httpx.post(
            f"https://api.github.com/repos/{owner}/{name}/pulls",
            json=body,
            timeout=30,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        r.raise_for_status()
        url = str(r.json().get("html_url", ""))
    except httpx.HTTPError as e:
        emit(project_id, None, "system", f"could not open the pull request: {e}")
        return ""
    with session() as s:
        p = s.get(Project, project_id)
        assert p is not None
        p.milestone_pr_url = url
        s.add(p)
        s.commit()
    emit(project_id, None, "system", f"pull request opened: {url}", url=url)
    return url


def kill_acceptance(project_id: int | None) -> list[str]:
    """Supervisor helper: kill running acceptance checks (one project, or all)."""
    with session() as s:
        q = select(Project).where(Project.acceptance_handle != "")
        if project_id is not None:
            q = q.where(Project.id == project_id)
        handles = [p.acceptance_handle for p in s.exec(q)]
    return [kill_handle(h) for h in handles]
