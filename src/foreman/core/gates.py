"""Checkpoints ("gates") and the "Needs you" inbox (FR-7).

A project's ``gates`` setting turns the optional checkpoints on or off:

    plan_approval   review the task breakdown before it runs        (default on)
    replan_approval approve every plan revision from the replanner  (default off)
    task_merge      approve each validated task before it merges    (default off)
    milestone       approve before the milestone is pushed / PR'd   (default on)

Always on, whatever the settings: spec approval, spec changes proposed by the
replanner, plan growth beyond the cap, and blocked tasks.

Opening a gate records it, emits an event and (if FOREMAN_NTFY_URL is set)
sends a push notification. Resolving one only records the decision; the
orchestrator carries it out.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import httpx
from sqlmodel import col, select

from foreman.config import get_settings
from foreman.core.models import Gate, GateKind, GateState, Project, now
from foreman.core.state import emit
from foreman.db import session

log = logging.getLogger("foreman.gates")

DEFAULTS: dict[str, bool] = {
    "plan_approval": True,
    "replan_approval": False,
    "task_merge": False,
    "milestone": True,
}


def enabled(project: Project, name: str) -> bool:
    return bool((project.gates or {}).get(name, DEFAULTS.get(name, False)))


def open_gate(
    project_id: int,
    kind: GateKind,
    title: str,
    detail: str = "",
    task_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> Gate:
    gate = Gate(
        project_id=project_id,
        task_id=task_id,
        kind=kind,
        title=title,
        detail=detail,
        payload=payload or {},
    )
    with session() as s:
        s.add(gate)
        s.commit()
        s.refresh(gate)
    emit(project_id, task_id, "gate", f"needs you: {title}", gate_id=gate.id, gate=kind.value)
    notify(project_id, title, detail)
    return gate


def pending(
    project_id: int | None = None, kind: GateKind | None = None, task_id: int | None = None
) -> list[Gate]:
    q = select(Gate).where(Gate.state == GateState.PENDING)
    if project_id is not None:
        q = q.where(Gate.project_id == project_id)
    if kind is not None:
        q = q.where(Gate.kind == kind)
    if task_id is not None:
        q = q.where(Gate.task_id == task_id)
    with session() as s:
        return list(s.exec(q.order_by(col(Gate.id))).all())


def resolve(gate_id: int, approve: bool, note: str = "", action: str = "") -> Gate | None:
    """Record the user's decision (API side). Returns None if not pending."""
    with session() as s:
        gate = s.get(Gate, gate_id)
        if gate is None or gate.state != GateState.PENDING:
            return None
        if action:
            gate.payload = {**(gate.payload or {}), "action": action}
        gate.state = GateState.APPROVED if approve else GateState.REJECTED
        gate.note, gate.resolved_at = note, now()
        s.add(gate)
        s.commit()
        s.refresh(gate)
    emit(
        gate.project_id,
        gate.task_id,
        "gate",
        f"{'approved' if approve else 'rejected'}: {gate.title}",
        gate_id=gate.id,
        gate=gate.kind.value,
        decision=gate.state.value,
    )
    return gate


def cancel(gate_id: int, note: str) -> None:
    with session() as s:
        gate = s.get(Gate, gate_id)
        if gate is not None and gate.state == GateState.PENDING:
            gate.state, gate.note = GateState.CANCELLED, note
            gate.resolved_at = gate.handled_at = now()
            s.add(gate)
            s.commit()


def unhandled() -> list[Gate]:
    """Decided gates the orchestrator still has to act on."""
    with session() as s:
        return list(
            s.exec(
                select(Gate)
                .where(
                    col(Gate.state).in_((GateState.APPROVED, GateState.REJECTED)),
                    col(Gate.handled_at).is_(None),
                )
                .order_by(col(Gate.id))
            ).all()
        )


def mark_handled(gate_id: int) -> None:
    with session() as s:
        gate = s.get(Gate, gate_id)
        if gate is not None:
            gate.handled_at = now()
            s.add(gate)
            s.commit()


def notify(project_id: int, title: str, detail: str = "") -> None:
    """Push notification via an ntfy-compatible endpoint, in the background."""
    settings = get_settings()
    url = settings.ntfy_url
    if not url:
        return
    with session() as s:
        project = s.get(Project, project_id)
    headers = {
        "Title": f"foreman: {project.name if project else project_id}",
        "Tags": "construction_worker",
    }
    if settings.public_url:
        headers["Click"] = f"{settings.public_url.rstrip('/')}/inbox"

    def send() -> None:
        try:
            httpx.post(
                url, content=f"{title}\n{detail}".strip().encode(), headers=headers, timeout=10
            )
        except httpx.HTTPError as e:
            log.warning("notification failed: %s", e)

    threading.Thread(target=send, daemon=True).start()
