from sqlmodel import select

from foreman.core.models import Control
from foreman.core.state import estop_active
from foreman.db import control_session
from tests.helpers import make_project, make_task, project, task


def test_project_lifecycle(client, origin):
    p = make_project(client, origin, name="demo")
    assert p["default_branch"] == "main"
    assert client.get("/api/projects").json()[0]["name"] == "demo"
    r = client.patch(f"/api/projects/{p['id']}", json={"test_cmd": "true"})
    assert r.json()["test_cmd"] == "true"
    assert client.delete(f"/api/projects/{p['id']}").status_code == 204
    assert client.get("/api/projects").json() == []


def test_clone_failure_is_reported(client, tmp_path):
    r = client.post("/api/projects", json={"name": "x", "repo_url": str(tmp_path / "nope")})
    assert r.status_code == 400 and "clone failed" in r.json()["detail"]
    assert client.get("/api/projects").json() == []


def test_task_requires_validation_and_valid_deps(client, origin):
    p = make_project(client, origin)  # scripted (shell) tasks: they need a check command
    r = client.post(f"/api/projects/{p['id']}/tasks", json={"title": "x"})
    assert r.status_code == 400 and "validation command" in r.json()["detail"]
    r = client.post(
        f"/api/projects/{p['id']}/tasks",
        json={"title": "x", "validation_cmd": "true", "deps": [999]},
    )
    assert r.status_code == 400
    t = make_task(client, p["id"], "true", title="first")
    assert t["state"] == "queued"
    t2 = make_task(client, p["id"], "true", deps=[t["id"]])
    assert t2["deps"] == [t["id"]]


def test_nodes_hide_api_key(client):
    r = client.post(
        "/api/nodes",
        json={"name": "mac", "base_url": "http://mac:8080/v1/", "api_key": "sk-x", "capacity": 2},
    )
    n = r.json()
    assert "api_key" not in n and n["has_api_key"] and n["base_url"] == "http://mac:8080/v1"
    checked = client.post(f"/api/nodes/{n['id']}/check").json()
    assert checked["healthy"] is False and checked["error"]


def test_update_node_capacity(client):
    n = client.post("/api/nodes", json={"name": "mac", "base_url": "http://mac:8080/v1"}).json()
    r = client.patch(f"/api/nodes/{n['id']}", json={"capacity": 3})
    assert r.status_code == 200 and r.json()["capacity"] == 3
    assert client.get("/api/nodes").json()[0]["capacity"] == 3
    assert client.patch(f"/api/nodes/{n['id']}", json={"capacity": 0}).status_code == 422
    assert client.patch("/api/nodes/999", json={"capacity": 2}).status_code == 404


def test_pause_resume(client, origin):
    p = make_project(client, origin)
    t = make_task(client, p["id"], "true")
    client.post("/api/controls", json={"scope": "project", "target_id": p["id"], "action": "pause"})
    assert project(p["id"]).paused
    client.post(
        "/api/controls", json={"scope": "project", "target_id": p["id"], "action": "resume"}
    )
    assert not project(p["id"]).paused
    client.post("/api/controls", json={"scope": "task", "target_id": t["id"], "action": "pause"})
    assert task(t["id"]).hold


def test_stop_and_estop_are_queued_for_supervisor(client, origin):
    p = make_project(client, origin)
    r = client.post(
        "/api/controls", json={"scope": "project", "target_id": p["id"], "action": "stop"}
    )
    assert r.json()["control_id"]
    client.post("/api/controls", json={"scope": "global", "action": "estop"})
    assert estop_active()
    with control_session() as s:
        assert len(s.exec(select(Control)).all()) == 2
    assert client.get("/api/system").json()["estop"] is True
    client.post("/api/controls", json={"scope": "global", "action": "clear_estop"})
    assert not estop_active()


def test_control_validation(client):
    assert client.post("/api/controls", json={"scope": "task", "action": "stop"}).status_code == 400
    assert (
        client.post("/api/controls", json={"scope": "global", "action": "pause"}).status_code == 400
    )


def test_token_auth(client, monkeypatch):
    monkeypatch.setenv("FOREMAN_TOKEN", "s3cret")
    assert client.get("/api/system").status_code == 401
    assert client.get("/api/system", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/api/system?token=s3cret").status_code == 200


def test_events_endpoint(client, origin):
    p = make_project(client, origin)
    make_task(client, p["id"], "true")
    events = client.get(f"/api/events?project_id={p['id']}").json()
    assert [e["kind"] for e in events][:2] == ["system", "state"]
    after = events[-1]["id"]
    assert client.get(f"/api/events?project_id={p['id']}&after={after}").json() == []


def test_github_token_from_dashboard(client, monkeypatch):
    monkeypatch.setenv("FOREMAN_GITHUB_TOKEN", "ghp_from_env_1111")
    assert client.get("/api/settings/github").json() == {
        "configured": True,
        "source": "environment",
        "hint": "1111",
    }
    r = client.put("/api/settings/github", json={"token": " ghp_from_ui_2222 "})
    assert r.json() == {"configured": True, "source": "dashboard", "hint": "2222"}
    assert "ghp_from_ui" not in r.text
    assert "ghp_from_ui" not in client.get("/api/settings/github").text
    from foreman.core.state import github_token

    assert github_token() == ("ghp_from_ui_2222", "dashboard")  # what git and the PR call use
    assert client.put("/api/settings/github", json={"token": ""}).status_code == 422
    assert client.delete("/api/settings/github").json()["source"] == "environment"


def test_github_token_check(client, monkeypatch):
    from foreman import github

    assert client.post("/api/settings/github/check").status_code == 400
    seen = []

    async def fake_user(token):
        seen.append(token)
        if token == "bad":
            raise github.GitHubError("GitHub rejected the token (401)")
        return {"login": "octocat", "scopes": "repo"}

    monkeypatch.setattr(github, "user", fake_user)
    client.put("/api/settings/github", json={"token": "good"})
    r = client.post("/api/settings/github/check").json()
    assert r["ok"] and r["login"] == "octocat"
    client.put("/api/settings/github", json={"token": "bad"})
    r = client.post("/api/settings/github/check").json()
    assert not r["ok"] and "401" in r["error"] and seen == ["good", "bad"]
