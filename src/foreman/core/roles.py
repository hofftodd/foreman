"""The agents and what each one may do.

Foreman runs four kinds of agent, each with a fixed list of capabilities. The
orchestrator (plain code, no model) is the only component that writes to the
repositories and the database, and it checks every agent action against this
registry before carrying it out. Anything outside an agent's list is refused and
recorded as a ``denied`` event, visible in the dashboard.

Planning agents have no tools at all: they answer in text, and Foreman parses
only the actions their role allows. Coding agents (Tester, Builder) work in a
sandbox whose mounts already make the forbidden files read-only; the same rules
are checked again on the result before anything is merged.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any

from foreman.core.models import Node, Project

PROJECT_PLANNER = "project_planner"
TASK_PLANNER = "task_planner"
TESTER = "tester"
BUILDER = "builder"
ESCALATION = "escalation"

# Planning documents, by key: human-readable markdown in specs/<feature>/.
DOCS = {
    "requirements": "requirements.md",
    "design": "design.md",
    "acceptance": "acceptance-criteria.md",
    "tasks": "tasks.md",
}
# Projects created before the role split keep their original file names.
LEGACY_DOCS = {"requirements": "spec.md", "design": "plan.md", "acceptance": "validation.md"}
PLANNING_DOCS = ("requirements", "design", "acceptance")


@dataclass(frozen=True)
class Role:
    key: str
    name: str
    summary: str
    may: tuple[str, ...]
    may_not: tuple[str, ...]
    # Binding keys tried in order (older projects used "planner" / "executor").
    bindings: tuple[str, ...]


ROLES: dict[str, Role] = {
    PROJECT_PLANNER: Role(
        PROJECT_PLANNER,
        "Project Planning Agent",
        "Works out the project with you and keeps the planning documents.",
        (
            "write the requirements, design and acceptance criteria (markdown)",
            "talk with the user",
            "research topics on the web (search and read public pages)",
        ),
        ("write code, tests or commands", "create or change tasks"),
        (PROJECT_PLANNER, "planner"),
    ),
    TASK_PLANNER: Role(
        TASK_PLANNER,
        "Task Planning Agent",
        "Reads the planning documents and manages the task list.",
        (
            "read the planning documents",
            "create, split, change and remove tasks (plain language)",
            "propose a change to the planning documents (you decide)",
        ),
        ("write documents, code or tests", "name files or commands"),
        (TASK_PLANNER, "planner"),
    ),
    TESTER: Role(
        TESTER,
        "Tester Agent",
        "Writes the tests for each task, and the acceptance tests, and runs them.",
        ("write and change files under the project's test paths", "run commands and tests"),
        ("change any file outside the test paths",),
        (TESTER, "executor"),
    ),
    BUILDER: Role(
        BUILDER,
        "Builder Agent",
        "Writes the code that makes each task's tests pass.",
        (
            "write and change code, docs and the toolchain Dockerfile",
            "run commands, including the tests",
        ),
        ("change any test",),
        (BUILDER, "executor"),
    ),
}

# Every binding key a project may set (the dashboard shows these).
BINDING_KEYS = (PROJECT_PLANNER, TASK_PLANNER, TESTER, BUILDER, ESCALATION)


def binding(project: Project, role: str) -> dict[str, Any]:
    """The {node_id, model} a role uses in this project ({} if unset)."""
    bindings = project.role_bindings or {}
    keys = ROLES[role].bindings if role in ROLES else (role,)
    for key in keys:
        b = bindings.get(key) or {}
        if b.get("node_id") is not None:
            return dict(b)
    return {}


def node_and_model(project: Project, role: str) -> tuple[Node | None, str]:
    from foreman.db import session

    b = binding(project, role)
    node = None
    if b.get("node_id") is not None:
        with session() as s:
            node = s.get(Node, b["node_id"])
    return node, str(b.get("model", ""))


# -- documents ---------------------------------------------------------------------


def doc_file(project: Project, key: str, exists: Any = None) -> str:
    """Repo-relative path of a planning document. `exists(rel) -> bool` lets old
    projects keep their original file names."""
    base = f"specs/{project.feature}/"
    new = base + DOCS[key]
    if key in LEGACY_DOCS and exists is not None:
        old = base + LEGACY_DOCS[key]
        if not exists(new) and exists(old):
            return old
    return new


def planner_may_write(project: Project, rel: str, exists: Any = None) -> bool:
    """The Project Planner writes exactly the three planning documents."""
    return rel in {doc_file(project, k, exists) for k in PLANNING_DOCS}


# -- tests and code ------------------------------------------------------------------

# Test files that can live next to code, outside the test directories.
TEST_FILE_PATTERNS = (
    "test_*.py",
    "*_test.py",
    "*_test.go",
    "*.test.ts",
    "*.test.tsx",
    "*.test.js",
    "*.spec.ts",
    "*.spec.js",
    "*_spec.rb",
    "*Test.java",
)


def is_test_path(project: Project, rel: str) -> bool:
    dirs = [d.strip("/") for d in (project.test_paths or ["tests"]) if d.strip("/")]
    if any(rel == d or rel.startswith(d + "/") for d in dirs):
        return True
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, p) for p in TEST_FILE_PATTERNS)


def fence(project: Project, role: str, changed: list[str]) -> list[str]:
    """Files this role changed that it may not touch (empty = within its capabilities)."""
    if role == TESTER:
        return [f for f in changed if not is_test_path(project, f)]
    if role == BUILDER:
        return [f for f in changed if is_test_path(project, f)]
    return []


def denied(project_id: int, task_id: int | None, role: str, what: str, **data: Any) -> None:
    """Record an action an agent tried that its role doesn't allow."""
    from foreman.core.state import emit

    emit(
        project_id,
        task_id,
        "denied",
        f"{ROLES[role].name if role in ROLES else role} may not {what}",
        role=role,
        **data,
    )


def describe() -> list[dict[str, Any]]:
    """The registry, for the dashboard."""
    return [
        {
            "key": r.key,
            "name": r.name,
            "summary": r.summary,
            "may": list(r.may),
            "may_not": list(r.may_not),
        }
        for r in ROLES.values()
    ]
