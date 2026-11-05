"""What happens after a task attempt fails (FR-5.5).

    retry with a fresh agent session  (up to max_retries)
      -> split into smaller tasks     (planner; once per task, not for split children)
      -> the escalation model         (role binding "escalation"; up to max_retries)
      -> blocked: an inbox item lets the user retry or discard

A configuration problem blocks immediately. A context pack that is too big
goes straight to splitting. The same failure signature `repeat_limit` times in
a row ends the retries at the current level early.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import col, select

from foreman.core import gates, roles, watchdog
from foreman.core.models import (
    GateKind,
    JobKind,
    PlannerJob,
    Project,
    RunState,
    Task,
    TaskRun,
    TaskState,
    now,
)
from foreman.core.state import emit, transition
from foreman.db import session

LEVEL_RETRY, LEVEL_SPLIT, LEVEL_ESCALATED = 0, 1, 2
MAX_SPLIT_DEPTH = 2


@dataclass
class Decision:
    action: str  # retry | split | escalate | block
    reason: str


def split_depth(task: Task) -> int:
    depth, parent = 0, task.parent_id
    with session() as s:
        while parent is not None and depth < 10:
            t = s.get(Task, parent)
            if t is None:
                break
            depth += 1
            parent = t.parent_id
    return depth


def recent_signatures(task_id: int, n: int) -> list[str]:
    with session() as s:
        runs = s.exec(
            select(TaskRun)
            .where(TaskRun.task_id == task_id, TaskRun.state == RunState.FAILED)
            .order_by(col(TaskRun.id).desc())
            .limit(n)
        ).all()
    return [r.error_sig for r in reversed(runs)]


def decide(project: Project, task: Task, run: TaskRun) -> Decision:
    """Pick the next rung of the ladder. `task.level_failures` already counts `run`."""
    max_retries = watchdog.limit(project, "max_retries")
    repeat = watchdog.limit(project, "repeat_limit")
    assert task.id is not None
    looping = watchdog.repeated(recent_signatures(task.id, repeat), repeat)
    has_escalation = bool(((project.role_bindings or {}).get("escalation") or {}).get("node_id"))
    can_split = (
        watchdog.limit(project, "split")
        and task.escalation_level < LEVEL_SPLIT
        and split_depth(task) < MAX_SPLIT_DEPTH
        and bool(roles.binding(project, roles.TASK_PLANNER))
    )

    if run.failure_kind == "config":
        return Decision("block", f"configuration problem: {run.reason}")
    if run.failure_kind == "too_big" and can_split:
        return Decision("split", "the task does not fit the executor's context")

    in_retry_budget = task.level_failures <= max_retries and not looping
    if task.escalation_level in (LEVEL_RETRY, LEVEL_ESCALATED) and in_retry_budget:
        return Decision("retry", f"retry {task.level_failures}/{max_retries}")
    retries = "the retry" if max_retries == 1 else f"{max_retries} retries"
    why = "the same failure keeps repeating" if looping else f"{retries} failed"
    if max_retries == 0 and not looping:
        why = "the attempt failed (no retries configured)"
    if can_split:
        return Decision("split", why)
    if has_escalation and task.escalation_level < LEVEL_ESCALATED:
        return Decision("escalate", why)
    return Decision("block", why)


def after_failure(task_id: int, run_id: int) -> Decision | None:
    """Apply the ladder to a task whose attempt just failed."""
    with session() as s:
        task = s.get(Task, task_id)
        run = s.get(TaskRun, run_id)
        if task is None or run is None or task.state != TaskState.FAILED:
            return None
        project = s.get(Project, task.project_id)
        assert project is not None
        if run.failure_kind == "budget":
            # Not the task's fault: it runs again once the spend cap is raised.
            d = Decision("retry", "waiting for the spend cap to be raised")
            emit(project.id, task_id, "watchdog", f"escalation: retry ({d.reason})", action="retry")
            transition(task_id, (TaskState.FAILED,), TaskState.QUEUED, d.reason)
            return d
        if not watchdog.limit(project, "escalation"):
            return None
        task.level_failures += 1
        s.add(task)
        s.commit()
        s.refresh(task)
    decision = decide(project, task, run)
    apply(project, task, run, decision)
    return decision


def apply(project: Project, task: Task, run: TaskRun, d: Decision) -> None:
    assert task.id is not None and project.id is not None
    emit(project.id, task.id, "watchdog", f"escalation: {d.action} ({d.reason})", action=d.action)
    if d.action == "retry":
        transition(task.id, (TaskState.FAILED,), TaskState.QUEUED, f"{d.reason}: {run.reason}")
    elif d.action == "split":
        with session() as s:
            t = s.get(Task, task.id)
            assert t is not None
            t.escalation_level, t.level_failures = LEVEL_SPLIT, 0
            s.add(t)
            s.add(
                PlannerJob(
                    project_id=project.id,
                    kind=JobKind.SPLIT,
                    task_id=task.id,
                    payload={"reason": run.reason, "why": d.reason},
                )
            )
            s.commit()
    elif d.action == "escalate":
        binding = (project.role_bindings or {}).get("escalation") or {}
        with session() as s:
            t = s.get(Task, task.id)
            assert t is not None
            t.escalation_level, t.level_failures = LEVEL_ESCALATED, 0
            t.node_id, t.model = binding.get("node_id"), binding.get("model", "")
            t.updated_at = now()
            s.add(t)
            s.commit()
        transition(
            task.id,
            (TaskState.FAILED,),
            TaskState.QUEUED,
            f"escalated to {binding.get('model') or 'the escalation model'}: {d.reason}",
        )
    else:
        block(task, f"{d.reason}. Last failure: {run.reason}")


def block(task: Task, why: str) -> None:
    assert task.id is not None
    if transition(task.id, (TaskState.FAILED,), TaskState.BLOCKED, why):
        gates.open_gate(
            task.project_id,
            GateKind.ESCALATION,
            f"Task {task.key or task.id} is stuck: {task.title}",
            f"{why}\n\nApprove to try again (fresh attempts), or reject to discard the task.",
            task_id=task.id,
        )


def split_failed(task_id: int, why: str) -> None:
    """The planner could not split the task: continue down the ladder."""
    with session() as s:
        task = s.get(Task, task_id)
        if task is None or task.state not in (TaskState.FAILED, TaskState.BLOCKED):
            return
        project = s.get(Project, task.project_id)
        assert project is not None
    has_escalation = bool(((project.role_bindings or {}).get("escalation") or {}).get("node_id"))
    run = TaskRun(task_id=task_id, project_id=task.project_id, reason=f"split failed: {why}")
    if has_escalation and task.escalation_level < LEVEL_ESCALATED:
        apply(project, task, run, Decision("escalate", f"could not split: {why}"))
    else:
        block(task, f"could not split it: {why}")


def resolve_blocked(task_id: int, approve: bool) -> None:
    """The user's answer to an escalation gate."""
    if approve:
        with session() as s:
            task = s.get(Task, task_id)
            if task is not None:
                task.level_failures = 0
                s.add(task)
                s.commit()
        transition(task_id, (TaskState.BLOCKED,), TaskState.QUEUED, "retry requested by user")
    else:
        transition(task_id, (TaskState.BLOCKED,), TaskState.DISCARDED, "discarded by user")
