import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from docgraph.config import Config
from docgraph.web.app import create_app
from docgraph.web.deps import AppState


def _stub_index_new(state, doc_id, p, wd, materialize, mtime_ns):
    from docgraph.models import DocumentRecord, DocumentStatus, SourceType
    doc = DocumentRecord(
        id=doc_id, filename=Path(p).name, folder=wd.folder, tags=list(wd.tags),
        source_type=SourceType.WATCHED, status=DocumentStatus.READY,
        watched_path=str(p), materialize=materialize, mtime_ns=mtime_ns,
    )
    state.sqlite.insert_document(doc)


def _stub_reindex(state, doc_id, mtime_ns):
    from docgraph.models import DocumentStatus
    state.sqlite.update_mtime_ns(doc_id, mtime_ns)
    state.sqlite.update_status(doc_id, DocumentStatus.READY)


@pytest.fixture
def client(tmp_path: Path):
    cfg = Config(data_dir=tmp_path / "data")
    cfg.rerank_prewarm = False  # avoid lifespan hang on model load
    cfg.watch_debounce_sec = 0.1
    cfg.watch_workers = 2
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    # Stub indexer for fast deterministic ingest.
    indexer = state.indexer()
    indexer.index_watched_new = AsyncMock(side_effect=lambda doc_id, p, wd, mat, mt: _stub_index_new(state, doc_id, p, wd, mat, mt))
    indexer.reindex_watched = AsyncMock(side_effect=lambda doc_id, p, mat, mt: _stub_reindex(state, doc_id, mt))
    app = create_app(cfg, state=state, mount_mcp=False)
    with TestClient(app) as c:
        yield c, state, tmp_path


def test_e2e_add_dir_then_enable_then_create_file(client, tmp_path: Path):
    c, state, _ = client
    watched = tmp_path / "live"
    watched.mkdir()
    # 1. Add dir
    r = c.post("/api/watch/dirs", json={"path": str(watched), "folder": "live"})
    assert r.status_code == 201
    # 2. Enable watcher
    r = c.post("/api/watch/enable")
    assert r.status_code in (200, 202)
    # 3. Drop a file
    (watched / "note.md").write_text("# Live note\n\nbody body body.")
    # 4. Wait for debounce + index
    import time
    for _ in range(20):
        time.sleep(0.2)
        docs = state.sqlite.list_watched_docs(prefix=str(watched))
        if docs:
            break
    docs = state.sqlite.list_watched_docs(prefix=str(watched))
    assert len(docs) == 1
    assert docs[0].folder == "live"
    # 5. Disable cleanly
    r = c.post("/api/watch/disable")
    assert r.status_code == 200
