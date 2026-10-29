"""Tests for Issue 5 (per-emitter unschedule) and Issue 7 (observer auto-restart)."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from docgraph.config import Config
from docgraph.models import WatchedDirRecord
from docgraph.watch.manager import WatcherManager
from docgraph.watch.types import WatcherState
from docgraph.web.deps import AppState


def _stub_index_new(state, doc_id, p, wd, materialize, mtime_ns):
    from docgraph.models import DocumentRecord, DocumentStatus, SourceType
    state.sqlite.insert_document(DocumentRecord(
        id=doc_id, filename=Path(p).name, folder=wd.folder, tags=list(wd.tags),
        source_type=SourceType.WATCHED, status=DocumentStatus.READY,
        watched_path=str(p), materialize=materialize, mtime_ns=mtime_ns,
    ))


@pytest.fixture
def state(tmp_path: Path) -> AppState:
    cfg = Config(data_dir=tmp_path / "data")
    cfg.rerank_prewarm = False
    cfg.watch_debounce_sec = 0.05
    cfg.watch_workers = 2
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    indexer = state.indexer()
    indexer.index_watched_new = AsyncMock(
        side_effect=lambda d, p, wd, mat, mt: _stub_index_new(state, d, p, wd, mat, mt)
    )
    indexer.reindex_watched = AsyncMock()
    return state


@pytest.mark.asyncio
async def test_schedule_and_unschedule_dir_tracks_handles(state: AppState, tmp_path: Path):
    a = tmp_path / "a"
    a.mkdir()
    state.sqlite.insert_watched_dir(WatchedDirRecord(
        id="wd_a", path=str(a), created_at="2026-06-07T00:00:00Z",
    ))
    mgr = WatcherManager(state)
    await mgr.enable()
    try:
        assert "wd_a" in mgr._watches
        # Add a second dir at runtime.
        b = tmp_path / "b"
        b.mkdir()
        wd_b = WatchedDirRecord(
            id="wd_b", path=str(b), created_at="2026-06-07T00:00:00Z",
        )
        state.sqlite.insert_watched_dir(wd_b)
        assert mgr.schedule_dir(wd_b) is True
        assert "wd_b" in mgr._watches

        # Per-dir unschedule removes only that one.
        assert mgr.unschedule_dir("wd_a") is True
        assert "wd_a" not in mgr._watches
        assert "wd_b" in mgr._watches

        # Idempotent: double-unschedule is a no-op (False).
        assert mgr.unschedule_dir("wd_a") is False
    finally:
        await mgr.disable()


@pytest.mark.asyncio
async def test_unschedule_unknown_dir_returns_false(state: AppState):
    mgr = WatcherManager(state)
    await mgr.enable()
    try:
        assert mgr.unschedule_dir("wd_does_not_exist") is False
    finally:
        await mgr.disable()


@pytest.mark.asyncio
async def test_recovery_loop_rebuilds_dead_observer(state: AppState, tmp_path: Path, monkeypatch):
    """When the observer thread dies, the next recovery cycle rebuilds it."""
    a = tmp_path / "rec"
    a.mkdir()
    state.sqlite.insert_watched_dir(WatchedDirRecord(
        id="wd_rec", path=str(a), created_at="2026-06-07T00:00:00Z",
    ))
    # Force the recovery loop to fire in <1s for the test.
    state.cfg.watch_recovery_interval_sec = 1
    mgr = WatcherManager(state)
    await mgr.enable()
    try:
        original_observer = mgr._observer
        assert original_observer is not None and original_observer.is_alive()

        # Simulate observer death by stopping the thread out from under us.
        original_observer.stop()
        original_observer.join(timeout=2.0)
        assert not original_observer.is_alive()

        # Wait one recovery cycle.
        import asyncio
        await asyncio.sleep(1.5)

        # Recovery loop should have rebuilt observer.
        assert mgr._observer is not None
        assert mgr._observer is not original_observer
        assert mgr._observer.is_alive()
        assert "wd_rec" in mgr._watches
    finally:
        await mgr.disable()
