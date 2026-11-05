"""M4.6: crash-recovery hardening and the Aider executor."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from foreman import gitops
from foreman.core.models import Project, TaskState
from foreman.db import session
from foreman.executors import EXECUTORS
from foreman.orchestrator.main import Orchestrator, recover
from tests.conftest import git
from tests.fakellm import FakeLLM
from tests.helpers import TERMINAL, drive, make_project, make_task, project, task


def repo(pid: int) -> Path:
    return gitops.repo_dir(pid)


def test_interrupted_merge_is_aborted_on_restart(client, origin):
    p = make_project(client, origin)
    r = repo(p["id"])
    branch = p["milestone_branch"]
    # Two branches that conflict, merged with the merge left open, as a crash would.
    git("checkout", "-q", "-b", "side", cwd=r)
    (r / "README.md").write_text("side\n")
    git("commit", "-qam", "side", cwd=r)
    git("checkout", "-q", branch, cwd=r)
    (r / "README.md").write_text("main\n")
    git("commit", "-qam", "main", cwd=r)
    subprocess.run(["git", "merge", "side"], cwd=r, capture_output=True)
    assert (r / ".git" / "MERGE_HEAD").exists()
    (r / ".git" / "index.lock").write_text("")

    recover()
    assert not (r / ".git" / "MERGE_HEAD").exists()
    assert not (r / ".git" / "index.lock").exists()
    assert (r / "README.md").read_text() == "main\n"
    assert git("status", "--porcelain", "--untracked-files=no", cwd=r) == ""


def test_half_written_files_are_dropped(client, origin):
    p = make_project(client, origin)
    r = repo(p["id"])
    (r / "README.md").write_text("half written\n")
    assert gitops.recover_repo(p["id"], p["milestone_branch"]) == ["discarded uncommitted changes"]
    assert (r / "README.md").read_text() == "# demo\n"


def test_wrong_branch_is_restored(client, origin):
    p = make_project(client, origin)
    r = repo(p["id"])
    git("checkout", "-q", "-b", "elsewhere", cwd=r)
    (r / "README.md").write_text("half written\n")
    actions = gitops.recover_repo(p["id"], p["milestone_branch"])
    assert actions == [f"switched back to {p['milestone_branch']}"]  # --force drops the edit
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=r).strip() == p["milestone_branch"]
    assert (r / "README.md").read_text() == "# demo\n"
    assert gitops.recover_repo(p["id"], p["milestone_branch"]) == []  # clean: nothing to do


def test_interrupted_acceptance_is_killed_and_cleared(client, origin):
    p = make_project(client, origin)
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        with session() as s:
            row = s.get(Project, p["id"])
            assert row is not None
            row.acceptance_handle = f"local:{proc.pid}"
            s.add(row)
            s.commit()
        recover()
        assert project(p["id"]).acceptance_handle == ""
        assert proc.wait(timeout=10) is not None
    finally:
        if proc.poll() is None:
            proc.kill()


# -- Aider ------------------------------------------------------------------------


def test_aider_command_and_files(tmp_path):
    from foreman.core.models import Task

    inv = EXECUTORS["aider"].prepare(
        Project(name="p", repo_url="x"),
        Task(
            project_id=1,
            title="t",
            allowed_globs=["src/a.py", "tests/*.py"],
            context_files=["src/b.py", "src/a.py"],
        ),
        "http://gw/g/tok/v1",
        "qwen",
        tmp_path,
        "do the thing",
    )
    cmd = inv.command[-1]
    assert (tmp_path / "prompt.md").read_text() == "do the thing"
    assert "--model openai/qwen" in cmd and "--no-auto-commits" in cmd
    assert cmd.endswith("-- src/a.py")  # globs are not files
    assert "--read src/b.py" in cmd and "--read src/a.py" not in cmd
    assert inv.env["OPENAI_API_BASE"] == "http://gw/g/tok/v1"
    assert inv.env["OPENAI_API_KEY"] == "foreman"  # the real key stays host-side


@pytest.mark.skipif(shutil.which("aider") is not None, reason="aider is installed here")
async def test_missing_aider_blocks_instead_of_retrying(client, origin):
    llm = FakeLLM(host="127.0.0.1").start()
    try:
        n = client.post("/api/nodes", json={"name": "n", "base_url": llm.url}).json()
        p = make_project(
            client,
            origin,
            executor_kind="aider",
            watchdog={"escalation": True, "max_retries": 3, "split": False},
            role_bindings={"executor": {"node_id": n["id"], "model": "m"}},
        )
        t = make_task(client, p["id"], script="")
        await drive(
            Orchestrator(),
            lambda: task(t["id"]).state in (*TERMINAL, TaskState.BLOCKED),
        )
        row = task(t["id"])
        assert row.state == TaskState.BLOCKED, row.reason
        assert row.attempts == 1  # a config problem is not retried
        assert "aider is not installed" in row.reason
    finally:
        llm.stop()
