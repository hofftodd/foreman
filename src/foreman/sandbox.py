"""Where executor and validation commands run.

`DockerRuntime` is the real thing: one container per step, the task clone
mounted at /workspace, the run's control directory (prompt, configs) mounted
read-only at /foreman, nothing else from the host.

Containers run on one of two networks (`StepSpec.network`, from the project):

- "isolated" (default): an internal Docker network with no route anywhere. The only
  thing on it besides executors is a relay container (`foreman-gw`) that forwards
  one port to the model gateway, so an agent can reach its model only through the
  gateway's budgets and loop detection - not the LAN, model servers or the internet.
- "open": the default bridge plus `host.docker.internal`, for projects whose tests
  need the network.

`LocalRuntime` runs the same commands as a host process group. It provides no
isolation and exists for tests and development only.

Every process started here is addressable by a *handle* string stored on the
TaskRun, so the supervisor can kill it without talking to the orchestrator:
    docker:<container name>   |   local:<process group id>
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from foreman.config import get_settings
from foreman.engine import HOST_ALIAS, detect, docker, docker_env

WORKSPACE = "/workspace"
CTL = "/foreman"
RESULTS = "/foreman-results"  # where the test runner writes junit.xml and coverage
CONTAINER_UID = 1000
NETWORKS = ("isolated", "open")
RELAY_ALIAS = "foreman-gw"
RELAY_PORT = 8701
RELAY_URL = f"http://{RELAY_ALIAS}:{RELAY_PORT}"


@dataclass
class StepSpec:
    name: str  # unique, used as the container name
    workspace: Path  # host path of the task clone
    ctl_dir: Path  # host path of the run control dir
    command: list[str]  # may reference $FOREMAN_WORKSPACE / $FOREMAN_CTL
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    image: str = ""  # the project's toolchain image; "" = FOREMAN_EXECUTOR_IMAGE
    network: str = "isolated"  # see module docstring
    # Role sandboxing (Docker only; the local runtime has no isolation):
    # the workspace mounted read-only (Tester), and workspace-relative directories
    # mounted over it read-only (Builder: the tests) or writable (Tester: the tests).
    workspace_readonly: bool = False
    readonly_dirs: list[str] = field(default_factory=list)
    writable_dirs: list[str] = field(default_factory=list)
    # Host directory for test results, mounted writable at $FOREMAN_RESULTS.
    results_dir: Path | None = None


class Proc:
    def __init__(self, handle: str, proc: asyncio.subprocess.Process):
        self.handle = handle
        self._proc = proc

    async def lines(self) -> AsyncIterator[str]:
        assert self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return
            yield line.decode(errors="replace").rstrip("\n")

    async def wait(self) -> int:
        return await self._proc.wait()


class LocalRuntime:
    name = "local"

    async def start(self, spec: StepSpec) -> Proc:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(spec.ctl_dir / "home"),
            "FOREMAN_WORKSPACE": str(spec.workspace),
            "FOREMAN_CTL": str(spec.ctl_dir),
            **({"FOREMAN_RESULTS": str(spec.results_dir)} if spec.results_dir else {}),
            **spec.env,
        }
        (spec.ctl_dir / "home").mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *spec.command,
            cwd=spec.workspace,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # own process group, so Stop kills the whole tree
        )
        return Proc(f"local:{proc.pid}", proc)


class DockerRuntime:
    name = "docker"

    def __init__(self) -> None:
        self.settings = get_settings()

    def _user(self, spec: StepSpec) -> str:
        for rel in [*spec.readonly_dirs, *spec.writable_dirs]:
            (spec.workspace / rel).mkdir(parents=True, exist_ok=True)  # mount points
        if os.getuid() == 0:
            # Never run agents as root: hand the mounts to the image's `agent` user.
            paths = [spec.workspace, spec.ctl_dir]
            if spec.results_dir:
                paths.append(spec.results_dir)
            for p in paths:
                subprocess.run(
                    ["chown", "-R", f"{CONTAINER_UID}:{CONTAINER_UID}", str(p)], check=True
                )
            return f"{CONTAINER_UID}:{CONTAINER_UID}"
        return f"{os.getuid()}:{os.getgid()}"

    def docker_args(self, spec: StepSpec) -> list[str]:
        s = self.settings
        env = {
            "HOME": "/tmp/home",
            "FOREMAN_WORKSPACE": WORKSPACE,
            "FOREMAN_CTL": CTL,
            **({"FOREMAN_RESULTS": RESULTS} if spec.results_dir else {}),
            **spec.env,
        }
        args = [
            "docker",
            "run",
            "--rm",
            "--init",
            "--name",
            spec.name,
            "--user",
            self._user(spec),
            "--cpus",
            s.container_cpus,
            "--memory",
            s.container_memory,
            "--pids-limit",
            "1024",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            *self._mounts(spec),
            "-w",
            WORKSPACE,
            "--label",
            "foreman=1",
            "--label",
            f"foreman.instance={s.instance_id}",
        ]
        if spec.network == "open":
            args += ["--add-host", f"{HOST_ALIAS}:{detect().host_ip or 'host-gateway'}"]
        else:
            args += ["--network", exec_network()]
        for k, v in spec.labels.items():
            args += ["--label", f"{k}={v}"]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        return [*args, spec.image or s.executor_image, *spec.command]

    def _mounts(self, spec: StepSpec) -> list[str]:
        ws = f"{spec.workspace}:{WORKSPACE}" + (":ro" if spec.workspace_readonly else "")
        out = ["-v", ws, "-v", f"{spec.ctl_dir}:{CTL}:ro"]
        for rel, mode in [
            *((d, "ro") for d in spec.readonly_dirs),
            *((d, "rw") for d in spec.writable_dirs),
        ]:
            out += ["-v", f"{spec.workspace / rel}:{WORKSPACE}/{rel}:{mode}"]
        if spec.results_dir:
            out += ["-v", f"{spec.results_dir}:{RESULTS}"]
        return out

    async def start(self, spec: StepSpec) -> Proc:
        proc = await asyncio.create_subprocess_exec(
            *self.docker_args(spec),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=docker_env(
                {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp")}
            ),
        )
        return Proc(f"docker:{spec.name}", proc)


Runtime = LocalRuntime | DockerRuntime


def get_runtime() -> Runtime:
    return LocalRuntime() if get_settings().runtime == "local" else DockerRuntime()


def kill_handle(handle: str, grace_s: float | None = None) -> str:
    """Terminate whatever `handle` names: SIGTERM, then SIGKILL after `grace_s`.

    Synchronous and self-contained so the supervisor can call it without any
    orchestrator state. Returns a short description of what happened.
    """
    grace = get_settings().stop_grace_s if grace_s is None else grace_s
    kind, _, ident = handle.partition(":")
    if kind == "docker" and ident:
        if not shutil.which("docker"):
            return "docker not available"
        proc = subprocess.run(
            ["docker", "stop", "-t", str(int(grace)), ident],
            capture_output=True,
            text=True,
            env=docker_env(),
        )
        if proc.returncode != 0 and "No such container" not in proc.stderr:
            subprocess.run(["docker", "kill", ident], capture_output=True, env=docker_env())
        return f"stopped container {ident}"
    if kind == "local" and ident:
        pgid = int(ident)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return "already exited"
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not group_alive(pgid):
                return f"terminated pgid {pgid}"
            time.sleep(0.1)
        # Darwin answers EPERM for a group that holds only zombies.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
        return f"killed pgid {pgid}"
    return "no handle"


def alive_in_group(pgid: int) -> list[int]:
    """Pids of the non-zombie processes in a process group. Zombies wait for their
    parent to reap them and count as dead here.

    Linux reads /proc; other platforms (macOS) ask `ps`. If neither works, falls back
    to probing the group with signal 0, which cannot tell zombies apart."""
    proc = Path("/proc")
    if proc.is_dir():
        alive = []
        for d in proc.iterdir():
            if not d.name.isdigit():
                continue
            try:
                fields = (d / "stat").read_text().rsplit(")", 1)[1].split()
            except (OSError, IndexError):
                continue
            if int(fields[2]) == pgid and fields[0] != "Z":
                alive.append(int(d.name))
        return alive
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,pgid=,stat="], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            # Darwin: EPERM when the group holds only zombies.
            return []
        return [pgid]
    alive = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        if int(parts[1]) == pgid and not parts[2].startswith("Z"):
            alive.append(int(parts[0]))
    return alive


def group_alive(pgid: int) -> bool:
    """True if any non-zombie process is in the group."""
    return bool(alive_in_group(pgid))


def running_containers() -> dict[str, str]:
    """This instance's running containers: name -> run id label."""
    if not shutil.which("docker"):
        return {}
    instance = get_settings().instance_id
    proc = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=foreman.instance={instance}",
            "--format",
            '{{.Names}}\t{{.Label "foreman.run"}}',
        ],
        capture_output=True,
        text=True,
        env=docker_env(),
    )
    if proc.returncode != 0:
        return {}
    out = {}
    for line in proc.stdout.splitlines():
        name, _, run = line.partition("\t")
        out[name] = run
    return out


# -- isolated executor network ----------------------------------------------


class IsolationError(RuntimeError):
    pass


def exec_network() -> str:
    return f"foreman-exec-{get_settings().instance_id}"


def relay_name() -> str:
    return f"foreman-gw-{get_settings().instance_id}"


def ensure_exec_network() -> None:
    """Create the internal executor network if it doesn't exist."""
    name = exec_network()
    if docker("network", "inspect", name).returncode == 0:
        return
    s = get_settings()
    proc = docker(
        "network", "create", "--internal",
        "--label", "foreman=1", "--label", f"foreman.instance={s.instance_id}",
        name,
    )  # fmt: skip
    if proc.returncode != 0 and "already exists" not in proc.stderr:
        raise IsolationError(f"cannot create network {name}: {proc.stderr.strip()}")


def ensure_relay(gateway_port: int) -> None:
    """Make sure the gateway relay runs and forwards to `gateway_port`.

    The relay sits on the default bridge (so it can reach the host) and on the
    executor network as `foreman-gw`. It carries no `foreman.run` label, so the
    supervisor's orphan sweep and Stop leave it alone; it holds no state."""
    ensure_exec_network()
    name = relay_name()
    proc = docker(
        "inspect", "-f", '{{.State.Running}} {{index .Config.Labels "foreman.gateway_port"}}', name
    )
    if proc.returncode == 0:
        if proc.stdout.split() == ["true", str(gateway_port)]:
            return
        docker("rm", "-f", name)
    s = get_settings()
    eng = detect()
    proc = docker(
        "run", "-d", "--name", name,
        "--user", f"{CONTAINER_UID}:{CONTAINER_UID}",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", "64m", "--pids-limit", "64",
        "--add-host", f"{HOST_ALIAS}:{eng.host_ip or 'host-gateway'}",
        "--label", "foreman=1", "--label", f"foreman.instance={s.instance_id}",
        "--label", "foreman.role=gateway-relay",
        "--label", f"foreman.gateway_port={gateway_port}",
        s.executor_image,
        "socat", f"TCP-LISTEN:{RELAY_PORT},fork,reuseaddr",
        f"TCP:{HOST_ALIAS}:{gateway_port}",
        timeout=60,
    )  # fmt: skip
    if proc.returncode != 0:
        raise IsolationError(f"cannot start the gateway relay: {proc.stderr.strip()[:300]}")
    proc = docker("network", "connect", "--alias", RELAY_ALIAS, exec_network(), name)
    if proc.returncode != 0 and "already exists" not in proc.stderr:
        docker("rm", "-f", name)
        raise IsolationError(f"cannot attach the relay: {proc.stderr.strip()[:300]}")


def teardown_isolation() -> None:
    """Remove the relay and the executor network (best effort; on orchestrator exit).
    The network stays if executor containers are still attached to it."""
    docker("rm", "-f", relay_name())
    docker("network", "rm", exec_network())
