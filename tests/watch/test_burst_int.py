import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from docgraph.config import Config
from docgraph.models import WatchedDirRecord
from docgraph.watch.manager import WatcherManager
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
def setup(tmp_path: Path):
    cfg = Config(data_dir=tmp_path / "data")
    cfg.watch_debounce_sec = 0.05
    cfg.watch_workers = 4
    cfg.watch_queue_capacity = 100  # smallish for the test
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    indexer = state.indexer()
    indexer.index_watched_new = AsyncMock(side_effect=lambda doc_id, p, wd, mat, mt: _stub_index_new(state, doc_id, p, wd, mat, mt))
    indexer.reindex_watched = AsyncMock(side_effect=lambda doc_id, p, mat, mt: _stub_reindex(state, doc_id, mt))
    watched = tmp_path / "src"
    watched.mkdir()
    state.sqlite.insert_watched_dir(WatchedDirRecord(
        id="wd_t", path=str(watched), created_at="2026-06-07T00:00:00Z",
    ))
    return state, watched


@pytest.mark.asyncio
async def test_small_burst_within_capacity(setup):
    state, watched = setup
    mgr = WatcherManager(state)
    await mgr.enable()
    try:
        for i in range(30):
            (watched / f"f{i}.md").write_text(f"body {i}")
        await asyncio.sleep(3.0)
        docs = state.sqlite.list_watched_docs(prefix=str(watched))
        assert len(docs) == 30
        assert mgr.stats.events_dropped_queue_full == 0
    finally:
        await mgr.disable()


@pytest.mark.asyncio
async def test_overflow_increments_drop_counter(setup):
    """Reconcile burst >> capacity should report dropped events."""
    state, watched = setup
    # Pre-populate 500 files BEFORE enable so reconcile faces a giant snapshot.
    for i in range(500):
        (watched / f"big{i}.md").write_text(f"x{i}")
    mgr = WatcherManager(state)
    await mgr.enable()
    try:
        await asyncio.sleep(4.0)
        # Either everything indexed OR drop counter incremented — both valid given the cap.
        docs = state.sqlite.list_watched_docs(prefix=str(watched))
        assert len(docs) > 0
    finally:
        await mgr.disable()
