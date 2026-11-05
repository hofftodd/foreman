"""Run one task attempt end to end.

    prepare clone -> executor step -> commit -> scope check
      -> validation step(s) -> merge into milestone branch -> done

Before every side-effecting step the runner re-reads the task: if the
supervisor marked it stopped (or E-stop is on) it bails out without touching
anything. State changes use compare-and-set transitions, so a Stop that lands
mid-step always wins.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlmodel import col, select

from foreman import gitops, images, sandbox, testresults
from foreman.config import get_settings
from foreman.core import gates, roles, watchdog
from foreman.core.contextpack import build_pack, build_role_pack
from foreman.core.models import (
    GateKind,
    Node,
    Project,
    RunState,
    Task,
    TaskRun,
    TaskState,
    TestCase,
    TestRun,
    now,
)
from foreman.core.state import ACTIVE_STATES, emit, finish_run, stop_requested, transition
from foreman.core.watchdog import failure_signature
from foreman.db import session
from foreman.executors import EXIT_CONFIG, Executor, ExecutorContext, OutputParser, get_executor
from foreman.executors.shell import role_scripts
from foreman.gateway import new_token
from foreman.sandbox import Proc, Runtime, StepSpec, get_runtime, kill_handle

SUITE_RUNNER = "tests/run.sh"
ACCEPTANCE_RUNNER = "tests/acceptance/run.sh"
LOG_FLUSH_S = 0.25
LOG_BATCH = 50


class Stopped(Exception):
    """The task was stopped (by the user or E-stop) while we were working."""


class StepFailed(Exception):
    """The attempt failed. `output` is the failing step's tail, for the signature."""

    kind = "executor"

    def __init__(self, reason: str, output: str = "", kind: str = ""):
        super().__init__(reason)
        self.output = output
        if kind:
            self.kind = kind


class ConfigError(StepFailed):
    """A configuration problem (no node, bad executor...): retrying can't help."""

    kind = "config"


class WatchdogTrip(StepFailed):
    kind = "watchdog"


def resolve_executor(
    project: Project, task: Task, role: str = roles.BUILDER
) -> tuple[str, int | None, str]:
    """(executor kind, node id, model) for one of the task's coding agents. A task-level
    node/model (set when escalated) applies to both the Tester and the Builder."""
    kind = task.executor_kind or project.executor_kind
    binding = roles.binding(project, role)
    node_id = task.node_id or binding.get("node_id")
    model = task.model or binding.get("model", "")
    return kind, node_id, model


def uses_pipeline(task: Task, executor: Executor) -> bool:
    """Agent tasks go Tester -> Builder; scripted shell tasks run their one script, unless
    it has a script per role (how Foreman's tests drive the pipeline)."""
    return executor.needs_node or bool(role_scripts(task.executor_script))


def first_role(task: Task) -> str:
    return roles.BUILDER if task.tests_ready else roles.TESTER


def store_results(
    project_id: int,
    task_id: int | None,
    run_id: int | None,
    step: str,
    commit: str,
    res: testresults.Results,
    task_tests: list[str] | None = None,
    changed_cov: float | None = None,
    changed_lines: int = 0,
) -> TestRun:
    """Save one suite run's results and coverage."""
    run = TestRun(
        project_id=project_id,
        task_id=task_id,
        run_id=run_id,
        step=step,
        commit=commit,
        passed=res.count("passed"),
        failed=res.count("failed"),
        skipped=res.count("skipped"),
        errors=res.count("error"),
        duration_s=sum(c.duration_s for c in res.cases),
        coverage=res.coverage,
        changed_coverage=changed_cov,
        changed_lines=changed_lines,
        files=res.per_file(),
        problem=res.problem,
    )
    with session() as s:
        s.add(run)
        s.commit()
        s.refresh(run)
        assert run.id is not None
        for c in res.cases:
            s.add(
                TestCase(
                    test_run_id=run.id,
                    name=c.name,
                    classname=c.classname,
                    file=c.file,
                    status=c.status,
                    duration_s=c.duration_s,
                    message=c.message,
                    task_test=testresults.is_task_case(c, task_tests or []),
                )
            )
        s.commit()
        s.refresh(run)
    return run


class TaskRunner:
    def __init__(
        self,
        task_id: int,
        run_id: int,
        runtime: Runtime | None = None,
        gateway_url: str = "",
        gateway_port: int = 0,
    ):
        self.task_id = task_id
        self.run_id = run_id
        self.runtime = runtime or get_runtime()
        # Base URL of the model gateway as seen from where the executor runs.
        self.gateway_url = gateway_url
        self.gateway_port = gateway_port
        self._tail: list[str] = []
        self.image = ""  # set once the attempt's toolchain image is ready
        self.settings = get_settings()
        self.run_dir = self.settings.runs_dir / str(run_id)
        with session() as s:
            run = s.get(TaskRun, run_id)
            assert run is not None
            self.started_at = run.started_at

    # -- helpers ---------------------------------------------------------

    def _load(self) -> tuple[Task, Project]:
        with session() as s:
            task = s.get(Task, self.task_id)
            assert task is not None
            project = s.get(Project, task.project_id)
            assert project is not None
            return task, project

    def _check(self) -> Task:
        """Raise Stopped if the task should not advance any further."""
        task, _ = self._load()
        if task.state not in ACTIVE_STATES:
            raise Stopped(task.state.value)
        if stop_requested(task.project_id, self.task_id, self.started_at):
            raise Stopped("stop requested")
        return task

    def _set_handle(self, handle: str) -> None:
        with session() as s:
            run = s.get(TaskRun, self.run_id)
            assert run is not None
            run.handle = handle
            s.add(run)
            s.commit()

    def _emit(self, kind: str, message: str, **data: object) -> None:
        task, _ = self._load()
        emit(task.project_id, self.task_id, kind, message, run_id=self.run_id, **data)

    async def _step(
        self,
        name: str,
        command: list[str],
        env: dict[str, str],
        kind: str,
        timeout_s: float,
        output: OutputParser | None = None,
        **mounts: Any,
    ) -> int:
        """Run one container step, streaming output into events and a log file.

        `output` renders the executor's raw output; `mounts` passes role restrictions
        and the results directory to StepSpec."""
        task, project = self._load()
        spec = StepSpec(
            name=f"foreman-run{self.run_id}-{name}-{int(time.time() * 1000) % 10**8}",
            workspace=gitops.task_dir(task.project_id, self.task_id),
            ctl_dir=self.run_dir / "ctl",
            command=command,
            env=env,
            labels={
                "foreman.run": str(self.run_id),
                "foreman.task": str(self.task_id),
                "foreman.project": str(task.project_id),
            },
            network=project.network,
            image=self.image,
            **mounts,
        )
        proc = await self.runtime.start(spec)
        self._set_handle(proc.handle)
        # A Stop may have landed before the handle was recorded: honour it ourselves.
        try:
            self._check()
        except Stopped:
            await asyncio.to_thread(kill_handle, proc.handle, 0)
            await proc.wait()
            raise
        self._tail = []
        watcher = asyncio.create_task(self._watch(proc.handle))
        try:
            code = await asyncio.wait_for(self._pump(proc, kind, name, output), timeout_s)
        except TimeoutError:
            await asyncio.to_thread(kill_handle, proc.handle)
            raise WatchdogTrip(
                f"{name} timed out after {int(timeout_s)}s", "\n".join(self._tail)
            ) from None
        finally:
            watcher.cancel()
        reason = self._watchdog_reason()
        if reason:
            kind = "budget" if reason.startswith("spend cap") else ""
            raise WatchdogTrip(f"watchdog: {reason}", reason, kind)
        return code

    async def _toolchain_image(self, project_id: int, clone: Path) -> str:
        """Build (or reuse) the project's .foreman/Dockerfile image for this attempt. A
        broken Dockerfile is a configuration problem: retrying the agent can't fix it."""
        if self.runtime.name != "docker" or images.project_dockerfile(clone) is None:
            return ""
        lines: list[str] = []

        def output(line: str) -> None:
            lines.append(line)
            if len(lines) >= LOG_BATCH:
                self._emit("log", "\n".join(lines), step="image")
                lines.clear()

        self._emit("system", f"preparing toolchain image from {images.DOCKERFILE}")
        try:
            tag = await images.ensure_image(
                project_id, clone, on_output=output, on_handle=self._set_handle
            )
        except images.ImageBuildError as e:
            self._check()  # a Stop that killed the build is a stop, not a failure
            raise ConfigError(f"{e}; fix {images.DOCKERFILE}", e.output) from e
        finally:
            if lines:
                self._emit("log", "\n".join(lines), step="image")
        self._emit("system", f"toolchain image {tag}")
        return tag

    async def _isolate(self, setup: Callable[[], None]) -> None:
        try:
            await asyncio.to_thread(setup)
        except sandbox.IsolationError as e:
            raise ConfigError(f"isolated executor network: {e}") from e

    async def _gateway_base(self, project: Project) -> str:
        """The gateway as the executor reaches it: through the relay on an isolated
        network, directly via host.docker.internal on an open one."""
        if self.runtime.name != "docker" or project.network == "open":
            return self.gateway_url
        port = self.gateway_port
        await self._isolate(lambda: sandbox.ensure_relay(port))
        return sandbox.RELAY_URL

    def _watchdog_reason(self) -> str:
        with session() as s:
            run = s.get(TaskRun, self.run_id)
            return run.watchdog if run else ""

    async def _watch(self, handle: str) -> None:
        """Kill the step as soon as the gateway trips the watchdog for this run."""
        while True:
            await asyncio.sleep(0.5)
            if self._watchdog_reason():
                await asyncio.to_thread(kill_handle, handle)
                return

    async def _pump(
        self, proc: Proc, kind: str, name: str, output: OutputParser | None = None
    ) -> int:
        """Raw output goes to the step's log file; what `output` renders (default: the
        raw line) goes to the dashboard and the failure tail."""
        task, _ = self._load()
        log_path = self.run_dir / f"{name}.log"
        buf: list[str] = []
        last = time.monotonic()

        def flush() -> None:
            nonlocal last
            if buf:
                emit(
                    task.project_id,
                    self.task_id,
                    kind,
                    "\n".join(buf),
                    run_id=self.run_id,
                    step=name,
                )
                buf.clear()
            last = time.monotonic()

        with log_path.open("a") as log:
            async for line in proc.lines():
                log.write(line + "\n")
                shown = output.render(line) if output else line
                if shown is None:
                    continue
                buf.append(shown)
                self._tail = [*self._tail[-79:], *shown.splitlines()][-80:]
                if len(buf) >= LOG_BATCH or time.monotonic() - last > LOG_FLUSH_S:
                    flush()
            flush()
        return await proc.wait()

    # -- main ------------------------------------------------------------

    async def run(self) -> None:
        try:
            await self._run()
        except Stopped as e:
            # The supervisor normally records this first; whoever gets there first wins and
            # the other write is a no-op. Never report a user stop as a failure.
            task, _ = self._load()
            if stop_requested(task.project_id, self.task_id, self.started_at):
                transition(
                    self.task_id, ACTIVE_STATES, TaskState.STOPPED_BY_USER, "stop requested by user"
                )
            finish_run(self.run_id, RunState.STOPPED, f"stopped ({e})")
        except StepFailed as e:
            self._fail(str(e), e.output, e.kind)
        except Exception as e:  # noqa: BLE001 - any crash fails the task, never wedges it
            self._fail(f"internal error: {type(e).__name__}: {e}", kind="internal")
        self._after()

    def _after(self) -> None:
        """Next step once the attempt is over: escalate a failure. (Successes queue their
        plan review in merge_task.)"""
        from foreman.orchestrator import escalation

        task, _ = self._load()
        try:
            if task.state == TaskState.FAILED:
                escalation.after_failure(self.task_id, self.run_id)
        except Exception:  # noqa: BLE001 - never let bookkeeping crash the runner
            import logging

            logging.getLogger("foreman.runner").exception("post-run step failed")

    def _fail(self, reason: str, output: str = "", kind: str = "executor") -> None:
        task, _ = self._load()
        if stop_requested(task.project_id, self.task_id, self.started_at):
            # The failure is a consequence of the stop (e.g. killed process): report the stop.
            transition(
                self.task_id, ACTIVE_STATES, TaskState.STOPPED_BY_USER, "stop requested by user"
            )
            finish_run(self.run_id, RunState.STOPPED, "stop requested by user")
            return
        with session() as s:
            run = s.get(TaskRun, self.run_id)
            if run is not None:
                run.error_sig = failure_signature(output or reason)
                run.failure_kind = kind
                s.add(run)
                s.commit()
        finish_run(self.run_id, RunState.FAILED, reason)
        transition(
            self.task_id, (TaskState.RUNNING, TaskState.VALIDATING), TaskState.FAILED, reason
        )

    async def _run(self) -> None:
        task, project = self._load()
        (self.run_dir / "ctl").mkdir(parents=True, exist_ok=True)

        clone = await asyncio.to_thread(
            gitops.prepare_task_clone, task.project_id, self.task_id, project.milestone_branch
        )
        self._check()
        # Fail fast on a broken Dockerfile, before any model or gateway work.
        self.image = await self._toolchain_image(task.project_id, clone)
        self._check()

        kind, _, _ = resolve_executor(project, task)
        executor = get_executor(kind)
        if uses_pipeline(task, executor):
            await self._pipeline(task, project, clone, executor)
        else:
            await self._single_step(task, project, clone, executor)

    async def _agent_setup(
        self, project: Project, task: Task, role: str, executor: Executor
    ) -> tuple[Node | None, str, str]:
        """(node, model, gateway endpoint) for the current run's agent. On an isolated
        network, also makes sure the executor network (and the gateway relay) exist."""
        kind, node_id, model = resolve_executor(project, task, role)
        node = None
        if node_id is not None:
            with session() as s:
                node = s.get(Node, node_id)
        who = roles.ROLES[role].name if role in roles.ROLES else "executor"
        if executor.needs_node and (node is None or not model):
            raise ConfigError(f"no node/model configured for the {who} in this project or task")
        if executor.needs_node and node is not None and node.provider not in executor.providers:
            raise ConfigError(
                f"the {kind} executor can't use node {node.name} ({node.provider} API); "
                f"it supports: {', '.join(sorted(executor.providers))}"
            )
        endpoint = ""
        if executor.needs_node:
            if not self.gateway_url:
                raise ConfigError("the model gateway is not running")
            base = await self._gateway_base(project)
            with session() as s:
                run = s.get(TaskRun, self.run_id)
                assert run is not None
                endpoint = f"{base}/g/{run.gateway_token}/v1"
        elif self.runtime.name == "docker" and project.network != "open":
            await self._isolate(sandbox.ensure_exec_network)
        return node, model, endpoint

    # -- scripted tasks: one step, the task's own validation command -------------

    async def _single_step(
        self, task: Task, project: Project, clone: Path, executor: Executor
    ) -> None:
        kind = executor.kind
        node, model, endpoint = await self._agent_setup(project, task, roles.BUILDER, executor)
        pack = await asyncio.to_thread(
            build_pack,
            project,
            task,
            clone,
            context_window=node.context_window if node else None,
            previous_failure=self._previous_failure(),
        )
        if pack.over_budget and executor.needs_node:
            raise StepFailed(
                f"context pack ~{pack.tokens} tokens exceeds budget {pack.budget}; split the task",
                kind="too_big",
            )
        (self.run_dir / "context_pack.md").write_text(pack.text)
        inv = executor.prepare(
            project,
            task,
            endpoint,
            model,
            self.run_dir / "ctl",
            pack.text,
            provider=node.provider if node else "openai",
            context=ExecutorContext(context_window=node.context_window if node else None),
        )
        self._emit(
            "system", f"executor {kind} starting" + (f" on {node.name} / {model}" if node else "")
        )
        code = await self._step(
            "execute", inv.command, inv.env, "log", task.timeout_s, output=inv.output
        )
        task = self._check()
        await asyncio.to_thread(gitops.commit_all, clone, f"Task {self.task_id}: {task.title}")
        if code == EXIT_CONFIG and executor.needs_node:
            raise ConfigError(self._tail[-1] if self._tail else f"{kind} is not usable here")
        if code != 0:
            raise StepFailed(f"executor exited with status {code}", "\n".join(self._tail))
        failure = inv.output.failure() if inv.output else None
        if failure:
            raise StepFailed(f"executor failed: {failure}", "\n".join(self._tail))

        # Scope fence and diff size (FR-5.3)
        files = await asyncio.to_thread(gitops.changed_files, clone)
        outside = gitops.out_of_scope(files, task.allowed_globs)
        if outside:
            raise WatchdogTrip(f"changed files outside allowed scope: {', '.join(outside)}")
        self._check_diff_size(project, clone, files)

        if task.hold:
            transition(self.task_id, (TaskState.RUNNING,), TaskState.PAUSED, "paused after execute")
            finish_run(self.run_id, RunState.SUCCEEDED, "paused after execute")
            return

        # Validation
        if not transition(self.task_id, (TaskState.RUNNING,), TaskState.VALIDATING):
            raise Stopped("not running")
        checks = [("validate", task.validation_cmd), ("tests", project.test_cmd)]
        for name, cmd in checks:
            if not cmd:
                continue
            self._check()
            self._emit("test", f"$ {cmd}", step=name)
            code = await self._step(name, ["bash", "-lc", cmd], {}, "test", task.timeout_s)
            if code != 0:
                raise StepFailed(
                    f"{name} failed (exit {code}): {cmd}", "\n".join(self._tail), kind="validation"
                )
            self._emit("test", f"{name} passed", step=name, passed=True)
        await self._merge_or_gate(project)

    def _check_diff_size(
        self, project: Project, clone: Path, files: list[str], base: str = gitops.BASE_REF
    ) -> None:
        lines = gitops.diff_size(clone, base)
        with session() as s:
            run = s.get(TaskRun, self.run_id)
            assert run is not None
            run.diff_lines = lines
            s.add(run)
            s.commit()
        max_lines = watchdog.limit(project, "max_diff_lines")
        if lines > max_lines:
            raise WatchdogTrip(f"diff too large: {lines} lines changed (limit {max_lines})")
        self._emit("diff", f"{len(files)} file(s) changed, {lines} lines", files=files)

    async def _merge_or_gate(self, project: Project) -> None:
        """The task passed: merge it, or wait for the user's approval (task_merge)."""
        task = self._check()
        if task.hold:
            transition(
                self.task_id, (TaskState.VALIDATING,), TaskState.PAUSED, "paused after validation"
            )
            finish_run(self.run_id, RunState.SUCCEEDED, "paused after validation")
            return
        if gates.enabled(project, "task_merge"):
            transition(
                self.task_id,
                (TaskState.VALIDATING,),
                TaskState.AWAITING_MERGE,
                "validated; waiting for merge approval",
            )
            finish_run(self.run_id, RunState.SUCCEEDED, "awaiting merge approval")
            gates.open_gate(
                task.project_id,
                GateKind.TASK_MERGE,
                f"Merge task {task.key or self.task_id}: {task.title}?",
                "The task passed its checks. Review the diff, then approve the merge or reject it.",
                task_id=self.task_id,
            )
            return
        try:
            await asyncio.to_thread(merge_task, self.task_id)
        except gitops.GitError as e:
            raise StepFailed(str(e), kind="merge") from e
        finish_run(self.run_id, RunState.SUCCEEDED)

    # -- agent tasks: Tester -> Builder -> verify -----------------------------------

    async def _pipeline(
        self, task: Task, project: Project, clone: Path, executor: Executor
    ) -> None:
        """The Tester writes the task's tests (once), the Builder makes them pass, and the
        suite's results and coverage decide whether it merges."""
        if task.tests_ready and not gitops.has_commit(clone, task.tests_commit):
            task = self._update_task(tests_ready=False, tests_commit="")  # clone was reset
        steps = [] if task.tests_ready else [roles.TESTER]
        if task.kind != "acceptance":
            steps.append(roles.BUILDER)
        for i, role in enumerate(steps):
            if i == 0:
                self._set_run(step=role)
            else:
                await self._next_run(role, project, task)
            if role == roles.TESTER:
                await self._tester(project, clone, executor)
            else:
                await self._builder(project, clone, executor)
            task = self._check()
        if task.kind == "acceptance" or not steps:
            if not transition(self.task_id, (TaskState.RUNNING,), TaskState.VALIDATING):
                raise Stopped("not running")
            await self._merge_or_gate(project)
            return
        # The Builder's verify step left the task VALIDATING.
        if await self._coverage_ok(project):
            await self._merge_or_gate(project)

    def _set_run(self, **fields: Any) -> None:
        with session() as s:
            run = s.get(TaskRun, self.run_id)
            assert run is not None
            for k, v in fields.items():
                setattr(run, k, v)
            s.add(run)
            s.commit()

    def _update_task(self, **fields: Any) -> Task:
        with session() as s:
            t = s.get(Task, self.task_id)
            assert t is not None
            for k, v in fields.items():
                setattr(t, k, v)
            t.updated_at = now()
            s.add(t)
            s.commit()
            s.refresh(t)
            return t

    async def _next_run(self, role: str, project: Project, task: Task) -> None:
        """Hand over to the next agent: a new run, on its own node and gateway token."""
        from foreman.orchestrator.main import node_available, node_load

        finish_run(self.run_id, RunState.SUCCEEDED, "step done")
        _, node_id, model = resolve_executor(project, task, role)
        while node_id is not None:  # wait for a free slot on the next agent's node
            with session() as s:
                node = s.get(Node, node_id)
            if node is None or node_available(node, node_load()):
                break
            self._check()
            await asyncio.sleep(1)
        with session() as s:
            run = TaskRun(
                task_id=self.task_id,
                project_id=task.project_id,
                node_id=node_id,
                model=model,
                gateway_token=new_token(),
                step=role,
            )
            s.add(run)
            s.commit()
            assert run.id is not None
            self.run_id = run.id
        self.run_dir = self.settings.runs_dir / str(self.run_id)
        (self.run_dir / "ctl").mkdir(parents=True, exist_ok=True)

    async def _agent(
        self, role: str, project: Project, clone: Path, executor: Executor, task_tests: list[str]
    ) -> list[str]:
        """Run one agent in its sandbox; returns the files it changed. Changes outside the
        role's capabilities are thrown away and fail the step."""
        task = self._check()
        node, model, endpoint = await self._agent_setup(project, task, role, executor)
        start = gitops.head(clone)
        test_paths = [d.strip("/") for d in (project.test_paths or ["tests"]) if d.strip("/")]
        from foreman.planner import BASE_IMAGE, doc_path

        pack = await asyncio.to_thread(
            build_role_pack,
            project,
            task,
            clone,
            role,
            docs={k: doc_path(project, k) for k in roles.PLANNING_DOCS},
            test_paths=test_paths,
            task_tests=task_tests,
            base_image=BASE_IMAGE,
            context_window=node.context_window if node else None,
            previous_failure=self._previous_failure(),
        )
        if pack.over_budget and executor.needs_node:
            raise StepFailed(
                f"context pack ~{pack.tokens} tokens exceeds budget {pack.budget}; split the task",
                kind="too_big",
            )
        (self.run_dir / "context_pack.md").write_text(pack.text)
        inv = executor.prepare(
            project,
            task,
            endpoint,
            model,
            self.run_dir / "ctl",
            pack.text,
            provider=node.provider if node else "openai",
            role=role,
            context=ExecutorContext(context_window=node.context_window if node else None),
        )
        who = roles.ROLES[role].name
        self._emit("system", f"{who} starting" + (f" on {node.name} / {model}" if node else ""))
        mounts: dict[str, Any] = {"results_dir": self._results_dir(f"{role}-agent")}
        if role == roles.TESTER:
            mounts |= {"workspace_readonly": True, "writable_dirs": test_paths}
        else:
            mounts |= {"readonly_dirs": test_paths}
        code = await self._step(
            role, inv.command, inv.env, "log", task.timeout_s, output=inv.output, **mounts
        )
        task = self._check()
        await asyncio.to_thread(
            gitops.commit_all, clone, f"Task {task.key or self.task_id} ({role}): {task.title}"
        )
        changed = gitops.changed_since(clone, start)
        outside = roles.fence(project, role, changed)
        if outside:
            await asyncio.to_thread(gitops.reset_to, clone, start)
            what = "change tests" if role == roles.BUILDER else "change non-test files"
            roles.denied(task.project_id, self.task_id, role, what, files=outside)
            raise WatchdogTrip(
                f"the {who} changed files it may not touch: {', '.join(outside)}", kind="fence"
            )
        if code == EXIT_CONFIG and executor.needs_node:
            raise ConfigError(self._tail[-1] if self._tail else f"{executor.kind} is not usable")
        if code != 0:
            raise StepFailed(f"the {who} exited with status {code}", "\n".join(self._tail))
        failure = inv.output.failure() if inv.output else None
        if failure:
            raise StepFailed(f"the {who} failed: {failure}", "\n".join(self._tail))
        self._emit("diff", f"{who}: {len(changed)} file(s) changed", files=changed, role=role)
        return changed

    async def _tester(self, project: Project, clone: Path, executor: Executor) -> None:
        task = self._check()
        changed = await self._agent(roles.TESTER, project, clone, executor, [])
        if not changed:
            raise StepFailed("the Tester added no tests for this task", kind="no_tests")
        if task.kind == "acceptance" and not (clone / ACCEPTANCE_RUNNER).is_file():
            raise StepFailed(f"the Tester did not write {ACCEPTANCE_RUNNER}", kind="no_tests")
        if not self._suite_cmd(project, clone):
            raise StepFailed(f"there is no test runner: {SUITE_RUNNER} is missing", kind="no_tests")
        # Record the new tests' first results: they are expected to fail for now.
        await self._run_suite(project, clone, "tester", changed, base="")
        self._update_task(tests_ready=True, tests_commit=gitops.head(clone))

    async def _builder(self, project: Project, clone: Path, executor: Executor) -> None:
        task = self._check()
        task_tests = gitops.changed_since(clone, gitops.BASE_REF)
        task_tests = [f for f in task_tests if roles.is_test_path(project, f)]
        changed = await self._agent(roles.BUILDER, project, clone, executor, task_tests)
        self._check_diff_size(project, clone, changed, base=task.tests_commit or gitops.BASE_REF)
        if task.hold:
            transition(self.task_id, (TaskState.RUNNING,), TaskState.PAUSED, "paused after build")
            finish_run(self.run_id, RunState.SUCCEEDED, "paused after build")
            raise Stopped("paused")
        # Verify: the whole suite, in the (possibly just changed) toolchain image.
        if not transition(self.task_id, (TaskState.RUNNING,), TaskState.VALIDATING):
            raise Stopped("not running")
        self.image = await self._toolchain_image(task.project_id, clone)
        # Changed-line coverage covers all the code this task changed, across rounds.
        result = await self._run_suite(project, clone, "builder", task_tests, base=gitops.BASE_REF)
        if result is None:
            raise StepFailed(
                f"there is no test runner: {SUITE_RUNNER} is missing", kind="validation"
            )
        run, code = result
        if run.problem:
            raise StepFailed(f"the test results are unusable: {run.problem}", kind="validation")
        failing = run.failed + run.errors
        if code != 0 or failing:
            raise StepFailed(
                f"tests fail: {failing} of {run.passed + failing} failed",
                "\n".join(self._tail),
                kind="validation",
            )

    def _results_dir(self, name: str) -> Path:
        path = self.run_dir / "results" / name
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        return path

    def _suite_cmd(self, project: Project, clone: Path) -> str:
        if project.test_cmd:
            return project.test_cmd
        return f"sh {SUITE_RUNNER}" if (clone / SUITE_RUNNER).is_file() else ""

    async def _run_suite(
        self, project: Project, clone: Path, step: str, task_tests: list[str], base: str
    ) -> tuple[TestRun, int] | None:
        """Run the whole suite, store its results and coverage. `base` is where the Builder's
        changes start (for changed-line coverage); "" for none."""
        cmd = self._suite_cmd(project, clone)
        if not cmd:
            return None
        task = self._check()
        results_dir = self._results_dir(f"{step}-suite")
        self._emit("test", f"$ {cmd}", step=f"tests-{step}")
        code = await self._step(
            f"tests-{step}",
            ["bash", "-lc", cmd],
            {},
            "test",
            task.timeout_s,
            results_dir=results_dir,
        )
        await asyncio.to_thread(gitops.clean_tree, clone)
        res = await asyncio.to_thread(testresults.collect, results_dir, clone)
        changed_cov, changed_lines = None, 0
        if base and res.lines:
            code_files = [
                f for f in gitops.changed_since(clone, base) if not roles.is_test_path(project, f)
            ]
            added = await asyncio.to_thread(testresults.added_lines, clone, base, code_files)
            changed_cov, changed_lines = testresults.changed_coverage(res.lines, added)
        run = store_results(
            task.project_id,
            self.task_id,
            self.run_id,
            step,
            gitops.head(clone),
            res,
            task_tests,
            changed_cov,
            changed_lines,
        )
        summary = f"{run.passed} passed, {run.failed + run.errors} failed"
        if run.coverage is not None:
            summary += f", {run.coverage:.0f}% coverage"
        if changed_cov is not None:
            summary += f", {changed_cov:.0f}% of changed lines"
        if res.problem:
            summary += f" ({res.problem})"
        self._emit("test", f"tests ({step}): {summary}", step=f"tests-{step}", test_run=run.id)
        return run, code

    async def _coverage_ok(self, project: Project) -> bool:
        """Changed-line coverage against the project minimum. Below it the task goes back to
        the Tester once, then waits for the user."""
        task = self._check()
        with session() as s:
            run = s.exec(
                select(TestRun)
                .where(TestRun.task_id == self.task_id, TestRun.step == "builder")
                .order_by(col(TestRun.id).desc())
            ).first()
        minimum = project.min_task_coverage
        cov = run.changed_coverage if run else None
        if minimum <= 0 or cov is None or cov >= minimum:
            return True
        why = f"tests cover {cov:.0f}% of the changed lines (minimum {minimum}%)"
        if task.coverage_rounds == 0:
            self._update_task(coverage_rounds=1, tests_ready=False)
            finish_run(self.run_id, RunState.SUCCEEDED, f"{why}; back to the Tester")
            self._set_run(failure_kind="coverage")
            transition(
                self.task_id,
                (TaskState.VALIDATING,),
                TaskState.QUEUED,
                f"{why}; back to the Tester",
            )
            return False
        transition(self.task_id, (TaskState.VALIDATING,), TaskState.AWAITING_MERGE, why)
        finish_run(self.run_id, RunState.SUCCEEDED, why)
        gates.open_gate(
            task.project_id,
            GateKind.COVERAGE,
            f"Task {task.key or self.task_id} is under-tested: {task.title}",
            f"The task's tests pass, but {why}, also after the Tester added more. Approve to "
            "merge it anyway, or reject to send it back to the Tester.",
            task_id=self.task_id,
        )
        return False

    def _previous_failure(self) -> str:
        with session() as s:
            prev = s.exec(
                select(TaskRun)
                .where(TaskRun.task_id == self.task_id, col(TaskRun.id) != self.run_id)
                .order_by(col(TaskRun.id).desc())
            ).first()
        if prev and prev.state in (RunState.FAILED, RunState.STOPPED) and prev.reason:
            return prev.reason
        return ""


def merge_task(task_id: int) -> None:
    """Merge a validated task into the milestone branch and mark it done.

    Used by the runner, and by the orchestrator when a task_merge checkpoint is
    approved. Raises GitError on conflict.
    """
    with session() as s:
        task = s.get(Task, task_id)
        assert task is not None
        project = s.get(Project, task.project_id)
        assert project is not None
    gitops.merge_task(task.project_id, task_id, task.title)
    emit(task.project_id, task_id, "system", f"merged into {project.milestone_branch}")
    if project.push_after_merge:
        try:
            gitops.push(task.project_id, project.repo_url, project.milestone_branch)
            emit(task.project_id, task_id, "system", f"pushed {project.milestone_branch}")
        except gitops.GitError as e:
            # The merge stands locally; a failed push must not fail the task.
            emit(task.project_id, task_id, "system", f"push failed: {e}")
    # Queue the plan review *before* marking the task done, so the scheduler never
    # starts the next task in between (replanning happens between tasks).
    from foreman.orchestrator.replan import queue_review

    queue_review(task.project_id, f"task {task.key or task_id} is done", task_id)
    transition(task_id, (TaskState.VALIDATING, TaskState.AWAITING_MERGE), TaskState.DONE)


def log_path(run_id: int, step: str) -> Path:
    return get_settings().runs_dir / str(run_id) / f"{step}.log"
