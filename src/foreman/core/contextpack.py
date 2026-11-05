"""Context packs: exactly what an executor gets for one task attempt.

A pack holds the task's slice of the requirements, its todos, the interface
signatures of files it depends on (not their bodies), the current contents of
the files it may change, and how it will be validated. Its size is checked
against the executor model's context window so small local models are never
handed more than they can work with (FR-3.3).

Token counts are estimated at ~4 characters per token; models differ, so the
budget percentage leaves headroom.
"""

from __future__ import annotations

import ast
import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path

from foreman.core.models import Project, Task

CHARS_PER_TOKEN = 4
MAX_FILE_LINES = 400
DEFAULT_CONTEXT_WINDOW = 32768


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


@dataclass
class ContextPack:
    text: str
    tokens: int
    budget: int
    files: list[str] = field(default_factory=list)
    missing_refs: list[str] = field(default_factory=list)

    @property
    def over_budget(self) -> bool:
        return self.tokens > self.budget


# -- requirement slices -------------------------------------------------------


def requirement_slice(spec: str, refs: list[str]) -> tuple[str, list[str]]:
    """The spec lines that define each referenced requirement id.

    A requirement is the line containing its id (e.g. ``FR-2.1``) plus the
    indented continuation lines after it. Returns (text, refs not found).
    """
    lines = spec.splitlines()
    out: list[str] = []
    missing: list[str] = []
    for ref in refs:
        pattern = re.compile(rf"(?<![\w.-]){re.escape(ref)}(?![\w-]|\.\d)")
        idx = next((i for i, line in enumerate(lines) if pattern.search(line)), None)
        if idx is None:
            missing.append(ref)
            continue
        block = [lines[idx]]
        indent = len(lines[idx]) - len(lines[idx].lstrip())
        for line in lines[idx + 1 :]:
            stripped = line.strip()
            if not stripped:
                break
            line_indent = len(line) - len(line.lstrip())
            if line_indent <= indent and (stripped.startswith(("-", "*", "#")) or line_indent == 0):
                break
            block.append(line)
        out.append("\n".join(block))
    return "\n".join(out), missing


# -- interface extraction -------------------------------------------------------

_SIGNATURE = re.compile(
    r"^\s*(export\s+)?(default\s+)?(pub(\([^)]*\))?\s+)?(async\s+)?"
    r"(def|class|function|interface|type|func|fn|struct|enum|trait|impl|const|module)\b"
)


def _python_interface(source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    lines = source.splitlines()
    out: list[str] = []

    def visit(nodes: list[ast.stmt], depth: int) -> None:
        for node in nodes:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                start = (node.decorator_list[0].lineno if node.decorator_list else node.lineno) - 1
                # Header lines up to (and including) the one ending with ':'
                end = node.body[0].lineno - 1 if node.body else node.lineno
                header = [ln for ln in lines[start:end] if ln.strip()]
                while header and not header[-1].rstrip().endswith(":"):
                    header.pop()
                out.extend(header or [lines[node.lineno - 1]])
                doc = ast.get_docstring(node)
                if doc:
                    pad = " " * (node.col_offset + 4)
                    out.append(f'{pad}"""{doc.strip().splitlines()[0]}"""')
                if isinstance(node, ast.ClassDef):
                    visit(node.body, depth + 1)
                else:
                    out.append(" " * (node.col_offset + 4) + "...")
            elif depth == 0 and isinstance(node, ast.Assign | ast.AnnAssign):
                out.append(lines[node.lineno - 1])

    visit(tree.body, 0)
    return "\n".join(out)


def extract_interface(path: str, source: str) -> str:
    """Signatures of a source file: enough to call into it without reading it."""
    if path.endswith(".py"):
        iface = _python_interface(source)
        if iface:
            return iface
    sig = [line.rstrip() for line in source.splitlines() if _SIGNATURE.match(line)]
    if sig:
        return "\n".join(sig)
    head = source.splitlines()[:40]
    return "\n".join(head)


# -- files ---------------------------------------------------------------------


def repo_files(repo: Path) -> list[str]:
    """Tracked + untracked (not ignored) files, repo-relative."""
    import subprocess

    proc = subprocess.run(
        ["git", "-c", "safe.directory=*", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return sorted({f for f in proc.stdout.splitlines() if f})


def matching_files(globs: list[str], files: list[str]) -> list[str]:
    return sorted({f for f in files for g in globs if fnmatch.fnmatch(f, g)})


def _read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(errors="replace")
    except OSError:
        return ""


def _clip(text: str) -> str:
    lines = text.splitlines()
    if len(lines) <= MAX_FILE_LINES:
        return text
    return "\n".join(lines[:MAX_FILE_LINES]) + f"\n... ({len(lines) - MAX_FILE_LINES} more lines)"


def _fence(path: str) -> str:
    ext = Path(path).suffix.lstrip(".")
    return f"```{ext}" if ext else "```"


# -- the pack -----------------------------------------------------------------


def budget_tokens(
    project: Project, context_window: int | None, executor_kind: str | None = None
) -> int:
    """Tokens a task's context pack may use: `context_budget_pct` of the window left
    after the executor's own system prompt and tool schemas."""
    from foreman.executors import EXECUTORS

    window = context_window or DEFAULT_CONTEXT_WINDOW
    executor = EXECUTORS.get(executor_kind or project.executor_kind)
    overhead = executor.prompt_overhead_tokens if executor else 0
    return max(window - overhead, 0) * project.context_budget_pct // 100


def build_pack(
    project: Project,
    task: Task,
    repo: Path,
    *,
    context_window: int | None = None,
    spec: str | None = None,
    previous_failure: str = "",
) -> ContextPack:
    """Render the executor prompt for `task` from the repository at `repo`."""
    if spec is None:
        spec = _read(repo, f"specs/{project.feature}/spec.md")
    files = repo_files(repo) if (repo / ".git").exists() else []

    lines = [
        f"# Task {task.key or task.id}: {task.title}",
        "",
        "You are working in a git repository checked out in the current directory.",
        "Complete exactly this task. Do not work on anything else.",
        "",
    ]
    if task.description:
        lines += ["## Description", task.description.strip(), ""]

    missing: list[str] = []
    if task.req_refs:
        slice_text, missing = requirement_slice(spec, task.req_refs)
        if slice_text:
            lines += ["## Requirements (from the project spec)", slice_text, ""]

    if task.todos:
        lines += ["## Todos", *[f"- [ ] {t}" for t in task.todos], ""]

    if task.context_files:
        lines += ["## Interfaces you can use (signatures only; do not modify these files)", ""]
        for rel in task.context_files:
            source = _read(repo, rel)
            if not source:
                continue
            lines += [f"`{rel}`:", _fence(rel), extract_interface(rel, source), "```", ""]

    editable = matching_files(task.allowed_globs, files)
    if task.allowed_globs:
        lines += [
            "## Scope",
            "You may only create or modify files matching:",
            *[f"- `{g}`" for g in task.allowed_globs],
            "Changes outside this list will cause the task to be rejected.",
            "",
        ]
        for rel in editable:
            source = _read(repo, rel)
            lines += [f"Current `{rel}`:", _fence(rel), _clip(source), "```", ""]

    if task.validation_cmd:
        lines += [
            "## Done when",
            "This command exits with status 0:",
            "```",
            task.validation_cmd,
            "```",
            "Run it yourself before finishing.",
            "",
        ]
    if project.test_cmd:
        lines += [
            "The project test suite must also keep passing:",
            "```",
            project.test_cmd,
            "```",
            "",
        ]
    if previous_failure:
        lines += ["## Previous attempt failed", previous_failure.strip(), ""]
    lines += ["Do not commit; the orchestrator commits your changes."]

    text = "\n".join(lines) + "\n"
    return ContextPack(
        text=text,
        tokens=estimate_tokens(text),
        budget=budget_tokens(project, context_window, task.executor_kind or None),
        files=editable,
        missing_refs=missing,
    )


def size_violations(project: Project, task: Task, pack: ContextPack, files: list[str]) -> list[str]:
    """Why a task is too big for an executor (empty = fine)."""
    out: list[str] = []
    if not task.validation_cmd.strip():
        out.append("no validation command")
    if not task.allowed_globs:
        out.append("no allowed files (scope is unrestricted)")
    else:
        touched = len(matching_files(task.allowed_globs, files))
        # New files don't exist yet: count each literal (non-wildcard) glob as one file.
        new = sum(
            1 for g in task.allowed_globs if not any(c in g for c in "*?[") and g not in files
        )
        n = touched + new
        if n > project.max_task_files:
            out.append(f"touches {n} files (limit {project.max_task_files})")
    if pack.over_budget:
        out.append(f"context pack ~{pack.tokens} tokens exceeds budget {pack.budget}")
    if pack.missing_refs:
        out.append(f"unknown requirement ids: {', '.join(pack.missing_refs)}")
    return out


def build_prompt(project: Project, task: Task, previous_failure: str = "") -> str:
    """Backwards-compatible: render a pack without repository context."""
    return build_pack(
        project, task, Path("/nonexistent"), spec="", previous_failure=previous_failure
    ).text


# -- role packs (Tester / Builder) ----------------------------------------------------

RESULTS_CONTRACT = [
    "## How tests run",
    "`tests/run.sh` runs the whole test suite. It must exit non-zero when any test fails "
    "and write, into the directory `$FOREMAN_RESULTS`:",
    "- `junit.xml`: the results in JUnit XML (e.g. pytest `--junitxml`, jest-junit, gotestsum);",
    "- `coverage.xml` (Cobertura) or `lcov.info`: line coverage of the project's code.",
    "There is no internet access: use only what the toolchain image provides.",
    "",
]

KIND_NOTES = {
    "tester": {
        "setup": [
            "## This task: project setup",
            "Create `tests/run.sh` following the contract below (pick the project's test "
            "framework and coverage tool from the design), and a first smoke test that "
            "checks the project's entry point exists. The Builder sets up the toolchain "
            "and skeleton afterwards to make it pass.",
            "",
        ],
        "acceptance": [
            "## This task: acceptance tests",
            "Turn every acceptance criterion below into executable tests under "
            "`tests/acceptance/`, and write `tests/acceptance/run.sh`, which runs only those "
            "tests and follows the same results contract. They are the definition of done "
            "for the whole project, so they will fail until the work is finished.",
            "",
        ],
        "docs": [
            "## This task: documentation",
            "Write tests that check README.md says what the project is, how to run it and "
            "how to test it (with run and test commands that work), and that "
            "ARCHITECTURE.md describes how the project is organised.",
            "",
        ],
    },
    "builder": {
        "setup": [
            "## This task: project setup",
            "Write `.foreman/Dockerfile` (FROM foreman/executor-base) installing the "
            "language toolchain, test framework and coverage tool the tests need, the "
            "project skeleton, a README.md (what the project is, how to run it, how to "
            "test it) and an ARCHITECTURE.md (how the project is organised, following the "
            "design).",
            "",
        ],
        "docs": [
            "## This task: documentation",
            "Bring README.md and ARCHITECTURE.md up to date with what has been built.",
            "",
        ],
    },
}


def _doc(repo: Path, rel: str | None, limit: int = 120) -> str:
    if not rel:
        return ""
    text = _read(repo, rel).strip()
    lines = text.splitlines()
    more = f"\n... ({len(lines) - limit} more lines)" if len(lines) > limit else ""
    return "\n".join(lines[:limit]) + more


def build_role_pack(
    project: Project,
    task: Task,
    repo: Path,
    role: str,
    *,
    docs: dict[str, str],
    test_paths: list[str],
    task_tests: list[str] | None = None,
    base_image: str = "",
    context_window: int | None = None,
    previous_failure: str = "",
) -> ContextPack:
    """The briefing for the Tester or the Builder on one task.

    `docs` maps planning-document keys (requirements, design, acceptance) to their
    repo paths; `task_tests` are the files the Tester wrote for this task (shown to
    the Builder in full)."""
    files = repo_files(repo) if (repo / ".git").exists() else []
    test_dirs = ", ".join(f"`{d}/`" for d in test_paths)
    lines = [f"# Task {task.key or task.id}: {task.title}", ""]
    if role == "tester":
        lines += [
            "You are the Tester Agent. Write the tests for this task: they define when it is "
            "done, and should fail until the Builder Agent implements it. Then run them.",
            f"You may only create or change files under {test_dirs}. Everything else is "
            "read-only: changes there are rejected.",
            "",
        ]
    else:
        lines += [
            "You are the Builder Agent. Write the code that makes this task's tests pass, "
            "without breaking the rest of the test suite.",
            f"You may change any file except the tests ({test_dirs}), which are read-only: "
            "you can run them but not edit them.",
            "",
        ]
    lines += KIND_NOTES.get(role, {}).get(task.kind, [])
    if task.description:
        lines += ["## Goal", task.description.strip(), ""]
    if task.criteria:
        lines += ["## Done when", *[f"- {c}" for c in task.criteria], ""]
    missing: list[str] = []
    if task.req_refs:
        requirements = _read(repo, docs.get("requirements", ""))
        slice_text, missing = requirement_slice(requirements, task.req_refs)
        if slice_text:
            lines += ["## Requirements", slice_text, ""]
    if task.kind == "acceptance" and role == "tester":
        criteria = _doc(repo, docs.get("acceptance"), limit=400)
        if criteria:
            lines += ["## Acceptance criteria (the whole project)", criteria, ""]
    design = _doc(repo, docs.get("design"))
    if design:
        lines += ["## Design (excerpt)", design, ""]
    if role == "builder" and task_tests:
        lines += ["## The tests for this task (read-only)", ""]
        for rel in task_tests:
            source = _read(repo, rel)
            if source:
                lines += [f"`{rel}`:", _fence(rel), _clip(source), "```", ""]
    if role == "builder" and task.kind == "setup" and base_image:
        lines += ["## Toolchain image", base_image, ""]
    lines += RESULTS_CONTRACT
    lines += ["Run the suite with `sh tests/run.sh`.", ""]
    if files:
        shown = files[:200]
        lines += ["## Repository files", *shown]
        if len(files) > len(shown):
            lines.append(f"... and {len(files) - len(shown)} more")
        lines.append("")
    if previous_failure:
        lines += ["## Previous attempt failed", previous_failure.strip(), ""]
    lines += ["Do not commit; the orchestrator commits your changes."]
    text = "\n".join(lines) + "\n"
    return ContextPack(
        text=text,
        tokens=estimate_tokens(text),
        budget=budget_tokens(project, context_window, task.executor_kind or None),
        files=task_tests or [],
        missing_refs=missing,
    )
