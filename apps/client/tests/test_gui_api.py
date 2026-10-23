"""GUI JSON API: the same flow a user clicks through in the browser."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dosr_client.gui import server as gui_server

DEMO = Path(__file__).resolve().parents[3] / "demo"


@pytest.fixture
def gui(tmp_path, chain, attestor, monkeypatch):
    state = gui_server.GuiState(
        workspace=tmp_path / "ws",
        seeds_dir=DEMO / "projects",
        profiles_dir=DEMO / "clients",
        profile_path=None,
        attestor_url=None,
        chain_state=chain.state_path,  # the attestor fixture reads the same chain
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


def test_mallory_blocked_then_forced_via_gui(gui):
    client, _ = gui
    client.post("/api/repos", json={"name": "calc", "seed": "calculator-py", "preset": "standard"})
    client.put("/api/repos/calc/file", json={"path": "x.py", "content": "X = 1\n"})
    client.post("/api/profile", json={"id": "mallory"})
    job = wait_job(client, client.post("/api/repos/calc/submit", json={"message": "x"}).json()["job_id"])
    assert job["outcome"] == "blocked"
    job = wait_job(client, client.post("/api/repos/calc/submit", json={"force": True}).json()["job_id"])
    assert job["outcome"] == "rejected"
    assert any(not c["ok"] for c in job["info"]["attestor_checks"])


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


def test_common_models_catalog_and_picker(gui, monkeypatch):
    client, _ = gui
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cat = client.get("/api/state").json()["model_catalog"]
    assert {"openai", "anthropic", "gemini"} <= {p["id"] for p in cat["providers"]}
    assert cat["keys"]["anthropic"] == {"env": "ANTHROPIC_API_KEY", "set": False}
    assert any(m["spec"] == "openai/gpt-4o@2024-08-06" and m["api_model"] == "gpt-4o-2024-08-06" for m in cat["models"])

    # Version-less and aliased models are completed from the catalog.
    r = client.post("/api/repos", json={"name": "multi", "preset": "standard",
                                         "approved_models": ["openai/gpt-4o@>=2024-08-06", "claude/claude-sonnet-4-5",
                                                             "gemini/gemini-2.5-pro"]})
    assert r.status_code == 200, r.text
    repo = client.get("/api/repos/multi").json()
    assert repo["approved_models"] == ["openai/gpt-4o@>=2024-08-06", "anthropic/claude-sonnet-4-5@20250929",
                                       "gemini/gemini-2.5-pro@stable"]
    ok = [c["request"] for c in repo["model_choices"] if c["approved"]]
    assert ok == ["openai/gpt-4o@2024-11-20", "openai/gpt-4o@2024-08-06", "anthropic/claude-sonnet-4-5@20250929",
                  "gemini/gemini-2.5-pro@stable"]
    assert any(c["request"] == "openai/gpt-5@2025-08-07" and not c["approved"] for c in repo["model_choices"])
    assert repo["provider_keys"]["openai"]["env"] == "OPENAI_API_KEY"

    # Without the contributor's key the client blocks before sending anything.
    client.put("/api/repos/multi/file", json={"path": "x.py", "content": "X = 1\n"})
    job = wait_job(client, client.post("/api/repos/multi/submit",
                                       json={"message": "x", "model": "claude/claude-sonnet-4-5"}).json()["job_id"])
    assert job["outcome"] == "blocked"
    failed = {c["name"] for c in job["info"]["local_checks"] if c["ok"] is False}
    assert failed == {"provider_key_available"}
    # A common model the policy does not approve is blocked too.
    job = wait_job(client, client.post("/api/repos/multi/submit",
                                       json={"model": "openai/gpt-5@2025-08-07"}).json()["job_id"])
    assert job["outcome"] == "blocked"
    assert "model_is_approved" in {c["name"] for c in job["info"]["local_checks"] if c["ok"] is False}


def test_api_keys_screen_masks_saves_and_applies_without_restart(gui, tmp_path, monkeypatch):
    import os

    from dosr_client.keys import KeyStore

    client, state = gui
    for env in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("# my settings\nOTHER=1\nANTHROPIC_API_KEY=\n", encoding="utf-8")
    state.keys = KeyStore(env_file)
    secret = "sk-ant-test-0123456789-abcd"
    try:
        keys = {k["provider"]: k for k in client.get("/api/keys").json()["keys"]}
        assert {"openai", "anthropic", "gemini", "zhipu"} <= set(keys) and "mock" not in keys
        assert keys["anthropic"]["set"] is False

        r = client.put("/api/keys/anthropic", json={"key": secret})
        assert r.status_code == 200
        assert r.json()["masked"] == "••••••••abcd" and r.json()["source"] == "saved"
        assert secret not in client.get("/api/keys").text and secret not in r.text
        assert os.environ["ANTHROPIC_API_KEY"] == secret
        lines = env_file.read_text("utf-8").splitlines()
        assert lines == ["# my settings", "OTHER=1", f"ANTHROPIC_API_KEY={secret}"]

        # Used right away: the client no longer blocks for a missing key.
        client.post("/api/repos", json={"name": "c", "preset": "standard",
                                         "approved_models": ["anthropic/claude-sonnet-4-5@20250929"]})
        repo = client.get("/api/repos/c").json()
        assert repo["provider_keys"]["anthropic"]["set"] is True

        assert client.put("/api/keys/anthropic", json={"key": "has space"}).status_code == 400
        assert client.put("/api/keys/nope", json={"key": "x"}).status_code == 400

        r = client.delete("/api/keys/anthropic")
        assert r.json()["set"] is False and "ANTHROPIC_API_KEY" not in os.environ
        assert env_file.read_text("utf-8").splitlines() == ["# my settings", "OTHER=1"]
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def test_template_editor_endpoints(gui, tmp_path, monkeypatch):
    client, _ = gui
    monkeypatch.setenv("DOSR_TEMPLATES", str(tmp_path / "tpl"))
    s = client.get("/api/state").json()
    assert {"standard-code-review-v1"} <= {t["id"] for t in s["templates"]}
    assert s["templates_dir"] == str(tmp_path / "tpl")

    builtin = client.get("/api/templates/standard-code-review-v1").json()
    assert builtin["builtin"] and "## Diff" in builtin["preview"]
    text = client.get("/api/template-scaffold", params={"id": "gui-v1", "base": "standard-code-review-v1"}).json()["toml"]
    text = text.replace('heading = "Commits"', 'heading = "History"')
    assert client.post("/api/templates/check", json={"text": "id = 1"}).json()["ok"] is False
    checked = client.post("/api/templates/check", json={"text": text}).json()
    assert checked["ok"] and "## History" in checked["preview"]
    clash = text.replace('id = "gui-v1"', 'id = "standard-code-review-v1"')
    assert client.post("/api/templates", json={"text": clash}).status_code == 400

    saved = client.post("/api/templates", json={"text": text}).json()
    assert Path(saved["saved"]) == tmp_path / "tpl" / "gui-v1.toml"
    assert client.post("/api/templates", json={"text": text + "\n# edit\n"}).status_code == 409
    assert client.post("/api/templates", json={"text": text + "\n# edit\n", "overwrite": True}).status_code == 200
    assert any(t["id"] == "gui-v1" and t["source"] == "file" for t in client.get("/api/templates").json()["templates"])

    r = client.post("/api/repos", json={"name": "tpl", "preset": "standard", "prompt_template": "gui-v1"})
    assert r.status_code == 200, r.text
    repo = client.get("/api/repos/tpl").json()
    assert repo["template"]["id"] == "gui-v1" and repo["template"]["builtin"] is False
    assert repo["policy"]["review"]["template"]["id"] == "gui-v1"
    assert "## History" in client.get("/api/repos/tpl/template").json()["preview"]
    assert client.post("/api/repos", json={"name": "x", "preset": "standard", "prompt_template": "nope"}).status_code == 400
