"""Orchestrator process: picks ready tasks and runs them.

Scheduling rules:
- nothing starts while E-stop is active;
- a paused project, or a task on hold, never starts;
- a task starts only when all its dependencies are done;
- at most one active task per project (tasks merge into one milestone branch);
- a node never runs more work (task attempts plus planner calls) than its capacity,
  and a node that failed its last health check gets none;
- paid (priced) nodes get no work for a project at its spend cap.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from typing import Any

from sqlmodel import col, select

from foreman import engine, gitops, sandbox
from foreman.config import get_settings
from foreman.core import gates, roles, spend
from foreman.core.models import (
    JobKind,
    JobState,
    Node,
    PlannerJob,
    Project,
    ProjectPhase,
    RunState,
    Task,
    TaskRun,
    TaskState,
)
from foreman.core.state import (
    ACTIVE_STATES,
    emit,
    estop_active,
    finish_run,
    heartbeat,
    pending_stops,
    transition,
)
from foreman.db import session
from foreman.executors import get_executor
from foreman.gateway import Gateway, new_token, serve
from foreman.llm import list_models
from foreman.orchestrator import gate_actions, milestone
from foreman.orchestrator.jobs import JobRunner, finish_job, job_role
from foreman.orchestrator.runner import TaskRunner, first_role, resolve_executor, uses_pipeline
from foreman.sandbox import Runtime, get_runtime, kill_handle

log = logging.getLogger("foreman.orchestrator")


def recover() -> None:
    """After a restart no task may be left silently 'running' (NFR-2), and no repository,
    acceptance check or planner job half-done."""
    recover_projects()
    with session() as s:
        runs = s.exec(select(TaskRun).where(TaskRun.state == RunState.RUNNING)).all()
    for run in runs:
        assert run.id is not None
        if run.handle:
            kill_handle(run.handle, 2)
        finish_run(run.id, RunState.INTERRUPTED, "orchestrator restarted during run")
        transition(
            run.task_id,
            ACTIVE_STATES,
            TaskState.FAILED,
            "orchestrator restarted during run; retry to continue",
        )
    # Tasks marked active without an open run (crash between transition and run insert).
    with session() as s:
        orphans = s.exec(select(Task).where(col(Task.state).in_(ACTIVE_STATES))).all()
        open_runs = {
            r.task_id for r in s.exec(select(TaskRun).where(TaskRun.state == RunState.RUNNING))
        }
    for t in orphans:
        if t.id is not None and t.id not in open_runs:
            transition(t.id, ACTIVE_STATES, TaskState.FAILED, "orchestrator restarted")
    # Planner jobs cut off mid-call.
    with session() as s:
        jobs = s.exec(select(PlannerJob).where(PlannerJob.state == JobState.RUNNING)).all()
    for job in jobs:
        assert job.id is not None
        if finish_job(job.id, (JobState.RUNNING,), JobState.FAILED, "orchestrator restarted"):
            JobRunner(job.id)._after_failure("orchestrator restarted; send your message again")


def recover_projects() -> None:
    with session() as s:
        projects = s.exec(select(Project)).all()
    for p in projects:
        assert p.id is not None
        if p.acceptance_handle:
            # The check was cut off: kill what's left; it runs again (no result recorded).
            kill_handle(p.acceptance_handle, 0)
            with session() as s:
                row = s.get(Project, p.id)
                if row is not None:
                    row.acceptance_handle = ""
                    s.add(row)
                    s.commit()
            emit(p.id, None, "system", "acceptance check interrupted by a restart; re-running")
        try:
            actions = gitops.recover_repo(p.id, p.milestone_branch)
        except (gitops.GitError, OSError) as e:
            log.warning("project %s: repository recovery failed: %s", p.id, e)
            continue
        if actions:
            emit(p.id, None, "system", "after restart: " + "; ".join(actions))


def node_load() -> dict[int, int]:
    """Work currently on each node: running task attempts plus running planner jobs."""
    load: dict[int, int] = {}
    with session() as s:
        runs = s.exec(select(TaskRun).where(TaskRun.state == RunState.RUNNING)).all()
        jobs = s.exec(select(PlannerJob).where(PlannerJob.state == JobState.RUNNING)).all()
    for nid in [r.node_id for r in runs] + [j.node_id for j in jobs]:
        if nid is not None:
            load[nid] = load.get(nid, 0) + 1
    return load


def node_available(node: Node | None, load: dict[int, int]) -> bool:
    """A node can take more work: it exists, isn't known to be down, and has a free slot."""
    if node is None or node.id is None or node.healthy is False:
        return False
    return load.get(node.id, 0) < node.capacity


async def check_nodes() -> None:
    """Refresh each node's health and model list; announce changes."""
    with session() as s:
        nodes = s.exec(select(Node)).all()

    async def one(node: Node) -> None:
        try:
            models = await asyncio.wait_for(list_models(node), 15)
            healthy, error = True, ""
        except Exception as e:  # noqa: BLE001 - any failure means unreachable
            models, healthy, error = list(node.models), False, str(e) or type(e).__name__
        with session() as s:
            row = s.get(Node, node.id)
            if row is None:
                return
            changed = row.healthy is not None and row.healthy != healthy
            row.healthy, row.last_checked = healthy, time.time()
            row.models = models or row.models
            s.add(row)
            s.commit()
        if changed:
            emit(
                None,
                None,
                "system",
                f"node {node.name} is {'back' if healthy else 'unreachable'}"
                + (f": {error}" if error else ""),
                node_id=node.id,
            )

    await asyncio.gather(*(one(n) for n in nodes))


def pick_ready() -> list[tuple[Task, int | None, str]]:
    """Ready tasks in id order with their resolved (node_id, model)."""
    with session() as s:
        projects = {p.id: p for p in s.exec(select(Project))}
        tasks = s.exec(select(Task).order_by(col(Task.id))).all()
        nodes = {n.id: n for n in s.exec(select(Node))}
    stopping_projects, stopping_tasks = pending_stops()
    # Replanning happens between tasks: the next one waits for the plan review.
    with session() as s:
        reviewing = {
            j.project_id
            for j in s.exec(
                select(PlannerJob).where(
                    PlannerJob.kind == JobKind.REVIEW,
                    col(PlannerJob.state).in_((JobState.QUEUED, JobState.RUNNING)),
                )
            )
        }
    by_id = {t.id: t for t in tasks}
    busy_projects = {t.project_id for t in tasks if t.state in ACTIVE_STATES}
    load = node_load()

    ready = []
    for t in tasks:
        p = projects.get(t.project_id)
        if t.state != TaskState.QUEUED or t.hold or p is None or p.paused or p.clone_error:
            continue
        if p.phase != ProjectPhase.EXECUTE:
            continue
        if t.project_id in busy_projects or t.project_id in stopping_projects:
            continue
        if t.project_id in reviewing:
            continue
        if t.id in stopping_tasks:
            continue
        if any(by_id.get(d) is None or by_id[d].state != TaskState.DONE for d in t.deps):
            continue
        kind, _, _ = resolve_executor(p, t)
        try:
            executor = get_executor(kind)
            needs_node = executor.needs_node
        except ValueError:
            executor, needs_node = None, False
        # The first agent this attempt runs decides the node it needs.
        role = first_role(t) if executor and uses_pipeline(t, executor) else roles.BUILDER
        _, nid, model = resolve_executor(p, t, role)
        if needs_node and nid is not None:
            if not node_available(nodes.get(nid), load):
                continue
            if spend.priced(nodes.get(nid)) and spend.over_cap(p):
                continue
            load[nid] = load.get(nid, 0) + 1
        busy_projects.add(t.project_id)
        ready.append((t, nid, model))
    return ready


class Orchestrator:
    def __init__(self, runtime: Runtime | None = None):
        self.runtime = runtime or get_runtime()
        self.settings = get_settings()
        self.running: dict[int, asyncio.Task[None]] = {}
        self.jobs: dict[int, asyncio.Task[None]] = {}  # planner jobs by project id
        self.acceptance: dict[int, asyncio.Task[None]] = {}  # acceptance checks by project
        self.gateway: Gateway | None = None
        self.gateway_server: Any = None
        self.gateway_port = 0
        self._health: asyncio.Task[None] | None = None
        self._last_health = 0.0
        self._stop = asyncio.Event()

    async def ensure_gateway(self) -> None:
        if self.gateway is None:
            self.gateway = Gateway()
            bind = await asyncio.to_thread(self.gateway_bind_host)
            self.gateway_server, self.gateway_port = await serve(
                self.gateway, bind, self.settings.gateway_port
            )
            log.info("model gateway on %s:%s", bind, self.gateway_port)

    def gateway_bind_host(self) -> str:
        """Where the gateway listens: only where executors arrive (foreman.engine)."""
        if self.settings.gateway_host != "auto":
            return self.settings.gateway_host
        if self.runtime.name != "docker":
            return "127.0.0.1"
        return engine.detect().bind_host

    @property
    def gateway_url(self) -> str:
        """The gateway as reachable from where executors run."""
        if not self.gateway_port:
            return ""
        host = engine.HOST_ALIAS if self.runtime.name == "docker" else "127.0.0.1"
        return f"http://{host}:{self.gateway_port}"

    def start_task(self, task: Task, node_id: int | None, model: str) -> bool:
        assert task.id is not None
        task_id = task.id
        if not transition(task_id, (TaskState.QUEUED,), TaskState.RUNNING):
            return False
        with session() as s:
            t = s.get(Task, task_id)
            assert t is not None
            t.attempts += 1
            run = TaskRun(
                task_id=task_id,
                project_id=t.project_id,
                node_id=node_id,
                model=model,
                gateway_token=new_token(),
            )
            p = s.get(Project, t.project_id)
            try:
                kind = t.executor_kind or (p.executor_kind if p else "")
                if uses_pipeline(t, get_executor(kind)):
                    run.step = first_role(t)
            except ValueError:
                pass
            s.add(t)
            s.add(run)
            s.commit()
            run_id = run.id
        assert run_id is not None
        emit(task.project_id, task_id, "system", f"attempt {t.attempts} started", run_id=run_id)
        runner = TaskRunner(
            task_id, run_id, self.runtime, self.gateway_url, gateway_port=self.gateway_port
        )
        self.running[task_id] = asyncio.create_task(runner.run())
        return True

    async def tick(self) -> None:
        heartbeat("orchestrator")
        await self.ensure_gateway()
        for registry in (self.running, self.jobs, self.acceptance):
            for key, fut in list(registry.items()):
                if fut.done():
                    del registry[key]
        for gate in gates.unhandled():
            await gate_actions.handle(gate)
        if estop_active():
            return
        self.start_health_checks()
        self.start_jobs()
        self.start_acceptance()
        for task, node_id, model in pick_ready():
            if task.id not in self.running:
                self.start_task(task, node_id, model)

    def start_jobs(self) -> None:
        """Start queued planner jobs: one at a time per project."""
        stopping, _ = pending_stops()
        with session() as s:
            queued = s.exec(
                select(PlannerJob)
                .where(PlannerJob.state == JobState.QUEUED)
                .order_by(col(PlannerJob.id))
            ).all()
            busy = {
                j.project_id
                for j in s.exec(select(PlannerJob).where(PlannerJob.state == JobState.RUNNING))
            }
            paused = {p.id for p in s.exec(select(Project).where(col(Project.paused)))}
            projects = {p.id: p for p in s.exec(select(Project))}
            nodes = {n.id: n for n in s.exec(select(Node))}
        load = node_load()
        for job in queued:
            assert job.id is not None
            if job.project_id in busy or job.project_id in self.jobs:
                continue
            if job.project_id in stopping or job.project_id in paused:
                continue
            # Planner calls share node capacity with task attempts.
            binding = roles.binding(projects[job.project_id], job_role(job.kind))
            nid = binding.get("node_id")
            if nid is not None:
                if not node_available(nodes.get(nid), load):
                    continue
                if spend.priced(nodes.get(nid)) and spend.over_cap(projects[job.project_id]):
                    continue
                load[nid] = load.get(nid, 0) + 1
            busy.add(job.project_id)
            self.jobs[job.project_id] = asyncio.create_task(JobRunner(job.id).run())

    def start_health_checks(self) -> None:
        """Check every node in the background, every NODE_CHECK_S seconds (0 = never)."""
        if self.settings.node_check_s <= 0:
            return  # disabled (tests)
        if self._health is not None and not self._health.done():
            return
        if time.monotonic() - self._last_health < self.settings.node_check_s:
            return
        self._last_health = time.monotonic()
        self._health = asyncio.create_task(check_nodes())

    def start_acceptance(self) -> None:
        stopping, _ = pending_stops()
        with session() as s:
            projects = s.exec(select(Project).where(Project.phase == ProjectPhase.EXECUTE)).all()
        for p in projects:
            assert p.id is not None
            if p.id in self.acceptance or p.id in self.jobs or p.id in stopping:
                continue
            if milestone.ready_for_acceptance(p):
                self.acceptance[p.id] = asyncio.create_task(
                    milestone.run_acceptance(p.id, self.runtime)
                )

    async def shutdown(self) -> None:
        for fut in [*self.running.values(), *self.jobs.values(), *self.acceptance.values()]:
            fut.cancel()
        if self.gateway_server is not None:
            self.gateway_server.should_exit = True
        if self.gateway is not None:
            await self.gateway.aclose()
        if self.runtime.name == "docker":
            with contextlib.suppress(Exception):
                await asyncio.to_thread(sandbox.teardown_isolation)

    async def run_forever(self) -> None:
        recover()
        log.info("orchestrator started (runtime=%s)", self.runtime.name)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # keep the loop alive; the error is logged
                log.exception("tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self.settings.poll_interval_s)
        await self.shutdown()

    def request_stop(self) -> None:
        self._stop.set()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    orch = Orchestrator()

    async def _amain() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, orch.request_stop)
        await orch.run_forever()

    asyncio.run(_amain())
