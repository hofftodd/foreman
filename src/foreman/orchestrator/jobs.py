"""Planner jobs, run by the orchestrator: the Project Planning Agent's chat turns
(JobKind.CHAT) and the Task Planning Agent's breakdown, splits and plan reviews
(DECOMPOSE, SPLIT, REVIEW). See core/roles.py for what each may do.

Jobs stream from the planner model and are stoppable mid-stream: Stop / E-stop
on the project (or the supervisor marking the job stopped) closes the model
connection. Results are applied only if the job is still running when the
model finishes (compare-and-set), so a stopped job never writes anything.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import delete, update
from sqlmodel import col, select

from foreman import gitops
from foreman.core import roles, spend
from foreman.core.contextpack import estimate_tokens, repo_files
from foreman.core.models import (
    JobKind,
    JobState,
    Node,
    PlannerJob,
    Project,
    ProjectPhase,
    RunState,
    SpecMessage,
    Task,
    TaskRun,
    TaskState,
    now,
)
from foreman.core.state import emit, stop_requested, transition
from foreman.db import session
from foreman.llm import Aborted, LLMError, stream_chat
from foreman.planner import (
    DOC_NAMES,
    PlanError,
    TaskSpec,
    chat_system_prompt,
    decompose_system_prompt,
    doc_path,
    parse_chat,
    parse_tasks,
    render_tasks_md,
    retry_message,
    split_system_prompt,
    standard_tasks,
    topo_order,
)

log = logging.getLogger("foreman.jobs")

MAX_DECOMPOSE_ATTEMPTS = 3
FLUSH_S = 0.25
BUDGET_POLL_S = 2.0


class JobFailed(Exception):
    pass


def finish_job(job_id: int, frm: tuple[JobState, ...], to: JobState, error: str = "") -> bool:
    with session() as s:
        res = s.exec(  # type: ignore[call-overload]
            update(PlannerJob)
            .where(col(PlannerJob.id) == job_id, col(PlannerJob.state).in_(frm))
            .values(state=to, error=error, ended_at=now())
        )
        s.commit()
        return bool(res.rowcount)


def set_phase(project_id: int, phase: ProjectPhase) -> None:
    with session() as s:
        p = s.get(Project, project_id)
        if p is not None:
            p.phase = phase
            s.add(p)
            s.commit()
    emit(project_id, None, "state", f"phase: {phase.value}", phase=phase.value)


def planner_note(project_id: int, text: str, job_id: int | None = None) -> None:
    """A message from foreman itself in the Phase 1 conversation."""
    with session() as s:
        s.add(SpecMessage(project_id=project_id, role="assistant", content=text, job_id=job_id))
        s.commit()


def read_docs(project: Project) -> dict[str, str]:
    assert project.id is not None
    return {name: gitops.read_repo_file(project.id, doc_path(project, name)) for name in DOC_NAMES}


def job_role(kind: JobKind) -> str:
    """Which agent runs a planner job."""
    return roles.PROJECT_PLANNER if kind == JobKind.CHAT else roles.TASK_PLANNER


def _fit_history(messages: list[dict[str, str]], budget: int) -> list[dict[str, str]]:
    """Drop the oldest turns until the conversation fits; always keep the last turn."""
    msgs = list(messages)
    while len(msgs) > 1 and sum(estimate_tokens(m["content"]) for m in msgs) > budget:
        msgs.pop(0)
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def _alternate(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Merge consecutive same-role turns (e.g. foreman notes after a planner reply)."""
    out: list[dict[str, str]] = []
    for m in messages:
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + m["content"]}
        else:
            out.append(dict(m))
    return out


class JobRunner:
    def __init__(self, job_id: int):
        self.job_id = job_id
        with session() as s:
            job = s.get(PlannerJob, job_id)
            assert job is not None
            self.project_id = job.project_id
            self.kind = job.kind
            self.task_id = job.task_id
            self.payload = dict(job.payload or {})
        self.started_at = time.time()
        # Streamed output waiting to be emitted, per event kind ("planner" is the reply,
        # "planner_thinking" a thinking model's reasoning).
        self._buf: dict[str, list[str]] = {}
        self._last_flush = time.monotonic()

    # -- helpers ------------------------------------------------------------

    def project(self) -> Project:
        with session() as s:
            p = s.get(Project, self.project_id)
            assert p is not None
            return p

    def should_abort(self) -> bool:
        with session() as s:
            job = s.get(PlannerJob, self.job_id)
        if job is None or job.state != JobState.RUNNING:
            return True
        return stop_requested(self.project_id, 0, self.started_at)

    def _on_text(self, text: str) -> None:
        self._buffer("planner", text)

    def _on_reasoning(self, text: str) -> None:
        self._buffer("planner_thinking", text)

    def _buffer(self, kind: str, text: str) -> None:
        self._buf.setdefault(kind, []).append(text)
        if time.monotonic() - self._last_flush > FLUSH_S:
            self._flush()

    def _flush(self) -> None:
        for kind, parts in self._buf.items():
            if parts:
                emit(self.project_id, None, kind, "".join(parts), job_id=self.job_id)
                parts.clear()
        self._last_flush = time.monotonic()

    def _record_usage(self, input_tokens: int, output_tokens: int) -> None:
        with session() as s:
            job = s.get(PlannerJob, self.job_id)
            if job is not None:
                job.input_tokens += input_tokens
                job.output_tokens += output_tokens
                s.add(job)
                s.commit()

    async def _wait_for_budget(self, node: Node) -> None:
        """Paid calls wait while the project is at its spend cap."""
        reason = spend.blocked(self.project_id, node)
        if not reason:
            return
        emit(self.project_id, None, "system", f"planner waiting: {reason}", job_id=self.job_id)
        while spend.blocked(self.project_id, node):
            if self.should_abort():
                raise Aborted()
            await asyncio.sleep(BUDGET_POLL_S)

    def _on_research(self, what: str) -> None:
        self._flush()
        emit(self.project_id, None, "research", what, job_id=self.job_id)

    async def _call(
        self,
        node: Node,
        model: str,
        system: str,
        messages: list[dict[str, str]],
        research: bool = False,
    ) -> str:
        await self._wait_for_budget(node)
        result = await stream_chat(
            node,
            model,
            system,
            messages,
            on_text=self._on_text,
            on_reasoning=self._on_reasoning,
            should_abort=self.should_abort,
            research=research,
            on_research=self._on_research,
        )
        self._flush()
        self._record_usage(result.input_tokens, result.output_tokens)
        spend.charge(self.project_id, node, result.input_tokens, result.output_tokens)
        if not result.text.strip():
            if result.truncated:
                doing = "thinking " if result.reasoning_chars else ""
                raise JobFailed(
                    f"the planner used its whole {result.max_tokens}-token output budget "
                    f"{doing}without answering; give the node a larger context window "
                    "or use a model that thinks less"
                )
            raise JobFailed("the planner returned an empty response")
        return result.text

    # -- entry point --------------------------------------------------------

    async def run(self) -> None:
        with session() as s:
            job = s.get(PlannerJob, self.job_id)
            assert job is not None
            if job.state != JobState.QUEUED:
                return
            job.state, job.started_at = JobState.RUNNING, now()
            s.add(job)
            s.commit()
        try:
            project = self.project()
            node, model = roles.node_and_model(project, job_role(self.kind))
            with session() as s:
                job = s.get(PlannerJob, self.job_id)
                assert job is not None
                job.node_id, job.model = (node.id if node else None), model
                s.add(job)
                s.commit()
            emit(
                self.project_id,
                None,
                "planner_start",
                f"planner {self.kind.value} started",
                job_id=self.job_id,
                job_kind=self.kind.value,
            )
            if node is None or not model:
                role = roles.ROLES[job_role(self.kind)].name
                raise JobFailed(f"no planner node/model configured for this project ({role})")
            if self.kind == JobKind.CHAT:
                await self._chat(node, model)
            elif self.kind == JobKind.DECOMPOSE:
                await self._decompose(node, model)
            elif self.kind == JobKind.SPLIT:
                await self._split(node, model)
            else:
                await self._review(node, model)
        except Aborted:
            self._flush()
            finish_job(self.job_id, (JobState.RUNNING,), JobState.STOPPED, "stopped by user")
            self._after_failure("stopped by user")
        except (JobFailed, LLMError) as e:
            self._fail(str(e))
        except Exception as e:  # noqa: BLE001 - never leave a job running
            log.exception("planner job %s crashed", self.job_id)
            self._fail(f"internal error: {type(e).__name__}: {e}")
        emit(
            self.project_id,
            None,
            "planner_end",
            f"planner {self.kind.value} finished",
            job_id=self.job_id,
        )

    def _fail(self, error: str) -> None:
        self._flush()
        # DONE too: the job may have been claimed as done and then failed to apply its
        # results (e.g. a git error); that must be reported, not swallowed.
        if finish_job(self.job_id, (JobState.RUNNING, JobState.DONE), JobState.FAILED, error):
            self._after_failure(error)

    def _after_failure(self, error: str) -> None:
        if self.kind == JobKind.DECOMPOSE:
            set_phase(self.project_id, ProjectPhase.SPECIFY)
            planner_note(self.project_id, f"Task breakdown did not complete: {error}", self.job_id)
        elif self.kind == JobKind.SPLIT:
            from foreman.orchestrator.escalation import split_failed

            if self.task_id is not None:
                split_failed(self.task_id, error)
        elif self.kind == JobKind.REVIEW:
            emit(self.project_id, None, "system", f"replanning failed: {error}", job_id=self.job_id)
        else:
            planner_note(self.project_id, f"(planner error: {error})", self.job_id)

    # -- chat ---------------------------------------------------------------

    async def _chat(self, node: Node, model: str) -> None:
        project = self.project()
        docs = read_docs(project)
        tree = repo_files(gitops.repo_dir(self.project_id))
        system = chat_system_prompt(project, docs, tree)
        with session() as s:
            history = s.exec(
                select(SpecMessage)
                .where(SpecMessage.project_id == self.project_id)
                .order_by(col(SpecMessage.id))
            ).all()
        messages = _alternate([{"role": m.role, "content": m.content} for m in history])
        budget = node.context_window // 2 - estimate_tokens(system)
        messages = _fit_history(messages, max(budget, 2000))
        if not messages or messages[-1]["role"] != "user":
            raise JobFailed("nothing to answer")

        # Only the Project Planning Agent may research on the web (core/roles.py).
        text = await self._call(node, model, system, messages, research=True)
        out = parse_chat(project, text)
        if not finish_job(self.job_id, (JobState.RUNNING,), JobState.DONE):
            return  # stopped while we were finishing: apply nothing
        if out.files:
            first = out.message.splitlines()[0][:60] if out.message else "update"
            await asyncio.to_thread(
                gitops.write_and_commit, self.project_id, out.files, f"spec: {first}"
            )
        for what in out.rejected:
            roles.denied(self.project_id, None, roles.PROJECT_PLANNER, f"write {what}")
        content = out.message or "(updated files)"
        if out.rejected:
            content += (
                "\n\n(foreman refused: " + ", ".join(out.rejected) + ". The planner writes only "
                "the planning documents.)"
            )
        with session() as s:
            s.add(
                SpecMessage(
                    project_id=self.project_id,
                    role="assistant",
                    content=content,
                    files=sorted(out.files),
                    job_id=self.job_id,
                )
            )
            s.commit()

    # -- decomposition ------------------------------------------------------

    async def _plan_loop(
        self,
        node: Node,
        model: str,
        system: str,
        first_message: str,
        what: str,
    ) -> list[TaskSpec]:
        """Ask for a task list until it parses and passes the checks (or attempts run out)."""
        messages = [{"role": "user", "content": first_message}]
        last_error = ""
        for attempt in range(1, MAX_DECOMPOSE_ATTEMPTS + 1):
            text = await self._call(node, model, system, messages)
            try:
                return parse_tasks(text)
            except PlanError as e:
                last_error = str(e)
            emit(
                self.project_id,
                None,
                "system",
                f"{what} attempt {attempt}: {last_error}",
                problems=[last_error],
            )
            if attempt < MAX_DECOMPOSE_ATTEMPTS:
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": retry_message([last_error])},
                ]
        raise JobFailed(f"could not get a valid task list: {last_error}")

    async def _decompose(self, node: Node, model: str) -> None:
        project = self.project()
        system = decompose_system_prompt(project, read_docs(project))
        specs = await self._plan_loop(
            node, model, system, "Break the plan into tasks now.", "task breakdown"
        )
        if not finish_job(self.job_id, (JobState.RUNNING,), JobState.DONE):
            return
        self._create_tasks(project, standard_tasks(specs))

    def _create_tasks(self, project: Project, specs: list[TaskSpec]) -> None:
        review = bool((project.gates or {}).get("plan_approval", True))
        initial = TaskState.PROPOSED if review else TaskState.QUEUED
        ordered = topo_order(specs)
        ids: dict[str, int] = {}
        with session() as s:
            s.exec(  # type: ignore[call-overload]
                delete(Task).where(
                    col(Task.project_id) == self.project_id,
                    col(Task.state) == TaskState.PROPOSED,
                )
            )
            for t in ordered:
                row = task_row(self.project_id, t, deps=[ids[d] for d in t.deps], state=initial)
                s.add(row)
                s.flush()
                assert row.id is not None
                ids[t.key] = row.id
            s.commit()
        rows: list[dict[str, Any]] = [t.__dict__ for t in ordered]
        gitops.write_and_commit(
            self.project_id,
            {doc_path(project, "tasks"): render_tasks_md(project, rows)},
            f"tasks: {len(ordered)} tasks from plan",
        )
        if not review:
            with session() as s:
                p = s.get(Project, self.project_id)
                assert p is not None
                p.baseline_tasks = len(ordered)
                s.add(p)
                s.commit()
        set_phase(self.project_id, ProjectPhase.PLAN_REVIEW if review else ProjectPhase.EXECUTE)
        planner_note(
            self.project_id,
            f"Broke the plan into {len(ordered)} tasks, including setup, acceptance tests and "
            "documentation.",
            self.job_id,
        )

    # -- split ---------------------------------------------------------------

    async def _split(self, node: Node, model: str) -> None:
        project = self.project()
        assert self.task_id is not None
        with session() as s:
            task = s.get(Task, self.task_id)
            runs = s.exec(
                select(TaskRun)
                .where(TaskRun.task_id == self.task_id, TaskRun.state == RunState.FAILED)
                .order_by(col(TaskRun.id))
            ).all()
        if task is None or task.state not in (TaskState.FAILED, TaskState.BLOCKED):
            raise JobFailed("the task is no longer waiting to be split")
        failures = [r.reason for r in runs[-4:] if r.reason] or [self.payload.get("reason", "")]
        original = {
            "key": task.key or f"T{task.id}",
            "title": task.title,
            "goal": task.description,
            "req_refs": task.req_refs,
            "criteria": task.criteria,
            "deps": [],
            "size": "medium",
        }
        system = split_system_prompt(project, read_docs(project), original, failures)
        specs = await self._plan_loop(node, model, system, "Split the task now.", "task split")
        if not finish_job(self.job_id, (JobState.RUNNING,), JobState.DONE):
            return
        children = replace_task(task, specs, origin="split")
        write_tasks_md(project)
        emit(
            self.project_id,
            self.task_id,
            "system",
            f"split into {len(children)} tasks: {', '.join(c.key for c in children)}",
        )

    async def _review(self, node: Node, model: str) -> None:
        from foreman.orchestrator.replan import run_review

        await run_review(self, node, model)


def task_row(project_id: int, t: TaskSpec, **fields: Any) -> Task:
    """A task table row from a Task Planner task."""
    return Task(
        project_id=project_id,
        key=t.key,
        title=t.title,
        description=t.goal,
        req_refs=t.req_refs,
        criteria=t.criteria,
        kind=t.kind,
        pack_tokens=estimate_tokens(t.goal + " ".join(t.criteria)),
        **fields,
    )


def replace_task(task: Task, specs: list[TaskSpec], origin: str) -> list[Task]:
    """Swap a task for new ones (split). Dependents now wait for all of them."""
    assert task.id is not None
    base = task.key or f"T{task.id}"
    keymap = {t.key: f"{base}.{i}" for i, t in enumerate(topo_order(specs), 1)}
    children: list[Task] = []
    ids: dict[str, int] = {}
    with session() as s:
        for t in topo_order(specs):
            row = task_row(
                task.project_id,
                t,
                deps=[*task.deps, *[ids[d] for d in t.deps]],
                parent_id=task.id,
                origin=origin,
                executor_kind=task.executor_kind,
                timeout_s=task.timeout_s,
                state=TaskState.QUEUED,
            )
            row.key = keymap[t.key]
            s.add(row)
            s.flush()
            assert row.id is not None
            ids[t.key] = row.id
            children.append(row)
        all_ids = [c.id for c in children if c.id is not None]
        for dependent in s.exec(select(Task).where(Task.project_id == task.project_id)).all():
            if task.id in (dependent.deps or []):
                dependent.deps = [d for d in dependent.deps if d != task.id] + all_ids
                s.add(dependent)
        s.commit()
    transition(
        task.id,
        (TaskState.FAILED, TaskState.BLOCKED),
        TaskState.SPLIT,
        f"split into {', '.join(c.key for c in children)}",
    )
    return children


def write_tasks_md(project: Project) -> None:
    """Re-render specs/<feature>/tasks.md from the task table and commit it."""
    assert project.id is not None
    with session() as s:
        tasks = s.exec(
            select(Task).where(Task.project_id == project.id).order_by(col(Task.id))
        ).all()
    keys = {t.id: t.key or f"#{t.id}" for t in tasks}
    rows = [
        {
            "key": t.key or f"#{t.id}",
            "title": t.title,
            "goal": t.description,
            "criteria": t.criteria,
            "req_refs": t.req_refs,
            "deps": [keys.get(d, f"#{d}") for d in t.deps],
            "kind": t.kind,
            "violations": t.size_violations,
            "state": t.state.value,
        }
        for t in tasks
        if t.state not in (TaskState.DISCARDED,)
    ]
    gitops.write_and_commit(
        project.id,
        {doc_path(project, "tasks"): render_tasks_md(project, rows)},
        "tasks: update task list",
    )
