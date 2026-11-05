"""Database tables. Timestamps are UNIX epoch seconds (float)."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def now() -> float:
    return time.time()


class TaskState(StrEnum):
    # Created by decomposition; waits for plan approval before it can be scheduled.
    PROPOSED = "proposed"
    QUEUED = "queued"
    RUNNING = "running"
    VALIDATING = "validating"
    DONE = "done"
    FAILED = "failed"
    PAUSED = "paused"
    STOPPED_BY_USER = "stopped_by_user"
    DISCARDED = "discarded"
    # Validated, waiting for the user to approve the merge (task_merge checkpoint).
    AWAITING_MERGE = "awaiting_merge"
    # The escalation ladder ran out; waiting for the user (an inbox item explains why).
    BLOCKED = "blocked"
    # Replaced by smaller tasks.
    SPLIT = "split"


class RunState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


class ProjectPhase(StrEnum):
    SPECIFY = "specify"  # interactive spec / plan / validation with the planner
    DECOMPOSING = "decomposing"  # planner is breaking the plan into tasks
    PLAN_REVIEW = "plan_review"  # proposed tasks wait for approval
    EXECUTE = "execute"  # tasks run autonomously
    DONE = "done"  # every task done and the acceptance command passes


class JobKind(StrEnum):
    CHAT = "chat"
    DECOMPOSE = "decompose"
    SPLIT = "split"  # break a failing task into smaller ones
    REVIEW = "review"  # replan after a task completes or acceptance fails


class GateKind(StrEnum):
    PLAN_REVISION = "plan_revision"  # replanner changes needing approval
    SPEC_CHANGE = "spec_change"  # replanner wants the spec changed (always gated)
    TASK_MERGE = "task_merge"  # a validated task waits to be merged
    ESCALATION = "escalation"  # a task is blocked; retry or discard
    MILESTONE = "milestone"  # acceptance passed; approve to push / open the PR
    BUDGET = "budget"  # the project reached its spend cap; approve to raise it
    COVERAGE = "coverage"  # a task's changed lines are under-tested; merge anyway or send back


class GateState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"


class ControlAction(StrEnum):
    STOP = "stop"
    ESTOP = "estop"


class ControlScope(StrEnum):
    GLOBAL = "global"
    PROJECT = "project"
    TASK = "task"


class Project(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    repo_url: str
    default_branch: str = "main"
    milestone_branch: str = "foreman/m1"
    # Project-wide test command run after every task (regression guard). Optional.
    test_cmd: str = ""
    executor_kind: str = "pi"
    # Executor network: "isolated" (only the model gateway is reachable) or "open"
    # (LAN and internet, for tests that need them). See foreman.sandbox. Older rows
    # may say "gateway", the earlier name for "isolated".
    network: str = "isolated"
    # role -> {"node_id": int, "model": str}. Roles: see core/roles.py (project_planner,
    # task_planner, tester, builder, escalation; older projects: planner, executor).
    role_bindings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Optional checkpoints, e.g. {"plan_approval": true}. See core/gates.py.
    gates: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Watchdog limits and escalation policy overrides. See core/watchdog.py.
    watchdog: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Tasks in the approved plan, for the replanner's growth cap.
    baseline_tasks: int = 0
    # Times the acceptance command has failed after all tasks were done.
    acceptance_failures: int = 0
    # Commit the acceptance command last ran against, and how it went.
    acceptance_commit: str = ""
    acceptance_passed: bool = False
    # How to kill a running acceptance check (like TaskRun.handle).
    acceptance_handle: str = ""
    milestone_pr_url: str = ""
    phase: ProjectPhase = ProjectPhase.SPECIFY
    # Specs live in the project repo under specs/<feature>/.
    feature: str = "001-initial"
    # Runs the project's acceptance (validation) tests; defines "done" for the project.
    acceptance_cmd: str = ""
    spec_approved_at: float | None = None
    spec_commit: str = ""
    # Task size limits enforced at decomposition (FR-3.3).
    max_task_files: int = 3
    context_budget_pct: int = 35
    paused: bool = False
    # When `paused` last changed; lets a late Stop avoid undoing a newer Resume.
    paused_at: float = 0.0
    # Directories that hold tests: only the Tester writes there, and the Builder sees
    # them read-only.
    test_paths: list[str] = Field(default_factory=lambda: ["tests"], sa_column=Column(JSON))
    # Coverage the Builder's changed lines must reach (percent; 0 = report only).
    min_task_coverage: int = 80
    # Spend on priced (cloud) nodes, in USD, and the cap that gates it (0 = no cap).
    spend_usd: float = 0.0
    spend_cap_usd: float = 0.0
    push_after_merge: bool = False
    clone_error: str = ""
    created_at: float = Field(default_factory=now)


class Node(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    # "openai": any OpenAI-compatible endpoint (llama.cpp, LM Studio, Ollama, MLX, vLLM...)
    # "anthropic": the Claude API (planner/reviewer roles only; keys never reach executors)
    provider: str = "openai"
    # e.g. http://192.168.1.20:8080/v1, or https://api.anthropic.com for Claude
    base_url: str
    # Usable context window in tokens; sizes context packs.
    context_window: int = 32768
    api_key: str = ""
    capacity: int = 1
    models: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    healthy: bool | None = None
    last_checked: float | None = None
    # USD per million tokens; 0 for local nodes. Priced nodes count against spend caps.
    price_in: float = 0.0
    price_out: float = 0.0


class Task(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    title: str
    description: str = ""
    todos: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Files the task may change (fnmatch globs). Empty = unrestricted.
    allowed_globs: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    validation_cmd: str = ""
    deps: list[int] = Field(default_factory=list, sa_column=Column(JSON))
    # Stable name from decomposition (e.g. "T3"), used for dependencies in tasks.md.
    key: str = ""
    # setup | acceptance | feature | docs (see orchestrator/pipeline.py)
    kind: str = "feature"
    # Plain-language acceptance criteria for this task; the Tester turns them into tests.
    criteria: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Tester step done: its tests are committed in the task clone at tests_commit.
    tests_ready: bool = False
    tests_commit: str = ""
    # Times the task went back to the Tester for low coverage.
    coverage_rounds: int = 0
    # Requirement ids from spec.md this task implements (e.g. ["FR-2.1"]).
    req_refs: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Read-only files whose interfaces the task needs (signatures go in the context pack).
    context_files: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Why the task breaks the size limits, if it does (blocks plan approval).
    size_violations: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    pack_tokens: int = 0
    # Escalation ladder: 0 retrying, 1 split requested, 2 on the escalation model.
    escalation_level: int = 0
    # Failed attempts at the current escalation level.
    level_failures: int = 0
    # Set when created by splitting another task, or by the replanner.
    parent_id: int | None = None
    origin: str = "plan"  # plan | manual | split | replan
    # Overrides of the project defaults.
    executor_kind: str = ""
    node_id: int | None = None
    model: str = ""
    # For the "shell" executor: the script to run (testing / scripted tasks).
    executor_script: str = ""
    timeout_s: int = 1800
    state: TaskState = TaskState.QUEUED
    # Task-level pause: don't start; if running, park after the current step.
    hold: bool = False
    reason: str = ""
    attempts: int = 0
    created_at: float = Field(default_factory=now)
    updated_at: float = Field(default_factory=now)


class TaskRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="task.id", index=True)
    project_id: int = Field(index=True)
    node_id: int | None = None
    model: str = ""
    # How to kill it: "docker:<container name>" or "local:<pgid>".
    handle: str = ""
    # Secret path component for this run's model gateway URL.
    gateway_token: str = ""
    model_requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    # Why the watchdog stopped this run (empty = it didn't).
    watchdog: str = ""
    # Normalised hash of the failure output, to detect the same failure repeating.
    error_sig: str = ""
    # config | watchdog | executor | validation | merge | internal | budget | coverage
    failure_kind: str = ""
    # Which agent this run was: tester | builder ("" = a single-step scripted task).
    step: str = ""
    diff_lines: int = 0
    state: RunState = RunState.RUNNING
    exit_code: int | None = None
    reason: str = ""
    started_at: float = Field(default_factory=now)
    ended_at: float | None = None


class TestRun(SQLModel, table=True):
    """One run of a project's test suite, with its results and coverage."""

    __test__ = False  # not a pytest class
    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    task_id: int | None = Field(default=None, index=True)
    run_id: int | None = None
    # tester (tests written, expected to fail) | builder | acceptance
    step: str = ""
    commit: str = ""
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    duration_s: float = 0.0
    # Line coverage of the whole project, and of the lines the Builder changed (percent).
    coverage: float | None = None
    changed_coverage: float | None = None
    changed_lines: int = 0
    # {file: {"covered": n, "total": n}}
    files: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Why results are missing or incomplete (e.g. no junit.xml), else "".
    problem: str = ""
    created_at: float = Field(default_factory=now)


class TestCase(SQLModel, table=True):
    __test__ = False
    id: int | None = Field(default=None, primary_key=True)
    test_run_id: int = Field(foreign_key="testrun.id", index=True)
    name: str
    classname: str = ""
    file: str = ""
    # passed | failed | error | skipped
    status: str = "passed"
    duration_s: float = 0.0
    message: str = ""
    # Written by the Tester for this run's task.
    task_test: bool = False


class Event(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    project_id: int | None = Field(default=None, index=True)
    task_id: int | None = Field(default=None, index=True)
    run_id: int | None = None
    # log | state | test | diff | control | system
    kind: str
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    ts: float = Field(default_factory=now)


class Control(SQLModel, table=True):
    """Stop requests. Lives in control.db: written by the API, executed by the supervisor."""

    id: int | None = Field(default=None, primary_key=True)
    scope: ControlScope
    target_id: int | None = None
    action: ControlAction
    created_at: float = Field(default_factory=now)
    acked_at: float | None = None
    result: str = ""


class SystemFlag(SQLModel, table=True):
    """Global flags (E-stop). Lives in control.db."""

    key: str = Field(primary_key=True)
    value: str = ""
    updated_at: float = Field(default_factory=now)


class AppSetting(SQLModel, table=True):
    """Global settings edited from the dashboard (e.g. the GitHub token). Lives in foreman.db."""

    key: str = Field(primary_key=True)
    value: str = ""
    updated_at: float = Field(default_factory=now)


class SpecMessage(SQLModel, table=True):
    """One turn of the Phase 1 conversation with the planner."""

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    role: str  # "user" | "assistant"
    content: str
    # Files the planner wrote in this turn (assistant turns only).
    files: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    job_id: int | None = None
    ts: float = Field(default_factory=now)


class PlannerJob(SQLModel, table=True):
    """A planner model call, run by the orchestrator (chat turn or decomposition)."""

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    kind: JobKind
    state: JobState = JobState.QUEUED
    # The task a split job breaks up, or the task whose completion triggered a review.
    task_id: int | None = None
    # Extra input, e.g. {"reason": "acceptance failed", "output": "..."} for reviews.
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    node_id: int | None = None
    model: str = ""
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    created_at: float = Field(default_factory=now)
    started_at: float | None = None
    ended_at: float | None = None


class Gate(SQLModel, table=True):
    """A checkpoint waiting for the user: the "Needs you" inbox.

    The API only records the decision; the orchestrator acts on it (sets
    handled_at), so a single process ever writes to the repository.
    """

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    task_id: int | None = None
    kind: GateKind
    title: str
    detail: str = ""
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    state: GateState = GateState.PENDING
    note: str = ""
    created_at: float = Field(default_factory=now)
    resolved_at: float | None = None
    handled_at: float | None = None


class RevisionState(StrEnum):
    APPLIED = "applied"
    PENDING = "pending"  # waiting for approval (gate)
    REJECTED = "rejected"
    EMPTY = "empty"  # the reviewer found nothing to change


class PlanRevision(SQLModel, table=True):
    """One replanning step: what the reviewer changed in the task list, and why."""

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    version: int
    trigger: str  # "task T3 done" | "acceptance failed"
    summary: str = ""
    # [{"op": "add"|"remove"|"modify", "key": ..., "task": {...}, "changes": {...},
    #   "reason": ...}]
    ops: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    spec_change: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    state: RevisionState = RevisionState.APPLIED
    gate_id: int | None = None
    created_at: float = Field(default_factory=now)
