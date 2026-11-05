"""Executor containers (validation.md V4.1, V4.4, V8.1 with Docker).

Needs a Docker daemon and the foreman/executor-base image:
    foreman build-image
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest

from foreman.core.models import TaskState
from foreman.engine import docker
from foreman.orchestrator.main import Orchestrator
from foreman.sandbox import running_containers
from tests.helpers import TERMINAL, drive, make_project, make_task, task, wait_for

IMAGE = os.environ.get("FOREMAN_TEST_IMAGE", "foreman/executor-base:latest")


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    # The same engine Foreman uses (e.g. the Lima VM on macOS), not just the CLI default.
    return docker("image", "inspect", IMAGE).returncode == 0


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_ready(), reason=f"docker or image {IMAGE} not available"),
]


@pytest.fixture(autouse=True)
def docker_runtime(monkeypatch):
    monkeypatch.setenv("FOREMAN_RUNTIME", "docker")
    monkeypatch.setenv("FOREMAN_EXECUTOR_IMAGE", IMAGE)
    monkeypatch.setenv("FOREMAN_GITHUB_TOKEN", "ghp_must_not_leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    yield
    from foreman.config import get_settings
    from foreman.sandbox import teardown_isolation

    teardown_isolation()  # each test's data dir is its own instance
    images = subprocess.run(
        [
            "docker",
            "images",
            "-q",
            "--filter",
            f"reference=foreman/project-{get_settings().instance_id}-*",
        ],
        capture_output=True,
        text=True,
    ).stdout.split()
    if images:
        subprocess.run(["docker", "rmi", "-f", *images], capture_output=True)


PROBE = """
id -u > probe_uid
pwd > probe_pwd
env | sort > probe_env
cat /proc/mounts > probe_mounts
echo 'VALUE = 42' > src/app.py
"""


async def test_task_runs_in_isolated_container(client, origin, env):
    p = make_project(client, origin)
    t = make_task(client, p["id"], script=PROBE, validation="grep -q 42 src/app.py && id -u")
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL, timeout=120)
    assert task(t["id"]).state == TaskState.DONE, task(t["id"]).reason

    from foreman import gitops

    repo = gitops.repo_dir(p["id"])
    assert (repo / "src" / "app.py").read_text() == "VALUE = 42\n"
    assert (repo / "probe_uid").read_text().strip() not in ("", "0")  # never root
    assert (repo / "probe_pwd").read_text().strip() == "/workspace"
    env_dump = (repo / "probe_env").read_text()
    assert "must_not_leak" not in env_dump and "must-not-leak" not in env_dump  # NFR-5
    mounts = (repo / "probe_mounts").read_text()
    # Only the task clone and the read-only control dir come from the host.
    assert str(env) not in mounts
    assert " /workspace " in mounts and " /foreman " in mounts
    ctl_line = next(line for line in mounts.splitlines() if " /foreman " in line)
    assert " ro," in ctl_line or ctl_line.split()[3].startswith("ro")


def test_stop_kills_container_within_10s(client, origin):
    import sys

    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "foreman.cli", c],
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for c in ("supervisor", "orchestrator")
    ]
    try:
        p = make_project(client, origin)
        t = make_task(client, p["id"], script="sleep 300 & sleep 300 & wait")

        def container() -> str | None:
            names = [n for n, run in running_containers().items() if run]
            return names[0] if names else None

        wait_for(lambda: container() is not None, timeout=60)
        name = container()
        t0 = time.monotonic()
        client.post("/api/controls", json={"scope": "task", "target_id": t["id"], "action": "stop"})
        wait_for(lambda: name not in running_containers(), timeout=10)
        assert time.monotonic() - t0 < 10
        wait_for(lambda: task(t["id"]).state == TaskState.STOPPED_BY_USER)
    finally:
        for proc in procs:
            proc.terminate()
            proc.wait(10)


@pytest.mark.parametrize(
    ("kind", "provider"), [("pi", "openai"), ("opencode", "openai"), ("opencode", "anthropic")]
)
async def test_executor_against_fake_model(client, origin, kind, provider):
    """A real agent (pi / OpenCode) in the executor container, on the isolated network,
    driven by a scripted model server through the gateway relay, speaking the OpenAI
    chat API or Claude's Messages API."""
    from sqlmodel import select

    from foreman.core.models import Event
    from foreman.db import session
    from tests.fakellm import MODEL, FakeLLM

    llm = FakeLLM().start()
    try:
        node = {"name": "fake", "base_url": llm.url}
        model = MODEL
        if provider == "anthropic":
            node = {
                "name": "claude",
                "provider": "anthropic",
                "api_key": "sk-host-only",
                "base_url": llm.url.removesuffix("/v1"),
            }
            model = "claude-haiku-5-5"
        n = client.post("/api/nodes", json=node).json()
        p = make_project(
            client,
            origin,
            executor_kind=kind,
            role_bindings={"executor": {"node_id": n["id"], "model": model}},
        )
        from foreman import gitops
        from tests.test_pipeline import RUN_SH

        gitops.write_and_commit(p["id"], {"tests/run.sh": RUN_SH}, "test runner")
        t = make_task(
            client,
            p["id"],
            script="",
            title="Create hello.txt",
            criteria=["hello.txt mentions the fake model"],
        )
        await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL, timeout=240)
        with session() as s:
            events = s.exec(select(Event).where(Event.task_id == t["id"])).all()
        log = "\n".join(e.message for e in events)
        assert task(t["id"]).state == TaskState.DONE, f"{task(t['id']).reason}\n{log[-3000:]}"
        repo = gitops.repo_dir(p["id"])
        assert "fake model" in (repo / "hello.txt").read_text()  # the Builder's work
        assert (repo / f"tests/check_t{t['id']}.sh").exists() or any(
            f.name.startswith("check_") for f in (repo / "tests").iterdir()
        )  # the Tester's
        prompts = json.dumps(llm.requests)
        assert "Create hello.txt" in prompts  # the context packs reached the model
        assert "You are the Tester Agent" in prompts and "You are the Builder Agent" in prompts
        if provider == "anthropic":
            # Native Messages API, with the key added by the gateway on the host.
            dump = os.environ.get("FOREMAN_DUMP_REQUESTS")
            if dump:
                with open(dump, "w") as f:
                    json.dump({"requests": llm.requests, "headers": llm.headers}, f, default=str)
            assert any("tools" in r for r in llm.requests)
            assert all(h.get("x-api-key") == "sk-host-only" for h in llm.headers)
    finally:
        llm.stop()


@pytest.mark.parametrize("kind", ["pi", "opencode"])
async def test_spec_to_done_with_agent(client, origin, kind):
    """E2E-1: spec chat -> approve -> decomposition -> plan approval -> OpenCode in a
    container implements the task -> validation -> merged. One fake model plays both
    the planner and the coding agent."""
    from sqlmodel import select

    from foreman import gitops
    from foreman.core.models import ProjectPhase, Task
    from foreman.db import session
    from tests.fakellm import MODEL, FakeLLM, system_text
    from tests.helpers import project

    spec_dir = "specs/001-initial"
    chat = (
        f"<<<FILE {spec_dir}/requirements.md\n- FR-1 A hello file mentions the fake model.\n>>>\n"
        f"<<<FILE {spec_dir}/design.md\nOne file.\n>>>\n"
        f"<<<FILE {spec_dir}/acceptance-criteria.md\n- AC-1 (FR-1) The file exists.\n>>>\nDone."
    )
    tasks = [
        {
            "key": "T1",
            "title": "Create hello.txt",
            "goal": "Write the hello file.",
            "req_refs": ["FR-1"],
            "criteria": ["The hello file mentions the fake model"],
            "deps": [],
        }
    ]

    def planner(req):
        if "break the project into small, ordered tasks" in system_text(req):
            return json.dumps(tasks)
        return chat

    llm = FakeLLM(planner=planner).start()
    try:
        planner_node = client.post("/api/nodes", json={"name": "p", "base_url": llm.url}).json()
        exec_node = client.post("/api/nodes", json={"name": "e", "base_url": llm.url}).json()
        planning = {"node_id": planner_node["id"], "model": MODEL}
        coding = {"node_id": exec_node["id"], "model": MODEL}
        p = client.post(
            "/api/projects",
            json={
                "name": "hello",
                "repo_url": str(origin),
                "executor_kind": kind,
                "gates": {"milestone": False},
                "role_bindings": {
                    "project_planner": planning,
                    "task_planner": planning,
                    "tester": coding,
                    "builder": coding,
                },
            },
        ).json()
        pid = p["id"]
        # The fake model writes one file per step, so the test runner is there already
        # (in a real project the Tester writes it in the setup task).
        from tests.test_pipeline import RUN_SH

        gitops.write_and_commit(pid, {"tests/run.sh": RUN_SH}, "test runner")
        orch = Orchestrator()
        client.post(f"/api/projects/{pid}/spec/messages", json={"content": "hello file"})
        await drive(
            orch,
            lambda: bool(client.get(f"/api/projects/{pid}/spec").json()["docs"]["acceptance"]),
        )
        assert client.post(f"/api/projects/{pid}/spec/approve").status_code == 200
        await drive(orch, lambda: project(pid).phase == ProjectPhase.PLAN_REVIEW)
        assert client.post(f"/api/projects/{pid}/plan/approve").status_code == 200
        with session() as s:
            planned = s.exec(select(Task).where(Task.project_id == pid)).all()
        assert [t.key for t in planned] == ["SETUP", "ACCEPT", "T1", "DOCS"]
        await drive(orch, lambda: project(pid).phase == ProjectPhase.DONE, timeout=600)
        assert all(task(t.id).state == TaskState.DONE for t in planned)
        repo = gitops.repo_dir(pid)
        assert "fake model" in (repo / "hello.txt").read_text()
        assert (repo / "tests/acceptance/run.sh").exists()  # the Tester's definition of done
        assert project(pid).acceptance_passed
        # The agents' prompts were the role packs, with the requirement slice.
        exec_prompts = json.dumps([r for r in llm.requests if r.get("tools")])
        assert "FR-1 A hello file" in exec_prompts
    finally:
        llm.stop()


def _toolchain(nonce: str, extra: str = "") -> str:
    # The nonce makes each test run build afresh instead of reusing an earlier image.
    return (
        "FROM foreman/executor-base\n"
        f"# test {nonce}\n"
        "USER root\n"
        "RUN echo toolchain-ok > /opt/toolchain\n"
        f"{extra}"
        "USER agent\n"
    )


def _remove_project_images() -> None:
    out = docker("images", "-q", "--filter", "label=foreman=1", "foreman/project-*").stdout.split()
    if out:
        docker("rmi", "-f", *out)


async def test_tasks_run_in_the_project_toolchain_image(client, origin, tmp_path):
    from sqlmodel import select

    from foreman.core.models import Event, TaskRun
    from foreman.db import session
    from tests.test_images import add_to_origin

    add_to_origin(origin, tmp_path, {".foreman/Dockerfile": _toolchain(str(time.time_ns()))})
    p = make_project(client, origin)
    try:
        a = make_task(
            client, p["id"], "cp /opt/toolchain out.txt", validation="grep -q ok /opt/toolchain"
        )
        b = make_task(client, p["id"], "echo b > b.txt", validation="test -f /opt/toolchain")
        await drive(
            Orchestrator(),
            lambda: {task(a["id"]).state, task(b["id"]).state} <= TERMINAL,
            timeout=300,
        )
        assert task(a["id"]).state == TaskState.DONE, task(a["id"]).reason
        assert task(b["id"]).state == TaskState.DONE, task(b["id"]).reason
        with session() as s:
            builds = s.exec(
                select(Event).where(Event.kind == "log", Event.message.contains("toolchain-ok"))
            ).all()
            runs = s.exec(select(TaskRun)).all()
        # Built once (the second task reuses the image), and both runs used it.
        assert {e.task_id for e in builds} == {a["id"]}
        assert len(runs) == 2
    finally:
        _remove_project_images()


async def test_broken_toolchain_fails_as_configuration(client, origin, tmp_path):
    from sqlmodel import select

    from foreman.core.models import TaskRun
    from foreman.db import session
    from tests.test_images import add_to_origin

    broken = _toolchain(str(time.time_ns()), "RUN exit 7\n")
    add_to_origin(origin, tmp_path, {".foreman/Dockerfile": broken})
    p = make_project(client, origin)
    try:
        t = make_task(client, p["id"], "echo never > ran.txt")
        await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL, timeout=300)
        assert task(t["id"]).state == TaskState.FAILED
        assert "building .foreman/Dockerfile failed" in task(t["id"]).reason
        with session() as s:
            run = s.exec(select(TaskRun)).one()
        assert run.failure_kind == "config"
    finally:
        _remove_project_images()


async def test_toolchain_must_derive_from_base(client, origin):
    from foreman import gitops

    p = make_project(client, origin, watchdog={})
    gitops.write_and_commit(p["id"], {".foreman/Dockerfile": "FROM ubuntu:24.04\n"}, "bad")
    t = make_task(client, p["id"], script="true")
    await drive(Orchestrator(), lambda: task(t["id"]).state == TaskState.BLOCKED, timeout=60)
    assert "must build FROM foreman/executor-base" in task(t["id"]).reason


# -- role sandboxes (Tester / Builder) ----------------------------------------------


async def test_pipeline_red_to_green_in_containers(client, origin):
    from tests.test_pipeline import add_task, scripts, test_runs_of

    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts())
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL, timeout=180)
    assert task(t["id"]).state == TaskState.DONE, task(t["id"]).reason
    red, green = test_runs_of(t["id"])
    assert red.failed == 1 and green.passed == 1
    assert green.changed_coverage == 100.0  # coverage paths from the container resolved


async def test_builder_cannot_write_tests_inside_its_container(client, origin):
    from foreman import gitops
    from tests.test_pipeline import BUILDER, add_task, scripts

    p = make_project(client, origin)
    cheat = BUILDER + "echo true > tests/check_greet.sh || exit 3\n"
    t = add_task(client, p["id"], scripts(builder=cheat))
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL, timeout=180)
    row = task(t["id"])
    # The write itself failed (read-only mount): the fence never had to undo anything.
    assert row.state == TaskState.FAILED and "exited with status 3" in row.reason
    clone = gitops.task_dir(p["id"], t["id"])
    assert "sh greet.sh bob" in (clone / "tests/check_greet.sh").read_text()


async def test_tester_cannot_write_code_inside_its_container(client, origin):
    from tests.test_pipeline import TESTER, add_task, scripts

    p = make_project(client, origin)
    t = add_task(client, p["id"], scripts(tester=TESTER + "echo x > greet.sh || exit 4\n"))
    await drive(Orchestrator(), lambda: task(t["id"]).state in TERMINAL, timeout=180)
    row = task(t["id"])
    assert row.state == TaskState.FAILED and "exited with status 4" in row.reason


def test_isolated_network_reaches_only_the_gateway(tmp_path):
    """Egress lockdown: on the isolated network an executor reaches the model gateway
    through the relay, and nothing else - not the host, the LAN or the internet."""
    from foreman import doctor, engine
    from foreman.engine import docker_env
    from foreman.sandbox import RELAY_URL, DockerRuntime, StepSpec, ensure_relay

    eng = engine.detect()
    server, port = doctor._serve(eng.bind_host)  # stands in for the gateway
    ws, ctl = tmp_path / "ws", tmp_path / "ctl"
    ws.mkdir()
    ctl.mkdir()

    def run(script: str, network: str = "isolated") -> subprocess.CompletedProcess[str]:
        spec = StepSpec(
            name=f"foreman-egress-{time.time_ns() % 10**9}",
            workspace=ws,
            ctl_dir=ctl,
            command=["sh", "-c", script],
            network=network,
        )
        return subprocess.run(
            DockerRuntime().docker_args(spec),
            capture_output=True,
            text=True,
            env=docker_env(),
            timeout=120,
        )

    try:
        ensure_relay(port)
        through_relay = run(f"curl -fsS --max-time 10 {RELAY_URL}/")
        assert through_relay.returncode == 0 and "foreman-doctor" in through_relay.stdout, (
            through_relay.stderr
        )
        for url in (
            f"http://host.docker.internal:{port}/",  # the host directly
            "http://1.1.1.1/",  # anything by IP
            "https://example.com/",  # anything by name
        ):
            r = run(f"curl -fsS --max-time 5 {url}")
            assert r.returncode != 0, f"{url} was reachable from the isolated network"
        # An "open" project keeps today's behaviour.
        assert (
            run(f"curl -fsS --max-time 10 http://host.docker.internal:{port}/", "open").returncode
            == 0
        )
    finally:
        server.shutdown()


async def test_pi_stuck_in_a_loop_is_stopped_by_the_watchdog(client, origin):
    """pi exits 0 whatever happens, so a run the gateway trips must still fail, and the
    agent must be killed rather than allowed to keep calling the model."""
    import uuid

    from tests.fakellm import MODEL, FakeLLM

    class Looping(FakeLLM):
        def decide(self, req):
            call = {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "ls"})},
            }
            return {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"

    llm = Looping().start()
    try:
        n = client.post("/api/nodes", json={"name": "loop", "base_url": llm.url}).json()
        p = make_project(
            client,
            origin,
            executor_kind="pi",
            role_bindings={"executor": {"node_id": n["id"], "model": MODEL}},
            watchdog={"max_retries": 0},
        )
        t = make_task(client, p["id"], script="", title="Loop forever", validation="true")
        settled = (*TERMINAL, TaskState.BLOCKED)
        await drive(Orchestrator(), lambda: task(t["id"]).state in settled, timeout=180)
        reason = task(t["id"]).reason
        assert task(t["id"]).state == TaskState.BLOCKED, reason  # no retries: needs you
        assert "same response" in reason, reason
        assert len(llm.requests) < 30  # tripped early, not after a long burn
    finally:
        llm.stop()
