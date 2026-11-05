"""Parsing test results and coverage; changed-line coverage; role bindings."""

from __future__ import annotations

import subprocess

from foreman import testresults as tr
from foreman.core import roles
from foreman.core.models import Project

JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest" tests="4">
  <testcase classname="tests.test_greet" name="test_ok" time="0.01" file="tests/test_greet.py"/>
  <testcase classname="tests.test_greet" name="test_bad" time="0.2">
    <failure message="assert 1 == 2">long traceback</failure></testcase>
  <testcase classname="tests.test_other" name="test_err"><error message="boom"/></testcase>
  <testcase classname="tests.test_other" name="test_skip"><skipped/></testcase>
</testsuite></testsuites>"""

COBERTURA = """<?xml version="1.0"?>
<coverage><sources><source>/workspace/src</source></sources><packages><package><classes>
  <class filename="greet.py"><lines>
    <line number="1" hits="1"/><line number="2" hits="0"/><line number="3" hits="4"/>
  </lines></class>
</classes></package></packages></coverage>"""

LCOV = """SF:/workspace/src/greet.py
DA:1,1
DA:2,0
end_of_record
SF:src/cli.py
DA:5,2
end_of_record
"""


def test_parse_junit():
    cases = tr.parse_junit(JUNIT)
    assert [c.status for c in cases] == ["passed", "failed", "error", "skipped"]
    assert cases[0].file == "tests/test_greet.py" and cases[1].message == "assert 1 == 2"
    assert cases[1].duration_s == 0.2


def test_parse_cobertura_resolves_sources(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "greet.py").write_text("x\n")
    lines = tr.parse_cobertura(COBERTURA, str(tmp_path))
    assert lines == {"src/greet.py": {1: 1, 2: 0, 3: 4}}


def test_parse_lcov():
    lines = tr.parse_lcov(LCOV)
    assert lines == {"src/greet.py": {1: 1, 2: 0}, "src/cli.py": {5: 2}}


def test_collect_reports_missing_results(tmp_path):
    res = tr.collect(tmp_path, tmp_path)
    assert "no junit.xml" in res.problem
    (tmp_path / "junit.xml").write_text(JUNIT)
    (tmp_path / "lcov.info").write_text(LCOV)
    res = tr.collect(tmp_path, tmp_path)
    assert res.problem == "" and res.count("passed") == 1
    assert res.coverage == 200 / 3
    assert res.per_file()["src/greet.py"] == {"covered": 1, "total": 2}


def test_task_cases_are_found_by_file_or_class():
    a = tr.Case(name="test_ok", classname="tests.test_greet", file="tests/test_greet.py")
    b = tr.Case(name="test_x", classname="tests.test_greet")
    c = tr.Case(name="test_y", classname="tests.test_other")
    files = ["tests/test_greet.py"]
    assert tr.is_task_case(a, files) and tr.is_task_case(b, files)
    assert not tr.is_task_case(c, files)


def test_changed_line_coverage(tmp_path):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (tmp_path / "greet.py").write_text("a\nb\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    (tmp_path / "greet.py").write_text("a\nB\nc\nd\n")  # lines 2-4 changed/added
    git("commit", "-qam", "change")
    added = tr.added_lines(tmp_path, base, ["greet.py"])
    assert added == {"greet.py": {2, 3, 4}}
    # Line 4 isn't instrumented (e.g. a comment), so it doesn't count.
    pct, counted = tr.changed_coverage({"greet.py": {1: 1, 2: 1, 3: 0}}, added)
    assert counted == 2 and pct == 50.0
    assert tr.changed_coverage({}, added) == (None, 0)


def test_role_bindings_fall_back_for_older_projects():
    p = Project(
        name="p",
        repo_url="x",
        role_bindings={
            "planner": {"node_id": 1, "model": "big"},
            "executor": {"node_id": 2, "model": "small"},
            "tester": {"node_id": 3, "model": "mid"},
        },
    )
    assert roles.binding(p, roles.PROJECT_PLANNER)["model"] == "big"
    assert roles.binding(p, roles.TASK_PLANNER)["model"] == "big"
    assert roles.binding(p, roles.TESTER)["model"] == "mid"  # its own binding wins
    assert roles.binding(p, roles.BUILDER)["model"] == "small"
    assert roles.binding(p, roles.ESCALATION) == {}


def test_test_paths_and_fences():
    p = Project(name="p", repo_url="x", test_paths=["tests", "spec/"])
    assert roles.is_test_path(p, "tests/test_a.py") and roles.is_test_path(p, "spec/x_spec.rb")
    assert roles.is_test_path(p, "src/foo_test.go")  # test files next to code count too
    assert not roles.is_test_path(p, "src/app.py")
    changed = ["src/app.py", "tests/test_a.py"]
    assert roles.fence(p, roles.TESTER, changed) == ["src/app.py"]
    assert roles.fence(p, roles.BUILDER, changed) == ["tests/test_a.py"]
