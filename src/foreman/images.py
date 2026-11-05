"""Per-project toolchain images (FR: `.foreman/Dockerfile`, FROM foreman/executor-base).

A project that needs a language runtime adds it in `.foreman/Dockerfile`. Before a
task attempt or an acceptance check, the orchestrator builds that file into an image
and runs every container of that step from it; projects without one use the base
image.

The Dockerfile must build FROM the executor base image (checked before building), so
agents keep running as the unprivileged `agent` user with the executor tools.

Images are tagged by a hash of the Dockerfile, the files it COPYs/ADDs and the base
image id, so a build happens once per change of any of them and is reused otherwise.
Only the `.foreman/` directory is sent as build context: the rest of the repository
(and anything on the host) stays out of the build.

The build runs as a host process group whose handle (`local:<pgid>`) is recorded like
a container's, so Stop / E-stop kill a build the same way they kill an agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
from collections.abc import Callable
from pathlib import Path

from foreman.config import get_settings
from foreman.engine import docker, docker_env
from foreman.sandbox import Proc, kill_handle

DOCKERFILE = ".foreman/Dockerfile"
BUILD_TIMEOUT_S = 1800
BUILD_TAIL = 40

# One build per tag at a time: a second step waiting for the same image reuses it.
_locks: dict[str, asyncio.Lock] = {}


class ImageBuildError(RuntimeError):
    def __init__(self, reason: str, output: str = ""):
        super().__init__(reason)
        self.output = output


def _base_repo(image: str) -> str:
    """'foreman/executor-base:latest' -> 'foreman/executor-base'."""
    name = image.rsplit("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    return name.rsplit(":", 1)[0] if ":" in last else name


def check_dockerfile(text: str) -> None:
    """The final stage must start from the executor base image."""
    froms = [ln.split() for ln in text.splitlines() if ln.strip().upper().startswith("FROM ")]
    if not froms:
        raise ImageBuildError(f"{DOCKERFILE} has no FROM line")
    final = [t for t in froms[-1][1:] if not t.startswith("--")]
    base = _base_repo(get_settings().executor_image)
    if not final or _base_repo(final[0]) != base:
        raise ImageBuildError(
            f"{DOCKERFILE} must build FROM {base} (got {final[0] if final else '?'}), "
            "so agents keep running as the unprivileged agent user"
        )


def copy_sources(text: str) -> list[str]:
    """Build-context paths referenced by COPY/ADD (excluding --from= stages)."""
    out: list[str] = []
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or parts[0].upper() not in ("COPY", "ADD"):
            continue
        rest = parts[1].strip()
        flags = []
        while rest.startswith("--"):
            flag, _, rest = rest.partition(" ")
            flags.append(flag)
            rest = rest.strip()
        try:
            # JSON form: COPY ["src", "dest"]; otherwise shell-style words.
            args = json.loads(rest) if rest.startswith("[") else shlex.split(rest)
        except (ValueError, json.JSONDecodeError):
            continue
        args = [*flags, *[str(a) for a in args]]
        if any(a.startswith("--from") for a in args):
            continue
        out += [a for a in args[:-1] if not a.startswith("--")]
    return out


def _sources_digest(context: Path, dockerfile: str) -> str:
    """Hash of the files the Dockerfile copies in, so changing one triggers a rebuild."""
    h = hashlib.sha256()
    for src in sorted(copy_sources(dockerfile)):
        paths = sorted(context.glob(src)) if re.search(r"[*?\[]", src) else [context / src]
        for path in paths:
            for f in sorted(path.rglob("*")) if path.is_dir() else [path]:
                if f.is_file():
                    h.update(f.relative_to(context).as_posix().encode())
                    h.update(f.read_bytes())
    return h.hexdigest()


def project_dockerfile(workspace: Path) -> str | None:
    path = workspace / DOCKERFILE
    return path.read_text() if path.is_file() else None


def base_image_id(base: str) -> str:
    proc = docker("image", "inspect", "-f", "{{.Id}}", base)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def image_tag(project_id: int, dockerfile: str, base_id: str, sources: str = "") -> str:
    digest = hashlib.sha256(f"{base_id}\n{sources}\n{dockerfile}".encode()).hexdigest()[:16]
    return f"foreman/project-{get_settings().instance_id}-{project_id}:{digest}"


def image_exists(tag: str) -> bool:
    return docker("image", "inspect", tag).returncode == 0


async def ensure_image(
    project_id: int,
    workspace: Path,
    *,
    on_output: Callable[[str], None] | None = None,
    on_handle: Callable[[str], None] | None = None,
) -> str:
    """The image to run `workspace`'s steps in: "" (the base image) when the project has
    no Dockerfile, else its toolchain image, built first if needed."""
    dockerfile = project_dockerfile(workspace)
    if dockerfile is None:
        return ""
    check_dockerfile(dockerfile)
    base = get_settings().executor_image
    sources = await asyncio.to_thread(_sources_digest, workspace / ".foreman", dockerfile)
    base_id = await asyncio.to_thread(base_image_id, base)
    tag = image_tag(project_id, dockerfile, base_id, sources)
    async with _locks.setdefault(tag, asyncio.Lock()):
        if await asyncio.to_thread(image_exists, tag):
            return tag
        await _build(tag, workspace / ".foreman", on_output, on_handle)
    return tag


async def _build(
    tag: str,
    context: Path,
    on_output: Callable[[str], None] | None,
    on_handle: Callable[[str], None] | None,
) -> None:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "build",
        "--progress=plain",
        "--label",
        "foreman=1",
        "--label",
        f"foreman.instance={get_settings().instance_id}",
        "-t",
        tag,
        "-f",
        str(context / "Dockerfile"),
        str(context),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        env=docker_env(),
        start_new_session=True,  # own process group, so Stop kills the whole build
    )
    build = Proc(f"local:{proc.pid}", proc)
    if on_handle:
        on_handle(build.handle)
    tail: list[str] = []

    async def pump() -> int:
        async for line in build.lines():
            tail[:] = [*tail[-(BUILD_TAIL - 1) :], line]
            if on_output:
                on_output(line)
        return await build.wait()

    try:
        code = await asyncio.wait_for(pump(), BUILD_TIMEOUT_S)
    except TimeoutError:
        await asyncio.to_thread(kill_handle, build.handle)
        raise ImageBuildError(
            f"building {DOCKERFILE} timed out after {BUILD_TIMEOUT_S}s", "\n".join(tail)
        ) from None
    if code != 0:
        raise ImageBuildError(f"building {DOCKERFILE} failed (exit {code})", "\n".join(tail))
