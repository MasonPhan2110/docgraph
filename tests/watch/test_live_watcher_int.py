import asyncio
from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.models import WatchedDirRecord
from docgraph.watch.manager import WatcherManager
from docgraph.web.deps import AppState


@pytest.fixture
def setup(tmp_path: Path):
    cfg = Config(data_dir=tmp_path / "data")
    cfg.watch_debounce_sec = 0.1
    cfg.watch_workers = 2
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    # Stub the indexer so we don't need a real embedder.
    from unittest.mock import AsyncMock
    indexer = state.indexer()
    indexer.index_watched_new = AsyncMock(side_effect=lambda doc_id, p, wd, mat, mt: _stub_index_new(state, doc_id, p, wd, mat, mt))
    indexer.reindex_watched = AsyncMock(side_effect=lambda doc_id, p, mat, mt: _stub_reindex(state, doc_id, mt))
    watched = tmp_path / "src"
    watched.mkdir()
    state.sqlite.insert_watched_dir(WatchedDirRecord(
        id="wd_t", path=str(watched), created_at="2026-06-07T00:00:00Z",
    ))
    return state, watched


def _stub_index_new(state, doc_id, p, wd, materialize, mtime_ns):
    """Simulate indexer: insert a doc row + mark READY without actually embedding."""
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


@pytest.mark.asyncio
async def test_live_create_and_modify(setup):
    state, watched = setup
    mgr = WatcherManager(state)
    await mgr.enable()
    try:
        f = watched / "note.md"
        f.write_text("first")
        await asyncio.sleep(0.5)  # debounce + index
        doc = state.sqlite.get_doc_by_watched_path(str(f))
        assert doc is not None
        # Modify
        f.write_text("second longer body")
        import os, time
        time.sleep(0.01)
        os.utime(f, None)
        await asyncio.sleep(0.5)
        doc2 = state.sqlite.get_doc_by_watched_path(str(f))
        assert doc2.id == doc.id
        assert doc2.mtime_ns >= doc.mtime_ns
    finally:
        await mgr.disable()


@pytest.mark.asyncio
async def test_live_delete_removes_doc(setup):
    state, watched = setup
    mgr = WatcherManager(state)
    await mgr.enable()
    try:
        f = watched / "x.md"
        f.write_text("body")
        await asyncio.sleep(0.5)
        assert state.sqlite.get_doc_by_watched_path(str(f)) is not None
        f.unlink()
        await asyncio.sleep(0.5)
        assert state.sqlite.get_doc_by_watched_path(str(f)) is None
    finally:
        await mgr.disable()


@pytest.mark.asyncio
async def test_live_ignores_unsupported_ext(setup):
    state, watched = setup
    mgr = WatcherManager(state)
    await mgr.enable()
    try:
        f = watched / "binary.dmg"
        f.write_bytes(b"junk")
        await asyncio.sleep(0.5)
        assert state.sqlite.get_doc_by_watched_path(str(f)) is None
    finally:
        await mgr.disable()
