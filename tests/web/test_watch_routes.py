from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docgraph.config import Config
from docgraph.web.app import create_app
from docgraph.web.deps import AppState


@pytest.fixture
def client(tmp_path: Path):
    cfg = Config(data_dir=tmp_path / "data")
    cfg.rerank_prewarm = False  # don't load Rust model in tests
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    app = create_app(cfg, state=state, mount_mcp=False)
    with TestClient(app) as c:
        yield c, state, tmp_path


def test_status_disabled_initial(client):
    c, _, _ = client
    r = c.get("/api/watch/status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["dirs_count"] == 0


def test_enable_then_disable(client):
    c, _, _ = client
    r = c.post("/api/watch/enable")
    assert r.status_code in (200, 202)
    body = r.json()
    assert body["enabled"] is True
    r = c.get("/api/watch/status")
    assert r.json()["enabled"] is True
    r = c.post("/api/watch/disable")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_enable_idempotent(client):
    c, _, _ = client
    c.post("/api/watch/enable")
    r = c.post("/api/watch/enable")
    assert r.status_code in (200, 202)
    c.post("/api/watch/disable")


def test_add_list_remove_dir(client, tmp_path: Path):
    c, _, _ = client
    watched = tmp_path / "src"
    watched.mkdir()
    r = c.post("/api/watch/dirs", json={
        "path": str(watched), "folder": "src", "tags": "team", "ignore_globs": []
    })
    assert r.status_code == 201
    wd_id = r.json()["id"]
    r = c.get("/api/watch/dirs")
    assert r.status_code == 200
    assert any(d["id"] == wd_id for d in r.json()["dirs"])
    r = c.delete(f"/api/watch/dirs/{wd_id}")
    assert r.status_code == 200


def test_add_dir_path_not_exist(client):
    c, _, _ = client
    r = c.post("/api/watch/dirs", json={"path": "/nonexistent/path"})
    assert r.status_code == 400


def test_add_dir_overlap_rejected(client, tmp_path: Path):
    c, _, _ = client
    w = tmp_path / "outer"
    w.mkdir()
    inner = w / "inner"
    inner.mkdir()
    r = c.post("/api/watch/dirs", json={"path": str(w)})
    assert r.status_code == 201
    r = c.post("/api/watch/dirs", json={"path": str(inner)})
    assert r.status_code == 409


def test_add_dir_inside_data_dir_rejected(client, tmp_path: Path):
    c, state, _ = client
    inside = state.cfg.data_dir / "subdir"
    inside.mkdir(parents=True)
    r = c.post("/api/watch/dirs", json={"path": str(inside)})
    assert r.status_code == 400


def test_remove_nonexistent_dir(client):
    c, _, _ = client
    r = c.delete("/api/watch/dirs/wd_missing")
    assert r.status_code == 404


def test_reconcile_endpoint(client, tmp_path: Path):
    c, _, _ = client
    w = tmp_path / "rec"
    w.mkdir()
    r = c.post("/api/watch/dirs", json={"path": str(w)})
    assert r.status_code == 201
    # Watcher must be enabled for reconcile to fire.
    c.post("/api/watch/enable")
    try:
        r = c.post("/api/watch/reconcile")
        assert r.status_code == 200
        assert r.json()["reconcile_started"] is True
    finally:
        c.post("/api/watch/disable")
