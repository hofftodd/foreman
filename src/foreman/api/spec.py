"""Phase 1 (specify) and plan review endpoints.

Lifecycle of a project:

    specify --approve spec--> decomposing --(planner)--> plan_review --approve plan--> execute
       ^                                                      |
       +------------------------- reopen ----------------------+

The documents live in the project repository under specs/<feature>/ and every
change is a commit on the milestone branch. Planner calls run as jobs in the
orchestrator; these endpoints only queue them.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete
from sqlmodel import col, select

from foreman import gitops
from foreman.api.schemas import ChatMessage, DocUpdate
from foreman.core import roles
from foreman.core.contextpack import build_pack, repo_files, size_violations
from foreman.core.models import (
    JobKind,
    JobState,
    Node,
    PlannerJob,
    PlanRevision,
    Project,
    ProjectPhase,
    SpecMessage,
    Task,
    TaskState,
    now,
)
from foreman.core.state import emit
from foreman.db import session
from foreman.planner import DOC_NAMES, MAX_CRITERIA, doc_path

router = APIRouter()

EDITABLE_PHASES = (ProjectPhase.SPECIFY, ProjectPhase.PLAN_REVIEW)


def _project(project_id: int) -> Project:
    with session() as s:
        p = s.get(Project, project_id)
    if p is None:
        raise HTTPException(404, "project not found")
    return p


def _active_job(project_id: int) -> PlannerJob | None:
    with session() as s:
        return s.exec(
            select(PlannerJob).where(
                PlannerJob.project_id == project_id,
                col(PlannerJob.state).in_((JobState.QUEUED, JobState.RUNNING)),
            )
        ).first()


def _require_idle(project_id: int) -> None:
    if _active_job(project_id) is not None:
        raise HTTPException(409, "the planner is busy; wait for it or stop the project")


def _queue_job(project_id: int, kind: JobKind) -> PlannerJob:
    job = PlannerJob(project_id=project_id, kind=kind)
    with session() as s:
        s.add(job)
        s.commit()
        s.refresh(job)
    return job


def _set_phase(project: Project, phase: ProjectPhase, **fields: Any) -> None:
    with session() as s:
        p = s.get(Project, project.id)
        assert p is not None
        p.phase = phase
        for k, v in fields.items():
            setattr(p, k, v)
        s.add(p)
        s.commit()
    emit(project.id, None, "state", f"phase: {phase.value}", phase=phase.value)


def recheck_task_size(task_id: int) -> None:
    """Recompute a proposed task's size violations (after an edit by the user)."""
    with session() as s:
        t = s.get(Task, task_id)
        assert t is not None
        p = s.get(Project, t.project_id)
        assert p is not None
        binding = roles.binding(p, roles.BUILDER)
        node = s.get(Node, binding["node_id"]) if binding.get("node_id") else None
    if not t.validation_cmd and not t.allowed_globs:
        # A planned task (plain language): its size is its criteria.
        problems = []
        if not t.criteria:
            problems.append("needs at least one acceptance criterion")
        elif len(t.criteria) > MAX_CRITERIA:
            problems.append(f"{len(t.criteria)} criteria (limit {MAX_CRITERIA}); split it")
        with session() as s:
            row = s.get(Task, task_id)
            assert row is not None
            row.size_violations = problems
            s.add(row)
            s.commit()
        return
    repo = gitops.repo_dir(t.project_id)
    if not (repo / ".git").exists():
        return
    pack = build_pack(p, t, repo, context_window=node.context_window if node else None)
    violations = size_violations(p, t, pack, repo_files(repo))
    with session() as s:
        row = s.get(Task, task_id)
        assert row is not None
        row.size_violations, row.pack_tokens = violations, pack.tokens
        s.add(row)
        s.commit()


# -- read ------------------------------------------------------------------------


@router.get("/api/projects/{project_id}/spec")
def get_spec(project_id: int) -> dict[str, Any]:
    p = _project(project_id)
    docs = {name: gitops.read_repo_file(project_id, doc_path(p, name)) for name in DOC_NAMES}
    with session() as s:
        messages = s.exec(
            select(SpecMessage)
            .where(SpecMessage.project_id == project_id)
            .order_by(col(SpecMessage.id))
        ).all()
        last_job = s.exec(
            select(PlannerJob)
            .where(PlannerJob.project_id == project_id)
            .order_by(col(PlannerJob.id).desc())
        ).first()
    active = _active_job(project_id)
    return {
        "phase": p.phase.value,
        "feature": p.feature,
        "acceptance_cmd": p.acceptance_cmd,
        "spec_approved_at": p.spec_approved_at,
        "docs": docs,
        "paths": {name: doc_path(p, name) for name in DOC_NAMES},
        "messages": [m.model_dump() for m in messages],
        "active_job": active.model_dump() if active else None,
        "last_job": last_job.model_dump() if last_job else None,
    }


@router.get("/api/projects/{project_id}/revisions")
def list_revisions(project_id: int) -> list[PlanRevision]:
    _project(project_id)
    with session() as s:
        return list(
            s.exec(
                select(PlanRevision)
                .where(PlanRevision.project_id == project_id)
                .order_by(col(PlanRevision.id).desc())
            ).all()
        )


@router.get("/api/projects/{project_id}/jobs")
def list_jobs(project_id: int) -> list[PlannerJob]:
    _project(project_id)
    with session() as s:
        return list(
            s.exec(
                select(PlannerJob)
                .where(PlannerJob.project_id == project_id)
                .order_by(col(PlannerJob.id).desc())
            ).all()
        )


# -- specify ------------------------------------------------------------------------


@router.put("/api/projects/{project_id}/spec/docs/{name}")
async def put_doc(project_id: int, name: str, body: DocUpdate) -> dict[str, Any]:
    p = _project(project_id)
    if name not in roles.PLANNING_DOCS:
        raise HTTPException(400, f"only {', '.join(roles.PLANNING_DOCS)} can be edited")
    if p.phase not in EDITABLE_PHASES:
        raise HTTPException(409, f"documents are frozen in phase {p.phase.value}")
    _require_idle(project_id)
    sha = await run_in_threadpool(
        gitops.write_and_commit,
        project_id,
        {doc_path(p, name): body.content},
        f"spec: edit {doc_path(p, name).rsplit('/', 1)[-1]}",
    )
    emit(project_id, None, "system", f"{doc_path(p, name)} edited by user", doc=name)
    return {"ok": True, "commit": sha}


@router.post("/api/projects/{project_id}/spec/messages", status_code=201)
def post_message(project_id: int, body: ChatMessage) -> dict[str, Any]:
    p = _project(project_id)
    if p.phase != ProjectPhase.SPECIFY:
        raise HTTPException(409, "the spec conversation is closed; reopen the spec first")
    _require_idle(project_id)
    with session() as s:
        msg = SpecMessage(project_id=project_id, role="user", content=body.content)
        s.add(msg)
        s.commit()
        s.refresh(msg)
    job = _queue_job(project_id, JobKind.CHAT)
    return {"message": msg.model_dump(), "job": job.model_dump()}


@router.post("/api/projects/{project_id}/spec/approve")
def approve_spec(project_id: int) -> dict[str, Any]:
    """Phase 1 gate (FR-2.4, always on): freeze the spec and start decomposition."""
    p = _project(project_id)
    if p.phase != ProjectPhase.SPECIFY:
        raise HTTPException(409, f"cannot approve the spec in phase {p.phase.value}")
    _require_idle(project_id)
    missing = [
        n
        for n in roles.PLANNING_DOCS
        if not gitops.read_repo_file(project_id, doc_path(p, n)).strip()
    ]
    if missing:
        raise HTTPException(400, f"missing documents: {', '.join(missing)}")
    _set_phase(
        p,
        ProjectPhase.DECOMPOSING,
        spec_approved_at=now(),
        spec_commit=gitops.head_commit(project_id),
    )
    emit(project_id, None, "gate", "spec approved", gate="spec_approval")
    job = _queue_job(project_id, JobKind.DECOMPOSE)
    return {"ok": True, "job": job.model_dump()}


# -- plan review ------------------------------------------------------------------------


def _drop_proposed(project_id: int) -> None:
    with session() as s:
        s.exec(  # type: ignore[call-overload]
            delete(Task).where(
                col(Task.project_id) == project_id, col(Task.state) == TaskState.PROPOSED
            )
        )
        s.commit()


@router.post("/api/projects/{project_id}/plan/redecompose")
def redecompose(project_id: int) -> dict[str, Any]:
    p = _project(project_id)
    if p.phase != ProjectPhase.PLAN_REVIEW:
        raise HTTPException(409, f"nothing to redo in phase {p.phase.value}")
    _require_idle(project_id)
    _drop_proposed(project_id)
    _set_phase(p, ProjectPhase.DECOMPOSING)
    return {"ok": True, "job": _queue_job(project_id, JobKind.DECOMPOSE).model_dump()}


@router.post("/api/projects/{project_id}/spec/reopen")
def reopen_spec(project_id: int) -> dict[str, Any]:
    """Back to the conversation: proposed tasks are dropped."""
    p = _project(project_id)
    if p.phase != ProjectPhase.PLAN_REVIEW:
        raise HTTPException(409, f"cannot reopen the spec in phase {p.phase.value}")
    _require_idle(project_id)
    _drop_proposed(project_id)
    _set_phase(p, ProjectPhase.SPECIFY, spec_approved_at=None)
    return {"ok": True}


@router.post("/api/projects/{project_id}/plan/approve")
def approve_plan(project_id: int) -> dict[str, Any]:
    """Plan gate: proposed tasks become queued and the project starts executing."""
    p = _project(project_id)
    if p.phase != ProjectPhase.PLAN_REVIEW:
        raise HTTPException(409, f"cannot approve the plan in phase {p.phase.value}")
    with session() as s:
        proposed = s.exec(
            select(Task).where(Task.project_id == project_id, Task.state == TaskState.PROPOSED)
        ).all()
    if not proposed:
        raise HTTPException(400, "there are no proposed tasks")
    ids = {t.id for t in proposed}
    orphaned = [t.key or str(t.id) for t in proposed if any(d not in ids for d in t.deps)]
    if orphaned:
        raise HTTPException(
            400, f"tasks depend on dropped tasks: {', '.join(orphaned)}; edit or drop them"
        )
    too_big = [t.key or str(t.id) for t in proposed if t.size_violations]
    if too_big:
        raise HTTPException(
            400, f"tasks exceed the size limits: {', '.join(too_big)}; edit or split them"
        )
    with session() as s:
        for t in proposed:
            row = s.get(Task, t.id)
            assert row is not None
            row.state = TaskState.QUEUED
            row.updated_at = now()
            s.add(row)
        s.commit()
    _set_phase(p, ProjectPhase.EXECUTE, baseline_tasks=len(proposed))
    emit(
        project_id,
        None,
        "gate",
        f"plan approved: {len(proposed)} tasks queued",
        gate="plan_approval",
    )
    return {"ok": True, "queued": len(proposed)}
