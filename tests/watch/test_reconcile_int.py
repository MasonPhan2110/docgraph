import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

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
    # Stub indexer so worker dispatch can complete without real embedding.
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
async def test_reconcile_indexes_preexisting_files(setup):
    state, watched = setup
    (watched / "a.md").write_text("first")
    (watched / "b.md").write_text("second")
    (watched / "ignored").mkdir()
    (watched / "ignored" / "deep.md").write_text("inside")
    (watched / ".git").mkdir()
    (watched / ".git" / "config").write_text("[core]")
    mgr = WatcherManager(state)
    await mgr.enable()
    try:
        await asyncio.sleep(1.5)
        docs = state.sqlite.list_watched_docs(prefix=str(watched))
        names = sorted(Path(d.watched_path).name for d in docs)
        assert "a.md" in names
        assert "b.md" in names
        assert "deep.md" in names
        assert "config" not in names  # .git ignored
    finally:
        await mgr.disable()


@pytest.mark.asyncio
async def test_reconcile_detects_deleted_files(setup):
    state, watched = setup
    f = watched / "doomed.md"
    f.write_text("body")
    mgr = WatcherManager(state)
    await mgr.enable()
    try:
        await asyncio.sleep(1.0)
        assert state.sqlite.get_doc_by_watched_path(str(f)) is not None
        await mgr.disable()
        f.unlink()
        await mgr.enable()  # reconcile sees missing file
        await asyncio.sleep(1.0)
        assert state.sqlite.get_doc_by_watched_path(str(f)) is None
    finally:
        await mgr.disable()


@pytest.mark.asyncio
async def test_reconcile_detects_mtime_change_while_offline(setup):
    state, watched = setup
    f = watched / "edited.md"
    f.write_text("v1")
    mgr = WatcherManager(state)
    await mgr.enable()
    try:
        await asyncio.sleep(1.0)
        doc1 = state.sqlite.get_doc_by_watched_path(str(f))
        await mgr.disable()
        f.write_text("v2 with much more body")
        import os, time
        time.sleep(0.01)
        os.utime(f, None)
        await mgr.enable()
        await asyncio.sleep(1.0)
        doc2 = state.sqlite.get_doc_by_watched_path(str(f))
        assert doc2.mtime_ns > doc1.mtime_ns
    finally:
        await mgr.disable()
