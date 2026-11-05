"""State transitions and shared bookkeeping used by all three processes.

Task state changes go through `transition`, a compare-and-set UPDATE, so the
orchestrator never overwrites a state the supervisor set (e.g. a Stop that
lands while a run is finishing).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from sqlalchemy import update
from sqlmodel import col, select

from foreman.config import get_settings
from foreman.core.models import (
    AppSetting,
    Control,
    ControlAction,
    ControlScope,
    Event,
    RunState,
    SystemFlag,
    Task,
    TaskRun,
    TaskState,
    now,
)
from foreman.db import control_session, session

ESTOP_FLAG = "estop"

# Allowed edges. Anything else is a bug.
TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.PROPOSED: {TaskState.QUEUED, TaskState.DISCARDED},
    TaskState.QUEUED: {TaskState.RUNNING, TaskState.DISCARDED, TaskState.STOPPED_BY_USER},
    TaskState.RUNNING: {
        TaskState.VALIDATING,
        TaskState.FAILED,
        TaskState.PAUSED,
        TaskState.STOPPED_BY_USER,
    },
    TaskState.VALIDATING: {
        TaskState.DONE,
        TaskState.FAILED,
        TaskState.PAUSED,
        TaskState.STOPPED_BY_USER,
        TaskState.AWAITING_MERGE,
        TaskState.QUEUED,  # coverage too low: back to the Tester
    },
    TaskState.AWAITING_MERGE: {
        TaskState.DONE,
        TaskState.FAILED,
        TaskState.STOPPED_BY_USER,
        TaskState.QUEUED,  # coverage checkpoint rejected: back to the Tester
    },
    TaskState.FAILED: {
        TaskState.QUEUED,
        TaskState.DISCARDED,
        TaskState.BLOCKED,
        TaskState.SPLIT,
    },
    TaskState.BLOCKED: {TaskState.QUEUED, TaskState.DISCARDED, TaskState.SPLIT},
    TaskState.SPLIT: set(),
    TaskState.PAUSED: {TaskState.QUEUED, TaskState.DISCARDED},
    TaskState.STOPPED_BY_USER: {TaskState.QUEUED, TaskState.DISCARDED},
    TaskState.DONE: set(),
    TaskState.DISCARDED: set(),
}

ACTIVE_STATES = (TaskState.RUNNING, TaskState.VALIDATING)


def can_transition(src: TaskState, dst: TaskState) -> bool:
    return dst in TRANSITIONS[src]


def transition(
    task_id: int, from_states: Iterable[TaskState], to: TaskState, reason: str = ""
) -> bool:
    """Move a task to `to` only if it is currently in one of `from_states`."""
    sources = [s for s in from_states if can_transition(s, to)]
    if not sources:
        raise ValueError(f"no legal transition from {list(from_states)} to {to}")
    with session() as s:
        res = s.exec(
            update(Task)
            .where(col(Task.id) == task_id, col(Task.state).in_(sources))
            .values(state=to, reason=reason, updated_at=now())
        )
        s.commit()
        changed = bool(res.rowcount)
        task = s.get(Task, task_id)
    if changed and task is not None:
        emit(
            task.project_id,
            task_id,
            "state",
            f"{to.value}" + (f": {reason}" if reason else ""),
            state=to.value,
        )
    return changed


def finish_run(
    run_id: int, state: RunState, reason: str = "", exit_code: int | None = None
) -> bool:
    """Close a run if it is still open. Returns False if someone closed it first."""
    with session() as s:
        res = s.exec(
            update(TaskRun)
            .where(col(TaskRun.id) == run_id, col(TaskRun.state) == RunState.RUNNING)
            .values(state=state, reason=reason, exit_code=exit_code, ended_at=now())
        )
        s.commit()
        return bool(res.rowcount)


def emit(
    project_id: int | None,
    task_id: int | None,
    kind: str,
    message: str = "",
    run_id: int | None = None,
    **data: Any,
) -> None:
    with session() as s:
        s.add(
            Event(
                project_id=project_id,
                task_id=task_id,
                run_id=run_id,
                kind=kind,
                message=message,
                data=data,
            )
        )
        s.commit()


def get_flag(key: str) -> str:
    with control_session() as s:
        flag = s.get(SystemFlag, key)
        return flag.value if flag else ""


def set_flag(key: str, value: str) -> None:
    with control_session() as s:
        flag = s.get(SystemFlag, key)
        if flag is None:
            flag = SystemFlag(key=key)
        flag.value = value
        flag.updated_at = now()
        s.add(flag)
        s.commit()


def get_app_setting(key: str) -> str:
    with session() as s:
        row = s.get(AppSetting, key)
        return row.value if row else ""


def set_app_setting(key: str, value: str) -> None:
    """Store a dashboard setting; an empty value removes it."""
    with session() as s:
        row = s.get(AppSetting, key)
        if not value:
            if row is not None:
                s.delete(row)
                s.commit()
            return
        if row is None:
            row = AppSetting(key=key)
        row.value = value
        row.updated_at = now()
        s.add(row)
        s.commit()


GITHUB_TOKEN_KEY = "github_token"


def github_token() -> tuple[str, str]:
    """The GitHub token and where it came from: one saved in the dashboard wins over
    FOREMAN_GITHUB_TOKEN. Used only by the orchestrator; never passed to executors."""
    saved = get_app_setting(GITHUB_TOKEN_KEY)
    if saved:
        return saved, "dashboard"
    env = get_settings().github_token
    return env, "environment" if env else ""


def estop_active() -> bool:
    return get_flag(ESTOP_FLAG) == "1"


def stop_requested(project_id: int, task_id: int, since: float) -> bool:
    """Has the user asked to stop this task (directly, via its project, or E-stop)
    since `since`? Reads control.db only, so it works however foreman.db is doing."""
    if estop_active():
        return True
    with control_session() as s:
        rows = s.exec(select(Control).where(col(Control.created_at) >= since)).all()
    return any(
        c.scope == ControlScope.GLOBAL
        or (c.scope == ControlScope.PROJECT and c.target_id == project_id)
        or (c.scope == ControlScope.TASK and c.target_id == task_id)
        for c in rows
        if c.action in (ControlAction.STOP, ControlAction.ESTOP)
    )


def pending_stops() -> tuple[set[int], set[int]]:
    """(project ids, task ids) with stop requests the supervisor has not finished."""
    with control_session() as s:
        rows = s.exec(select(Control).where(col(Control.acked_at).is_(None))).all()
    projects = {c.target_id for c in rows if c.scope == ControlScope.PROJECT and c.target_id}
    tasks = {c.target_id for c in rows if c.scope == ControlScope.TASK and c.target_id}
    return projects, tasks


# Heartbeats are plain files (mtime = last beat) so that recording liveness
# never contends for a database lock.


def heartbeat(component: str) -> None:
    d = get_settings().heartbeat_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / component).write_text(str(os.getpid()))


def heartbeats() -> dict[str, float]:
    d = get_settings().heartbeat_dir
    if not d.is_dir():
        return {}
    return {f.name: f.stat().st_mtime for f in d.iterdir() if f.is_file()}
