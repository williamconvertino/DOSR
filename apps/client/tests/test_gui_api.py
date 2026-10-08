"""GUI JSON API: the same flow a user clicks through in the browser."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dosr_client.gui import server as gui_server

DEMO = Path(__file__).resolve().parents[3] / "demo"


@pytest.fixture
def gui(tmp_path, attestor, monkeypatch):
    state = gui_server.GuiState(
        workspace=tmp_path / "ws",
        seeds_dir=DEMO / "projects",
        profiles_dir=DEMO / "clients",
        profile_path=None,
        attestor_url=None,
        chain_state=tmp_path / "chain.json",
    )
    # Route the GUI's attestor calls to the in-process attestor app.
    monkeypatch.setattr(state, "attestor", lambda: attestor)
    return TestClient(gui_server.create_app(state)), state


def wait_job(client, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["finished_at"]:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_index_and_state(gui):
    client, _ = gui
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    s = client.get("/api/state").json()
    assert {p["id"] for p in s["profiles"]} >= {"alice", "bob", "carol", "mallory"}
    assert {x["id"] for x in s["seeds"]} == {"calculator-py", "todo-js", "secure-vault"}
    assert [st["id"] for st in s["steps"]][0] == "preflight"


def test_full_gui_flow(gui):
    client, _ = gui
    r = client.post("/api/repos", json={"name": "calc", "seed": "calculator-py", "preset": "standard"})
    assert r.status_code == 200, r.text
    assert client.get("/api/state").json()["repos"][0]["id"] == "calc"

    repo = client.get("/api/repos/calc").json()
    assert repo["in_sync"] and repo["registered"]

    files = [f["path"] for f in client.get("/api/repos/calc/files").json()["files"]]
    assert "calculator/ops.py" in files and ".dosr/policy.json" in files
    assert client.get("/api/repos/calc/file", params={"path": ".dosr/policy.json"}).json()["readonly"]

    src = client.get("/api/repos/calc/file", params={"path": "calculator/ops.py"}).json()["content"]
    client.put("/api/repos/calc/file", json={"path": "calculator/ops.py", "content": src + "\n\ndef neg(a):\n    return -a\n"})
    assert "def neg" in client.get("/api/repos/calc/diff").json()["diff"]

    assert client.post("/api/profile", json={"id": "bob"}).status_code == 200
    job_id = client.post("/api/repos/calc/submit", json={"message": "Add neg()"}).json()["job_id"]
    job = wait_job(client, job_id)
    assert job["outcome"] == "approved", job["errors"]
    assert job["info"]["profile"] == "Bob"

    subs = client.get("/api/repos/calc/submissions").json()["submissions"]
    assert subs[0]["request_id"] == job_id
    detail = client.get(f"/api/repos/calc/submissions/{job_id}").json()
    assert "def neg" in detail["pr_markdown"]
    assert client.get("/api/repos/calc").json()["in_sync"]


def test_mallory_rejected_via_gui(gui):
    client, _ = gui
    client.post("/api/repos", json={"name": "calc", "seed": "calculator-py"})
    client.put("/api/repos/calc/file", json={"path": "x.py", "content": "X = 1\n"})
    client.post("/api/profile", json={"id": "mallory"})
    job = wait_job(client, client.post("/api/repos/calc/submit", json={"message": "x"}).json()["job_id"])
    assert job["outcome"] == "rejected"


def test_path_traversal_blocked(gui):
    client, _ = gui
    client.post("/api/repos", json={"name": "calc"})
    for bad in ("../escape.txt", ".git/config", "a/../../escape.txt"):
        r = client.put("/api/repos/calc/file", json={"path": bad, "content": "x"})
        assert r.status_code == 400, bad


def test_bad_repo_name_and_duplicate(gui):
    client, _ = gui
    assert client.post("/api/repos", json={"name": "../x"}).status_code == 400
    assert client.post("/api/repos", json={"name": "ok"}).status_code == 200
    assert client.post("/api/repos", json={"name": "ok"}).status_code == 409
