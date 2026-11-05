"""Git operations on the host side.

Layout per project (under FOREMAN_DATA_DIR/projects/<id>/):
    repo/            main clone; the milestone branch is checked out here
    tasks/<task_id>/ isolated local clone for one task, on branch foreman/task-<id>

Task clones are standalone (own .git directory, no remote) rather than git
worktrees: a worktree's .git file points at an absolute host path, which would
force us to mount the main repository - and its config/credentials - into
the executor container.
"""

from __future__ import annotations

import base64
import fnmatch
import subprocess
from pathlib import Path

from foreman.config import get_settings
from foreman.core.state import github_token

# Marks the commit a task clone started from.
BASE_REF = "refs/foreman/base"

GIT_IDENTITY = [
    "-c",
    "user.name=foreman",
    "-c",
    "user.email=foreman@localhost",
    # Task clones are owned by the container user, not necessarily by us.
    "-c",
    "safe.directory=*",
]


class GitError(RuntimeError):
    pass


def _auth_args(url: str) -> list[str]:
    token, _ = github_token()
    if not token or not url.startswith("https://github.com/"):
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    # Passed per command so the token is never written to .git/config.
    return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]


def git(*args: str, cwd: Path | None = None, auth_url: str = "", check: bool = True) -> str:
    cmd = ["git", *GIT_IDENTITY, *(_auth_args(auth_url) if auth_url else []), *args]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        safe = [a if "extraheader" not in a else "<auth>" for a in cmd]
        raise GitError(f"{' '.join(safe)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def project_dir(project_id: int) -> Path:
    return get_settings().projects_dir / str(project_id)


def repo_dir(project_id: int) -> Path:
    return project_dir(project_id) / "repo"


def task_dir(project_id: int, task_id: int) -> Path:
    return project_dir(project_id) / "tasks" / str(task_id)


def task_branch(task_id: int) -> str:
    return f"foreman/task-{task_id}"


def clone_project(project_id: int, repo_url: str, milestone_branch: str) -> str:
    """Clone the repo and check out the milestone branch. Returns the default branch."""
    dest = repo_dir(project_id)
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--", repo_url, str(dest), auth_url=repo_url)
    head = git("symbolic-ref", "--short", "-q", "HEAD", cwd=dest, check=False).strip()
    default_branch = head or "main"
    if not has_commits(dest):
        # Empty repository: create a root commit so branches have something to point at.
        git("commit", "--allow-empty", "-m", "Initial commit (foreman)", cwd=dest)
    exists = git("rev-parse", "--verify", "-q", milestone_branch, cwd=dest, check=False).strip()
    if exists:
        git("checkout", milestone_branch, cwd=dest)
    else:
        git("checkout", "-b", milestone_branch, cwd=dest)
    return default_branch


def has_commits(repo: Path) -> bool:
    return bool(git("rev-parse", "--verify", "-q", "HEAD", cwd=repo, check=False).strip())


def prepare_task_clone(project_id: int, task_id: int, milestone_branch: str) -> Path:
    """Create (or reuse) the task's isolated clone. Reuse keeps prior partial work."""
    dest = task_dir(project_id, task_id)
    if (dest / ".git").exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    repo = repo_dir(project_id)
    git("clone", "--quiet", "--no-hardlinks", "--branch", milestone_branch, str(repo), str(dest))
    git("remote", "remove", "origin", cwd=dest)
    git("checkout", "-b", task_branch(task_id), cwd=dest)
    git("update-ref", BASE_REF, "HEAD", cwd=dest)
    return dest


def reset_task_clone(project_id: int, task_id: int) -> None:
    import shutil

    shutil.rmtree(task_dir(project_id, task_id), ignore_errors=True)


def commit_all(path: Path, message: str) -> bool:
    """Commit any pending changes in the task clone. Returns True if a commit was made."""
    git("add", "-A", cwd=path)
    if not git("status", "--porcelain", cwd=path).strip():
        return False
    git("commit", "-q", "-m", message, cwd=path)
    return True


def _read(*args: str, cwd: Path) -> str:
    """Read-only git command that never takes optional locks (safe while an agent works)."""
    return git("--no-optional-locks", *args, cwd=cwd, check=False)


def changed_files(path: Path) -> list[str]:
    """Files changed in the task clone (committed or not) since the task started."""
    out = _read("diff", "--name-only", BASE_REF, cwd=path)
    untracked = _read("ls-files", "--others", "--exclude-standard", cwd=path)
    return sorted({f for f in (out + untracked).splitlines() if f})


def head(path: Path) -> str:
    return _read("rev-parse", "HEAD", cwd=path).strip()


def has_commit(path: Path, sha: str) -> bool:
    return bool(sha) and bool(_read("cat-file", "-t", sha, cwd=path).strip() == "commit")


def changed_since(path: Path, base: str) -> list[str]:
    """Files changed between `base` and HEAD."""
    return sorted(f for f in _read("diff", "--name-only", base, "HEAD", cwd=path).splitlines() if f)


def reset_to(path: Path, sha: str) -> None:
    """Throw away commits and changes after `sha` (an agent stepped outside its role)."""
    git("reset", "-q", "--hard", sha, cwd=path)
    git("clean", "-q", "-fd", cwd=path)


def clean_tree(path: Path) -> None:
    """Drop anything a test run left behind (caches, outputs) so it is never committed."""
    git("checkout", "-q", "--", ".", cwd=path, check=False)
    git("clean", "-q", "-fd", cwd=path, check=False)


def diff_size(path: Path, base: str = BASE_REF) -> int:
    """Lines added + removed since `base` (the task start by default; committed changes)."""
    total = 0
    for line in _read("diff", "--numstat", base, cwd=path).splitlines():
        added, removed, *_ = line.split("\t")
        if added.isdigit() and removed.isdigit():  # "-" for binary files
            total += int(added) + int(removed)
    return total


def diff(path: Path) -> str:
    """Unified diff of the task's changes since it started, including new files."""
    if not (path / ".git").exists():
        return ""
    parts = [_read("diff", BASE_REF, cwd=path)]
    for f in _read("ls-files", "--others", "--exclude-standard", cwd=path).splitlines():
        if f:
            parts.append(_read("diff", "--no-index", "--", "/dev/null", f, cwd=path))
    return "".join(parts)


def out_of_scope(files: list[str], globs: list[str]) -> list[str]:
    if not globs:
        return []
    return [f for f in files if not any(fnmatch.fnmatch(f, g) for g in globs)]


def merge_task(project_id: int, task_id: int, title: str) -> None:
    """Merge the task branch into the milestone branch in the main clone."""
    repo = repo_dir(project_id)
    branch = task_branch(task_id)
    # Push from the task clone rather than fetch into the main repo: the task clone is
    # owned by the container user, and git refuses to serve fetches from a repository
    # owned by someone else.
    git(
        "push",
        "--quiet",
        "--force",
        str(repo),
        f"{branch}:{branch}",
        cwd=task_dir(project_id, task_id),
    )
    proc = subprocess.run(
        ["git", *GIT_IDENTITY, "merge", "--no-ff", "-m", f"Merge task {task_id}: {title}", branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        git("merge", "--abort", cwd=repo, check=False)
        raise GitError(f"merge conflict: {proc.stdout.strip()} {proc.stderr.strip()}")


def recover_repo(project_id: int, milestone_branch: str) -> list[str]:
    """After a crash: put the main clone back into a clean state on the milestone branch.

    Only the orchestrator writes here, so anything half-done is its own interrupted work:
    a merge (aborted - the task merges again), a stale lock, uncommitted files from a
    planner write (dropped - the job is re-run).
    """
    repo = repo_dir(project_id)
    if not (repo / ".git").is_dir():
        return []
    done: list[str] = []
    lock = repo / ".git" / "index.lock"
    if lock.exists():
        lock.unlink(missing_ok=True)
        done.append("removed a stale git lock")
    if (repo / ".git" / "MERGE_HEAD").exists():
        git("merge", "--abort", cwd=repo, check=False)
        done.append("aborted an interrupted merge")
    current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo, check=False).strip()
    if current != milestone_branch:
        git("checkout", "--quiet", "--force", milestone_branch, cwd=repo, check=False)
        done.append(f"switched back to {milestone_branch}")
    if git("status", "--porcelain", "--untracked-files=no", cwd=repo, check=False).strip():
        git("reset", "--quiet", "--hard", "HEAD", cwd=repo, check=False)
        done.append("discarded uncommitted changes")
    return done


def push(project_id: int, repo_url: str, branch: str) -> None:
    git("push", "--quiet", "origin", branch, cwd=repo_dir(project_id), auth_url=repo_url)


def head_commit(project_id: int) -> str:
    return git("rev-parse", "HEAD", cwd=repo_dir(project_id)).strip()


def read_repo_file(project_id: int, rel: str) -> str:
    path = repo_dir(project_id) / rel
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def safe_rel_path(rel: str) -> str:
    """Normalise a repo-relative path; reject absolute paths and escapes."""
    from pathlib import PurePosixPath

    p = PurePosixPath(rel.strip())
    if p.is_absolute() or not p.parts or ".." in p.parts or p.parts[0] == ".git":
        raise ValueError(f"unsafe path: {rel!r}")
    return str(p)


def write_and_commit(project_id: int, files: dict[str, str], message: str) -> str:
    """Write files into the main clone (milestone branch) and commit them.

    Returns the new commit sha, or the current HEAD if nothing changed.
    """
    repo = repo_dir(project_id)
    for rel, content in files.items():
        path = repo / safe_rel_path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if content.endswith("\n") else content + "\n")
    git("add", "--", *[safe_rel_path(r) for r in files], cwd=repo)
    if not _staged(repo):
        return head_commit(project_id)
    git("commit", "-q", "-m", message, cwd=repo)
    return head_commit(project_id)


def _staged(repo: Path) -> bool:
    proc = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo)
    return proc.returncode != 0


def acceptance_dir(project_id: int) -> Path:
    return project_dir(project_id) / "acceptance"


def prepare_acceptance_clone(project_id: int, milestone_branch: str) -> Path:
    """A fresh clone of the milestone tip, for running the acceptance command."""
    import shutil

    dest = acceptance_dir(project_id)
    shutil.rmtree(dest, ignore_errors=True)
    git(
        "clone",
        "--quiet",
        "--no-hardlinks",
        "--branch",
        milestone_branch,
        str(repo_dir(project_id)),
        str(dest),
    )
    return dest


def github_repo(url: str) -> tuple[str, str] | None:
    """(owner, repo) for a GitHub URL, else None."""
    import re

    m = re.match(
        r"^(?:https://github\.com/|git@github\.com:)([^/]+)/([^/]+?)(?:\.git)?/?$", url.strip()
    )
    return (m.group(1), m.group(2)) if m else None
