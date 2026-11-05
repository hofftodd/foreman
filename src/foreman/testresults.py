"""Test results and coverage from a project's test runner.

The Tester sets up ``tests/run.sh`` to run the whole suite and write, into
``$FOREMAN_RESULTS``:

- ``junit.xml``: JUnit XML (pytest ``--junitxml``, jest-junit, gotestsum, ...);
- ``coverage.xml`` (Cobertura) or ``lcov.info``: line coverage.

These are the formats nearly every language's tools can produce, so Foreman
needs nothing language-specific. This module parses them, works out which
tests belong to a task, and computes the coverage of the lines the Builder
changed ("changed-line coverage").
"""

from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

CONTAINER_WORKSPACE = "/workspace/"


@dataclass
class Case:
    name: str
    classname: str = ""
    file: str = ""
    status: str = "passed"  # passed | failed | error | skipped
    duration_s: float = 0.0
    message: str = ""


@dataclass
class Results:
    cases: list[Case] = field(default_factory=list)
    # {file: {line: hits}} for lines the coverage tool instrumented.
    lines: dict[str, dict[int, int]] = field(default_factory=dict)
    problem: str = ""

    def count(self, status: str) -> int:
        return sum(1 for c in self.cases if c.status == status)

    @property
    def coverage(self) -> float | None:
        total = sum(len(v) for v in self.lines.values())
        if not total:
            return None
        covered = sum(1 for v in self.lines.values() for hits in v.values() if hits > 0)
        return 100.0 * covered / total

    def per_file(self) -> dict[str, dict[str, int]]:
        return {
            f: {"covered": sum(1 for h in v.values() if h > 0), "total": len(v)}
            for f, v in sorted(self.lines.items())
        }


# -- parsing ---------------------------------------------------------------------


def parse_junit(text: str) -> list[Case]:
    root = ET.fromstring(text)
    cases: list[Case] = []
    for tc in root.iter("testcase"):
        status, message = "passed", ""
        for child in tc:
            tag = child.tag.lower()
            if tag in ("failure", "error", "skipped"):
                status = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                message = (child.get("message") or child.text or "").strip()[:2000]
                break
        try:
            duration = float(tc.get("time") or 0)
        except ValueError:
            duration = 0.0
        cases.append(
            Case(
                name=tc.get("name", ""),
                classname=tc.get("classname", ""),
                file=_rel(tc.get("file", "")),
                status=status,
                duration_s=duration,
                message=message,
            )
        )
    return cases


def _rel(path: str, root: str = "") -> str:
    """Repo-relative path from what a tool reported (absolute in the container, etc.)."""
    path = path.replace("\\", "/")
    for prefix in (root.rstrip("/") + "/" if root else "", CONTAINER_WORKSPACE):
        if prefix and path.startswith(prefix):
            path = path[len(prefix) :]
    return str(PurePosixPath(path)).lstrip("/") if path else ""


def parse_cobertura(text: str, root: str = "") -> dict[str, dict[int, int]]:
    tree = ET.fromstring(text)
    sources = [s.text.strip() for s in tree.iter("source") if s.text and s.text.strip()]
    out: dict[str, dict[int, int]] = {}
    for cls in tree.iter("class"):
        filename = cls.get("filename", "")
        if not filename:
            continue
        # Filenames are relative to one of the <source> roots.
        rel = _rel(filename, root)
        for src in sources:
            src_rel = _rel(src, root)
            if src_rel and src_rel != "." and not rel.startswith(src_rel + "/"):
                candidate = f"{src_rel}/{rel}"
                if root and (Path(root) / candidate).exists():
                    rel = candidate
                    break
        lines = out.setdefault(rel, {})
        for line in cls.iter("line"):
            try:
                num, hits = int(line.get("number", "0")), int(float(line.get("hits", "0")))
            except ValueError:
                continue
            lines[num] = max(lines.get(num, 0), hits)
    return out


def parse_lcov(text: str, root: str = "") -> dict[str, dict[int, int]]:
    out: dict[str, dict[int, int]] = {}
    current: dict[int, int] | None = None
    for line in text.splitlines():
        if line.startswith("SF:"):
            current = out.setdefault(_rel(line[3:].strip(), root), {})
        elif line.startswith("DA:") and current is not None:
            parts = line[3:].split(",")
            try:
                num, hits = int(parts[0]), int(float(parts[1]))
            except (ValueError, IndexError):
                continue
            current[num] = max(current.get(num, 0), hits)
        elif line.strip() == "end_of_record":
            current = None
    return out


def collect(results_dir: Path, workspace: Path) -> Results:
    """Everything the test runner wrote into results_dir."""
    res = Results()
    junit = results_dir / "junit.xml"
    if not junit.is_file():
        res.problem = "the test runner wrote no junit.xml to $FOREMAN_RESULTS"
        return res
    try:
        res.cases = parse_junit(junit.read_text(errors="replace"))
    except ET.ParseError as e:
        res.problem = f"junit.xml is not valid XML: {e}"
        return res
    root = str(workspace)
    try:
        if (results_dir / "coverage.xml").is_file():
            res.lines = parse_cobertura((results_dir / "coverage.xml").read_text(), root)
        elif (results_dir / "lcov.info").is_file():
            res.lines = parse_lcov((results_dir / "lcov.info").read_text(), root)
    except ET.ParseError as e:
        res.problem = f"coverage.xml is not valid XML: {e}"
    return res


# -- tasks and changes -------------------------------------------------------------


def is_task_case(case: Case, test_files: list[str]) -> bool:
    """Whether a test case lives in one of the files the Tester changed for the task."""
    for f in test_files:
        if case.file and case.file == f:
            return True
        stem = PurePosixPath(f).with_suffix("").as_posix()
        dotted = stem.replace("/", ".")
        name = PurePosixPath(f).stem
        cls = case.classname
        if cls and (cls == dotted or cls.startswith(dotted + ".") or cls.endswith(name)):
            return True
        if not cls and not case.file and name in case.name:
            return True
    return False


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def added_lines(repo: Path, base: str, files: list[str]) -> dict[str, set[int]]:
    """Line numbers added or changed in `files` since `base` (in the current version)."""
    if not files:
        return {}
    proc = subprocess.run(
        ["git", "-c", "safe.directory=*", "diff", "-U0", base, "--", *files],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    out: dict[str, set[int]] = {}
    current: set[int] | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path == "/dev/null":
                current = None
            else:
                current = out.setdefault(path.removeprefix("b/"), set())
        elif (m := _HUNK.match(line)) and current is not None:
            start, count = int(m.group(1)), int(m.group(2) or "1")
            current.update(range(start, start + count))
    return out


def changed_coverage(
    lines: dict[str, dict[int, int]], changed: dict[str, set[int]]
) -> tuple[float | None, int]:
    """Coverage of changed lines the coverage tool instruments: (percent, lines counted).

    Lines a tool doesn't instrument (blank lines, comments, declarations) don't count;
    None when no changed line is instrumented."""
    total = covered = 0
    for path, nums in changed.items():
        hits = lines.get(path)
        if hits is None:
            continue
        for n in nums:
            if n in hits:
                total += 1
                covered += hits[n] > 0
    return (100.0 * covered / total if total else None), total
