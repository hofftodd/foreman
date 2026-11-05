"""Cloud spend: what priced nodes cost each project, and the cap that gates it.

Local nodes are free (price 0) and never count. A node with a price (USD per
million input/output tokens) charges the project for every call made through
it: planner jobs and, when an executor runs on it, the model gateway.

When a project with a cap (``spend_cap_usd`` > 0) reaches it, a budget
checkpoint opens - always, whatever the gate settings - and paid calls are
refused until the cap is raised: approving the checkpoint extends the cap by
its original amount; rejecting it pauses the project. Raising the cap in the
project settings clears the checkpoint too.
"""

from __future__ import annotations

from sqlalchemy import update
from sqlmodel import col

from foreman.core import gates
from foreman.core.models import GateKind, Node, Project
from foreman.core.state import emit
from foreman.db import session


def priced(node: Node | None) -> bool:
    return node is not None and (node.price_in > 0 or node.price_out > 0)


def cost(node: Node, tokens_in: int, tokens_out: int) -> float:
    return (tokens_in * node.price_in + tokens_out * node.price_out) / 1_000_000


def over_cap(project: Project) -> bool:
    return project.spend_cap_usd > 0 and project.spend_usd >= project.spend_cap_usd


def blocked(project_id: int, node: Node | None) -> str:
    """Why a paid call for this project must not be made now, or ""."""
    if not priced(node):
        return ""
    with session() as s:
        p = s.get(Project, project_id)
    if p is None or not over_cap(p):
        return ""
    return f"spend cap reached (${p.spend_usd:.2f} of ${p.spend_cap_usd:.2f})"


def charge(project_id: int, node: Node | None, tokens_in: int, tokens_out: int) -> float:
    """Add a call's cost to the project; open the budget checkpoint on reaching the cap."""
    if node is None or not priced(node):
        return 0.0
    amount = cost(node, tokens_in, tokens_out)
    if amount <= 0:
        return 0.0
    with session() as s:
        s.exec(  # type: ignore[call-overload]
            update(Project)
            .where(col(Project.id) == project_id)
            .values(spend_usd=col(Project.spend_usd) + amount)
        )
        s.commit()
        p = s.get(Project, project_id)
    if p is not None and over_cap(p):
        check_cap(p)
    return amount


def check_cap(p: Project) -> None:
    assert p.id is not None
    if not over_cap(p) or gates.pending(p.id, GateKind.BUDGET):
        return
    emit(p.id, None, "system", f"spend cap reached: ${p.spend_usd:.2f} of ${p.spend_cap_usd:.2f}")
    gates.open_gate(
        p.id,
        GateKind.BUDGET,
        f"Spend cap of ${p.spend_cap_usd:.2f} reached",
        f"Spent ${p.spend_usd:.2f} on priced models. Paid calls are paused. Approve to "
        f"allow another ${p.spend_cap_usd:.2f}; reject to pause the project. You can "
        "also set a new cap in the project settings.",
        payload={"cap": p.spend_cap_usd},
    )


def cap_changed(p: Project) -> None:
    """After the user edits the cap: clear a stale checkpoint, or open one."""
    assert p.id is not None
    if over_cap(p):
        check_cap(p)
        return
    for g in gates.pending(p.id, GateKind.BUDGET):
        assert g.id is not None
        gates.cancel(g.id, f"cap raised to ${p.spend_cap_usd:.2f}")


def raise_cap(project_id: int, step: float) -> None:
    """Budget checkpoint approved: extend the cap by its original amount."""
    with session() as s:
        p = s.get(Project, project_id)
        if p is None:
            return
        p.spend_cap_usd = max(p.spend_cap_usd, p.spend_usd) + max(step, 0.01)
        s.add(p)
        s.commit()
        s.refresh(p)
    emit(project_id, None, "system", f"spend cap raised to ${p.spend_cap_usd:.2f}")
