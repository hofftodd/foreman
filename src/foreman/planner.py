"""The two planning agents (see core/roles.py), as pure functions over text.

- The **Project Planning Agent** works out the project with the user and writes the
  planning documents (requirements, design, acceptance criteria): human-readable
  markdown, no code. It writes them by emitting blocks::

      <<<FILE specs/001-initial/requirements.md
      ...complete file content...
      >>>

  Full-file blocks (not diffs) are deliberately simple so small local models
  can follow them reliably. The parser is lenient about the variations local
  models produce: the path may be on the line after ``<<<FILE``, and a block
  may end at the next ``<<<FILE`` instead of ``>>>`` (a bare ``<<<FILE``
  followed by a blank line is then only a terminator). A block still open when
  the reply ends is not applied: the reply was cut off. Writes to anything but
  the planning documents are refused.

- The **Task Planning Agent** breaks the documents into tasks, in plain language
  (goal, acceptance criteria, requirement ids, dependencies, size): no files and
  no commands, which the Tester and Builder work out. Its JSON is validated
  (fields, dependency graph, size) and problems are sent back for another try.
  Foreman adds the standard tasks every project gets (setup, acceptance tests,
  documentation).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from foreman.core import roles
from foreman.core.models import Project

DOC_NAMES = tuple(roles.DOCS)  # requirements, design, acceptance, tasks

_FILE_OPEN = re.compile(r"^<<<FILE(?:[ \t]+(.*?))?[ \t]*$")
_CMD_OPEN = re.compile(r"^<<<ACCEPTANCE_CMD[ \t]*$")
_CLOSE = ">>>"


def _exists(project: Project):  # type: ignore[no-untyped-def]
    if project.id is None:
        return None
    from foreman.gitops import repo_dir

    root = repo_dir(project.id)
    return lambda rel: (root / rel).is_file()


def doc_path(project: Project, name: str) -> str:
    """Path of a planning document (older projects keep their original file names)."""
    return roles.doc_file(project, name, _exists(project))


def writable(project: Project, rel: str) -> bool:
    """The Project Planner writes the three planning documents and nothing else."""
    return roles.planner_may_write(project, rel, _exists(project))


@dataclass
class ChatOutput:
    message: str  # what the planner said, with file blocks replaced by notes
    files: dict[str, str] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)


def parse_chat(project: Project, text: str) -> ChatOutput:
    from foreman.gitops import safe_rel_path

    out = ChatOutput(message="")
    message: list[str] = []
    # The open block: ("file", path) or ("cmd", ""), and its lines so far.
    block: tuple[str, str] | None = None
    body: list[str] = []

    def take_file(raw_path: str, content: str) -> str:
        try:
            rel = safe_rel_path(raw_path)
        except ValueError:
            out.rejected.append(raw_path)
            return f"[rejected write to {raw_path}]"
        if not writable(project, rel):
            out.rejected.append(rel)
            return f"[rejected write to {rel}]"
        out.files[rel] = content
        return f"[wrote {rel}]"

    def close() -> None:
        nonlocal block
        if block is None:
            return
        kind, path = block
        if kind == "file":
            message.append(take_file(path, "\n".join(body)))
        else:
            # Commands are the Tester's job; the planner only describes criteria.
            out.rejected.append("an acceptance command")
            message.append("[rejected: acceptance command]")
        block = None
        body.clear()

    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if m := _FILE_OPEN.match(line):
            close()
            path = (m.group(1) or "").strip()
            nxt = lines[i].strip() if i < len(lines) else ""
            if not path and nxt and not any(c.isspace() for c in nxt):
                path = nxt  # path on the next line (prose there means a bare terminator)
                i += 1
            if path:
                block = ("file", path)
        elif _CMD_OPEN.match(line):
            close()
            block = ("cmd", "")
        elif block is not None and line.strip() == _CLOSE:
            close()
        elif block is not None:
            body.append(line)
        else:
            message.append(line)
    if block is not None:
        kind, path = block
        message.append(
            f"[incomplete file {path}: not written]" if kind == "file" else "[incomplete command]"
        )
    out.message = "\n".join(message).strip()
    return out


# What `.foreman/Dockerfile` builds on (docker/executor-base/Dockerfile). The Builder
# writes that file in the setup task; this tells it what is already there.
BASE_IMAGE = (
    "`foreman/executor-base` is Ubuntu 24.04 with git, curl, bash and the coding agent, and "
    "no language runtimes (no Node, no Python): the Dockerfile must install what the project "
    "needs. The base image ends with `USER agent`, so switch to `USER root` for apt-get and "
    "back to `USER agent` at the end."
)

PLAIN_LANGUAGE = (
    "Think and write in plain language about what the software must do and how it is "
    "organised. Never write code, tests, file names or paths, shell commands or "
    "configuration: the Tester and Builder agents do that later, from your documents."
)


def chat_stage(docs: dict[str, str]) -> str:
    """ "design" until the three planning documents exist, then "review"."""
    if not all(docs.get(n, "").strip() for n in roles.PLANNING_DOCS):
        return "design"
    return "review"


_STAGE_GUIDANCE = {
    "design": [
        "Current step: DESIGN. Ask clarifying questions until the request is clear, then "
        "write all three documents. End by asking the user to review them.",
    ],
    "review": [
        "Current step: REVIEW. The documents exist. Revise them as the user asks, keeping "
        "requirements, design and acceptance criteria consistent with each other. When the "
        "user is happy, they approve them and the work is broken into tasks.",
    ],
}


def chat_system_prompt(project: Project, docs: dict[str, str], tree: list[str]) -> str:
    """The Project Planning Agent's instructions and the documents as they are now."""
    paths = {k: doc_path(project, k) for k in roles.PLANNING_DOCS}
    stage = chat_stage(docs)
    parts = [
        "You are the Project Planning Agent. You work out a software project with the user "
        "and write its planning documents, which everyone on the team reads.",
        PLAIN_LANGUAGE,
        "",
        "The documents (markdown, concise, readable by anyone):",
        f"1. {paths['requirements']} - what the software must do. Give every requirement a "
        "stable id on its own bullet, e.g. `- FR-1.1 Users can add a todo.` (functional) or "
        "`- NFR-1 Pages load within a second.` (non-functional).",
        f"2. {paths['design']} - how it is organised: the main parts, what each is responsible "
        "for, how they work together, the information they keep, and the important decisions "
        "with their reasons. Describe, don't implement.",
        f"3. {paths['acceptance']} - how we will know it is done: concrete, checkable "
        "statements, each citing the requirement ids it checks, e.g. "
        "`- AC-1 (FR-1.1) After adding a todo it appears in the list.`",
        "",
        "Ask clarifying questions when the request is ambiguous. If you have web search "
        "tools, use them to research topics you don't know well before deciding; mention "
        "what you relied on. Keep each document short: the work will be broken into small "
        "tasks.",
        "Think about decisions, not text: do not draft the documents while reasoning. Write "
        "each one once, in your reply.",
        "",
        *_STAGE_GUIDANCE[stage],
        "",
        "To create or replace a document, output a block with its COMPLETE new content:",
        "<<<FILE path/of/document.md",
        "...complete document...",
        ">>>",
        f"You may only write {', '.join(paths.values())}. Anything else is refused.",
        "Outside those blocks, reply to the user briefly: what you changed and what you need "
        "from them next.",
        "",
        "# Current documents",
    ]
    for key, path in paths.items():
        parts += ["", f"## {path}", docs.get(key, "").strip() or "(empty)"]
    if tree:
        parts += ["", "## The repository already contains (for context only)", *tree[:100]]
        if len(tree) > 100:
            parts.append(f"... and {len(tree) - 100} more")
    return "\n".join(parts)


# -- task planning ---------------------------------------------------------------

MAX_CRITERIA = 6
SIZES = ("small", "medium")
# Fields that would make the Task Planner think in code: refused.
CODE_FIELDS = ("allowed_files", "files", "context_files", "validation_cmd", "command", "todos")

STANDARD_TASKS_NOTE = (
    "Foreman adds three standard tasks itself; do not include them: project setup "
    "(toolchain, skeleton, test runner, first README and ARCHITECTURE), the acceptance tests, "
    "and bringing the documentation up to date at the end."
)

TASK_RULES = [
    "Each task is done by two agents in turn: a Tester writes tests for it from its "
    "criteria, then a Builder writes code until those tests pass. They work out the files, "
    "code and commands; you describe the work.",
    "Rules for every task:",
    "- It delivers one small piece of behaviour that can be tested on its own.",
    "- goal: one or two plain sentences on what it achieves.",
    f"- criteria: 1-{MAX_CRITERIA} checkable statements in plain language; together they "
    "say when the task is done (these become its tests).",
    "- req_refs: the requirement ids it implements.",
    "- deps: keys of tasks that must finish first. No cycles.",
    '- size: "small" or "medium". Anything bigger must be split into more tasks.',
    "- No file names, paths, code or commands anywhere.",
]

TASK_EXAMPLE = (
    '[{"key": "T1", "title": "Add a todo", "goal": "Users can add a todo with a title.", '
    '"req_refs": ["FR-1.1"], "criteria": ["A new todo appears in the list", '
    '"An empty title is refused with a message"], "deps": [], "size": "small"}]'
)


def _docs_section(project: Project, docs: dict[str, str]) -> list[str]:
    out: list[str] = []
    for key in roles.PLANNING_DOCS:
        out += ["", f"## {doc_path(project, key)}", docs.get(key, "").strip() or "(empty)"]
    return out


def decompose_system_prompt(project: Project, docs: dict[str, str]) -> str:
    """The Task Planning Agent breaks the approved documents into tasks."""
    return "\n".join(
        [
            "You are the Task Planning Agent. Read the approved planning documents and break "
            "the project into small, ordered tasks.",
            PLAIN_LANGUAGE,
            "",
            *TASK_RULES,
            STANDARD_TASKS_NOTE,
            "Together, the tasks must make every acceptance criterion true.",
            "",
            "Output ONLY a JSON array, no prose, like:",
            TASK_EXAMPLE,
            *_docs_section(project, docs),
        ]
    )


def split_system_prompt(
    project: Project, docs: dict[str, str], task: dict[str, Any], failures: list[str]
) -> str:
    """Ask the Task Planning Agent to split one task that keeps failing."""
    return "\n".join(
        [
            "You are the Task Planning Agent. The agents keep failing ONE task. Split it into "
            "2-4 smaller, ordered tasks that together achieve the same goal and criteria.",
            PLAIN_LANGUAGE,
            "",
            *TASK_RULES,
            "- deps lists keys of the other new tasks that must finish first.",
            "- Address the failures below: smaller steps, one behaviour at a time.",
            "",
            "Output ONLY a JSON array in the same format as the task below.",
            "",
            "## Task to split",
            json.dumps(task, indent=2),
            "",
            "## Why it failed",
            *[f"- {f}" for f in failures],
            *_docs_section(project, docs),
        ]
    )


class PlanError(ValueError):
    pass


def extract_json_array(text: str) -> list[Any]:
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            raise PlanError("no JSON array found in the response")
        candidate = text[start : end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise PlanError(f"invalid JSON: {e}") from e
    if not isinstance(data, list) or not data:
        raise PlanError("expected a non-empty JSON array of tasks")
    return data


def _str_list(item: dict[str, Any], name: str) -> list[str]:
    v = item.get(name) or []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        raise PlanError(f"task {item.get('key')}: {name} must be a list")
    return [str(x).strip() for x in v if str(x).strip()]


@dataclass
class TaskSpec:
    key: str
    title: str
    goal: str
    req_refs: list[str]
    criteria: list[str]
    deps: list[str]
    size: str = "small"
    kind: str = "feature"  # setup | acceptance | feature | docs


def parse_tasks(text: str) -> list[TaskSpec]:
    """Parse and validate the Task Planner's task list (raises PlanError)."""
    return parse_task_items(extract_json_array(text))


def parse_task_items(items: list[Any], known: tuple[str, ...] = ()) -> list[TaskSpec]:
    """Validate task objects. `known` are keys of existing tasks new ones may depend on."""
    tasks: list[TaskSpec] = []
    problems: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"item {i} is not an object")
            continue
        key = str(item.get("key") or f"T{i + 1}").strip()
        code = [f for f in CODE_FIELDS if item.get(f)]
        if code:
            problems.append(
                f"task {key}: don't give {', '.join(code)}; describe the goal and criteria, "
                "and the Tester and Builder work out files and commands"
            )
        try:
            t = TaskSpec(
                key=key,
                title=str(item.get("title") or "").strip(),
                goal=str(item.get("goal") or item.get("description") or "").strip(),
                req_refs=_str_list(item, "req_refs"),
                criteria=_str_list(item, "criteria"),
                deps=_str_list(item, "deps"),
                size=str(item.get("size") or "small").strip().lower(),
            )
        except PlanError as e:
            problems.append(str(e))
            continue
        if not t.title:
            problems.append(f"task {key}: missing title")
        if not t.criteria:
            problems.append(f"task {key}: needs at least one acceptance criterion")
        elif len(t.criteria) > MAX_CRITERIA:
            problems.append(
                f"task {key}: {len(t.criteria)} criteria (limit {MAX_CRITERIA}); split it"
            )
        if t.size not in SIZES:
            problems.append(f"task {key}: size {t.size!r} is too big; split it")
        tasks.append(t)

    keys = [t.key for t in tasks]
    dupes = {k for k in keys if keys.count(k) > 1 or k in known or k in STANDARD_KEYS}
    if dupes:
        problems.append(f"duplicate or reserved keys: {', '.join(sorted(dupes))}")
    allowed = set(keys) | set(known)
    for t in tasks:
        unknown = [d for d in t.deps if d not in allowed]
        if unknown:
            problems.append(f"task {t.key}: unknown deps {', '.join(unknown)}")
        if t.key in t.deps:
            problems.append(f"task {t.key}: depends on itself")
    if not problems:
        cycle = find_cycle({t.key: [d for d in t.deps if d in keys] for t in tasks})
        if cycle:
            problems.append(f"dependency cycle: {' -> '.join(cycle)}")
    if problems:
        raise PlanError("; ".join(problems))
    return tasks


# -- standard tasks -------------------------------------------------------------

SETUP, ACCEPTANCE, DOCS = "SETUP", "ACCEPT", "DOCS"
STANDARD_KEYS = (SETUP, ACCEPTANCE, DOCS)


def standard_tasks(features: list[TaskSpec]) -> list[TaskSpec]:
    """The plan as it runs: setup first, the acceptance tests, the Task Planner's tasks,
    and the documentation last."""
    setup = TaskSpec(
        key=SETUP,
        title="Project setup",
        goal="Prepare the project so every later task can be built and tested: the "
        "toolchain, the project skeleton, the test runner, and a first README.md and "
        "ARCHITECTURE.md based on the design.",
        req_refs=[],
        criteria=[
            "The test runner runs the whole test suite and reports results and coverage",
            "README.md explains what the project is, how to run it and how to test it",
            "ARCHITECTURE.md explains how the project is organised, following the design",
        ],
        deps=[],
        kind="setup",
    )
    acceptance = TaskSpec(
        key=ACCEPTANCE,
        title="Acceptance tests",
        goal="Turn the acceptance criteria into executable acceptance tests: the "
        "project's definition of done. They fail until the work is finished.",
        req_refs=[],
        criteria=["Every acceptance criterion is checked by at least one acceptance test"],
        deps=[SETUP],
        kind="acceptance",
    )
    planned = [
        TaskSpec(**{**t.__dict__, "deps": t.deps or [SETUP]})  # everything after setup
        for t in features
    ]
    docs = TaskSpec(
        key=DOCS,
        title="Documentation",
        goal="Bring README.md and ARCHITECTURE.md up to date with what was built.",
        req_refs=[],
        criteria=[
            "README.md says what the project is, how to run it and how to test it, and "
            "its run and test instructions work",
            "ARCHITECTURE.md describes how the finished project is organised",
        ],
        deps=[ACCEPTANCE, *[t.key for t in planned]],
        kind="docs",
    )
    return [setup, acceptance, *planned, docs]


def find_cycle(graph: dict[str, list[str]]) -> list[str]:
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(n: str) -> list[str]:
        state[n] = 1
        path.append(n)
        for d in graph.get(n, []):
            if state.get(d) == 1:
                return [*path[path.index(d) :], d]
            if state.get(d) is None:
                found = visit(d)
                if found:
                    return found
        state[n] = 2
        path.pop()
        return []

    for node in graph:
        if state.get(node) is None:
            found = visit(node)
            if found:
                return found
    return []


def topo_order(tasks: list[TaskSpec]) -> list[TaskSpec]:
    by_key = {t.key: t for t in tasks}
    done: set[str] = set()
    out: list[TaskSpec] = []

    def visit(t: TaskSpec) -> None:
        if t.key in done:
            return
        done.add(t.key)
        for d in t.deps:
            visit(by_key[d])
        out.append(t)

    for t in tasks:
        visit(t)
    return out


def retry_message(problems: list[str]) -> str:
    return (
        "Your task list has problems:\n"
        + "\n".join(f"- {p}" for p in problems)
        + "\nFix them, splitting tasks that are too large, and output the COMPLETE corrected "
        "JSON array only (plain language: no files, code or commands)."
    )


def render_tasks_md(project: Project, rows: list[dict[str, Any]]) -> str:
    """specs/<feature>/tasks.md: the human-readable task list kept in the repo."""
    lines = [
        f"# Tasks: {project.name}",
        "",
        "Kept by Foreman from the Task Planning Agent's plan. For each task the Tester writes "
        "tests from its criteria, then the Builder makes them pass.",
        "",
    ]
    for r in rows:
        lines += [f"## {r['key']}: {r['title']}", ""]
        goal = r.get("goal") or r.get("description")
        if goal:
            lines += [goal, ""]
        if r.get("criteria"):
            lines += ["Done when:", *[f"- {c}" for c in r["criteria"]], ""]
        meta = []
        if r.get("req_refs"):
            meta.append(f"- Requirements: {', '.join(r['req_refs'])}")
        if r.get("deps"):
            meta.append(f"- Depends on: {', '.join(r['deps'])}")
        if r.get("kind") and r["kind"] != "feature":
            meta.append(f"- Kind: {r['kind']}")
        if r.get("state"):
            meta.append(f"- State: {r['state']}")
        lines += meta
        if r.get("violations"):
            lines += ["", f"> Size check: {'; '.join(r['violations'])}"]
        lines.append("")
    return "\n".join(lines)
