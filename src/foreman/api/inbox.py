"""The "Needs you" inbox: checkpoints waiting for a decision."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import col, select

from foreman.core import gates
from foreman.core.models import Gate, GateState, Project, Task
from foreman.db import session

router = APIRouter()


class Decision(BaseModel):
    approve: bool
    note: str = ""
    # For a blocked task: "send_back" asks the Tester to write its tests again.
    action: str = ""


def _out(g: Gate, names: dict[int, str], titles: dict[int, str]) -> dict[str, Any]:
    return {
        **g.model_dump(),
        "project_name": names.get(g.project_id, ""),
        "task_title": titles.get(g.task_id, "") if g.task_id else "",
    }


@router.get("/api/gates")
def list_gates(project_id: int | None = None, state: str = "pending") -> list[dict[str, Any]]:
    q = select(Gate)
    if state != "all":
        q = q.where(Gate.state == GateState(state))
    if project_id is not None:
        q = q.where(Gate.project_id == project_id)
    with session() as s:
        rows = s.exec(q.order_by(col(Gate.id).desc()).limit(200)).all()
        names = {p.id: p.name for p in s.exec(select(Project)) if p.id is not None}
        task_ids = [g.task_id for g in rows if g.task_id]
        titles = {
            t.id: t.title
            for t in s.exec(select(Task).where(col(Task.id).in_(task_ids)))
            if t.id is not None
        }
    return [_out(g, names, titles) for g in rows]


@router.post("/api/gates/{gate_id}/resolve")
def resolve_gate(gate_id: int, body: Decision) -> Gate:
    if body.action and body.action != "send_back":
        raise HTTPException(400, f"unknown action {body.action!r}")
    gate = gates.resolve(gate_id, body.approve, body.note, action=body.action)
    if gate is None:
        raise HTTPException(409, "this checkpoint is not waiting for a decision")
    return gate
