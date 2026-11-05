"""`foreman claude-nodes`: run every role on Claude, e.g. to try Foreman before
local models are set up.

Creates (or updates) one node per model so each has its own price and capacity,
and assigns the agents:

    claude-opus    claude-opus-5-5    Project Planner, Task Planner
    claude-sonnet  claude-sonnet-5-5  Tester, escalation (stuck tasks)
    claude-haiku   claude-haiku-5-5   Builder

All three share the API key, which stays on this machine: the planner calls
Claude directly and agents reach it only through the model gateway.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import select

from foreman.core.models import Node, Project
from foreman.db import session

API_ROOT = "https://api.anthropic.com"


@dataclass(frozen=True)
class ClaudeNode:
    name: str
    model: str
    roles: tuple[str, ...]
    price_in: float  # USD per million tokens
    price_out: float
    capacity: int


# Anthropic list prices (Haiku 5.5's apply to prompts up to 100K tokens; task
# context packs stay far below that).
NODES = (
    ClaudeNode("claude-opus", "claude-opus-5-5", ("project_planner", "task_planner"), 4.0, 20.0, 2),
    ClaudeNode("claude-sonnet", "claude-sonnet-5-5", ("tester", "escalation"), 2.0, 10.0, 2),
    ClaudeNode("claude-haiku", "claude-haiku-5-5", ("builder",), 0.10, 0.50, 4),
)


def setup(
    api_key: str,
    *,
    base_url: str = API_ROOT,
    context_window: int = 1_000_000,
    project_id: int | None = None,
) -> dict[str, Node]:
    """Create or update the Claude nodes; optionally bind a project's roles to them.

    Returns the nodes by role (project_planner, task_planner, tester, builder, escalation).
    """
    if not api_key:
        raise ValueError("an Anthropic API key is required")
    out: dict[str, Node] = {}
    with session() as s:
        for spec in NODES:
            node = s.exec(select(Node).where(Node.name == spec.name)).first() or Node(
                name=spec.name, base_url=base_url
            )
            node.provider, node.base_url, node.api_key = "anthropic", base_url, api_key
            node.price_in, node.price_out = spec.price_in, spec.price_out
            node.capacity, node.context_window = spec.capacity, context_window
            node.models = sorted({*node.models, spec.model})
            s.add(node)
            s.commit()
            s.refresh(node)
            for role in spec.roles:
                out[role] = node
        if project_id is not None:
            project = s.get(Project, project_id)
            if project is None:
                raise ValueError(f"project {project_id} not found")
            bindings = dict(project.role_bindings or {})
            for spec in NODES:
                for role in spec.roles:
                    bindings[role] = {"node_id": out[role].id, "model": spec.model}
            for old in ("planner", "executor"):  # superseded by the roles above
                bindings.pop(old, None)
            project.role_bindings = bindings
            if project.executor_kind != "opencode":
                project.executor_kind = "opencode"  # the executor that speaks Claude's API
            s.add(project)
            s.commit()
    return out
