"""Runtime settings, read from FOREMAN_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"FOREMAN_{name}", default)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")).resolve())
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8700")))
    # Optional static bearer token for the API. Empty = no auth (LAN-only use).
    token: str = field(default_factory=lambda: _env("TOKEN", ""))
    # Container runtime for executors: "docker" (default) or "local" (tests/dev only,
    # no isolation).
    runtime: str = field(default_factory=lambda: _env("RUNTIME", "docker"))
    executor_image: str = field(
        default_factory=lambda: _env("EXECUTOR_IMAGE", "foreman/executor-base:latest")
    )
    container_cpus: str = field(default_factory=lambda: _env("CONTAINER_CPUS", "2"))
    container_memory: str = field(default_factory=lambda: _env("CONTAINER_MEMORY", "4g"))
    # Seconds between SIGTERM and SIGKILL when stopping an executor.
    stop_grace_s: int = field(default_factory=lambda: int(_env("STOP_GRACE_S", "5")))
    # Used only by the orchestrator for clone/fetch/push; never passed to executors.
    github_token: str = field(default_factory=lambda: _env("GITHUB_TOKEN", ""))
    poll_interval_s: float = field(default_factory=lambda: float(_env("POLL_INTERVAL_S", "0.5")))
    # Push notifications for the inbox: an ntfy topic URL, e.g. https://ntfy.sh/my-foreman
    ntfy_url: str = field(default_factory=lambda: _env("NTFY_URL", ""))
    # Where the dashboard is reachable, for links in notifications.
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL", ""))
    # Model gateway: executors reach their model through the orchestrator. "auto" binds
    # where containers arrive (docker bridge on Linux, loopback on macOS); see
    # foreman.engine.
    gateway_host: str = field(default_factory=lambda: _env("GATEWAY_HOST", "auto"))
    gateway_port: int = field(default_factory=lambda: int(_env("GATEWAY_PORT", "8701")))
    # Docker socket for every docker command. Empty: DOCKER_HOST, else the managed
    # Lima VM's socket if it exists, else the docker CLI's default context.
    docker_host: str = field(default_factory=lambda: _env("DOCKER_HOST", ""))
    # What `host.docker.internal` maps to inside containers ("auto": host-gateway, or
    # Lima's host address on Lima).
    container_host_ip: str = field(default_factory=lambda: _env("CONTAINER_HOST_IP", "auto"))
    # Managed Lima VM size (macOS); 0 = derived from this machine.
    vm_cpus: int = field(default_factory=lambda: int(_env("VM_CPUS", "0")))
    vm_memory_gib: int = field(default_factory=lambda: int(_env("VM_MEMORY_GIB", "0")))
    # Web search for the Project Planning Agent on local models: a SearXNG URL (JSON
    # output enabled). Claude nodes use Anthropic's own search and need nothing here.
    search_url: str = field(default_factory=lambda: _env("SEARCH_URL", ""))
    # How often the orchestrator checks each node's health.
    node_check_s: float = field(default_factory=lambda: float(_env("NODE_CHECK_S", "60")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "foreman.db"

    @property
    def instance_id(self) -> str:
        """Distinguishes this installation's containers from another's on the same host."""
        import hashlib

        return hashlib.sha256(str(self.data_dir).encode()).hexdigest()[:12]

    @property
    def control_db_path(self) -> Path:
        return self.data_dir / "control.db"

    @property
    def heartbeat_dir(self) -> Path:
        return self.data_dir / "heartbeats"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"


def get_settings() -> Settings:
    return Settings()
