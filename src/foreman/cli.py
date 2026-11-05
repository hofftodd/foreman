"""`foreman` command line.

foreman up            run api + orchestrator + supervisor (separate processes)
foreman api           HTTP API and dashboard
foreman orchestrator  scheduler and task runner
foreman supervisor    stop / E-stop enforcement
foreman build-image   build the executor base image (--with-aider adds Aider)
foreman claude-nodes  add Claude Opus / Sonnet / Haiku nodes from $ANTHROPIC_API_KEY
                      (--project ID also assigns them to that project's roles)
foreman vm ACTION     macOS: the Lima VM containers run in (start|stop|status|shell|delete)
foreman doctor        check that this host can run executors
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

from foreman.config import get_settings


def _api(args: argparse.Namespace) -> None:
    import uvicorn

    from foreman.api.app import create_app

    s = get_settings()
    uvicorn.run(create_app(), host=s.host, port=s.port, log_level="info")


def _orchestrator(args: argparse.Namespace) -> None:
    from foreman.orchestrator.main import main

    main()


def _supervisor(args: argparse.Namespace) -> None:
    from foreman.supervisor.main import main

    main()


def _up(args: argparse.Namespace) -> None:
    """Run the three components as independent OS processes.

    They are separate on purpose: a hung orchestrator must not be able to block
    the supervisor from executing a Stop.
    """
    procs = {
        name: subprocess.Popen([sys.executable, "-m", "foreman.cli", name])
        for name in ("supervisor", "orchestrator", "api")
    }

    def shutdown(*_: object) -> None:
        for p in procs.values():
            p.terminate()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        while all(p.poll() is None for p in procs.values()):
            time.sleep(0.5)
    finally:
        shutdown()
        for p in procs.values():
            p.wait(timeout=15)
    dead = [n for n, p in procs.items() if p.returncode not in (0, -signal.SIGTERM)]
    if dead:
        sys.exit(f"component(s) exited unexpectedly: {', '.join(dead)}")


def _build_image(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parents[2]
    ctx = root / "docker" / "executor-base"
    tag = get_settings().executor_image
    from foreman.engine import docker_env

    extra = ["--build-arg", "WITH_AIDER=1"] if args.with_aider else []
    sys.exit(subprocess.call(["docker", "build", *extra, "-t", tag, str(ctx)], env=docker_env()))


def _claude_nodes(args: argparse.Namespace) -> None:
    import os

    from foreman.claude_setup import NODES, setup

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        sys.exit("set ANTHROPIC_API_KEY first (it is stored with the nodes, not printed)")
    try:
        nodes = setup(key, project_id=args.project)
    except ValueError as e:
        sys.exit(str(e))
    for spec in NODES:
        n = nodes[spec.roles[0]]
        print(
            f"{n.name:<14} {spec.model:<18} {', '.join(spec.roles):<30} capacity {n.capacity}, "
            f"${n.price_in:g} / ${n.price_out:g} per M tokens in / out"
        )
    if args.project is not None:
        print(f"project {args.project}: every agent role now uses these nodes")
    else:
        print("assign them under a project's Settings, or rerun with --project ID")


def _vm(args: list[str]) -> None:
    from foreman.vm import main as vm_main

    vm_main(args)


def _doctor(args: list[str]) -> None:
    from foreman.doctor import main as doctor_main

    doctor_main(args)


# Commands that take further arguments.
ARG_COMMANDS = {"vm": _vm, "doctor": _doctor}

COMMANDS = {
    "up": _up,
    "api": _api,
    "orchestrator": _orchestrator,
    "supervisor": _supervisor,
    "build-image": _build_image,
    "claude-nodes": _claude_nodes,
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="foreman")
    parser.add_argument("command", choices=sorted([*COMMANDS, *ARG_COMMANDS]))
    parser.add_argument("args", nargs="*")
    parser.add_argument("--with-aider", action="store_true", help="build-image: include Aider")
    parser.add_argument("--project", type=int, help="claude-nodes: project to assign roles in")
    args = parser.parse_args()
    if args.command in ARG_COMMANDS:
        ARG_COMMANDS[args.command](args.args)
        return
    if args.args:
        parser.error(f"{args.command} takes no arguments")
    COMMANDS[args.command](args)


if __name__ == "__main__":
    main()
