"""Reconcile must never mass-delete the index because the disk view is incomplete."""
from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.models import DocumentRecord, DocumentStatus, SourceType, WatchedDirRecord
from docgraph.watch.manager import WatcherManager
from docgraph.watch.reconcile import reconcile_dir
from docgraph.web.deps import AppState


@pytest.fixture
def setup(tmp_path: Path):
    cfg = Config(data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    watched = tmp_path / "src"
    watched.mkdir()
    wd = WatchedDirRecord(id="wd_t", path=str(watched), created_at="2026-06-07T00:00:00Z")
    state.sqlite.insert_watched_dir(wd)
    mgr = WatcherManager(state)
    events = []
    mgr._enqueue_direct = events.append
    return state, mgr, wd, watched, events


def _track(state, watched: Path, n: int, create_files: bool) -> None:
    for i in range(n):
        p = watched / f"f{i}.md"
        if create_files:
            p.write_text(f"doc {i}")
        state.sqlite.insert_document(DocumentRecord(
            id=f"doc_{i}", filename=p.name, folder="", tags=[],
            source_type=SourceType.WATCHED, status=DocumentStatus.READY,
            watched_path=str(p), materialize=False,
            mtime_ns=p.stat().st_mtime_ns if create_files else 1,
        ))


def _deletes(events):
    return [e for e in events if e.action == "DELETE"]


@pytest.mark.asyncio
async def test_missing_root_skips_reconcile(setup):
    state, mgr, wd, watched, events = setup
    _track(state, watched, 3, create_files=True)
    for p in watched.iterdir():
        p.unlink()
    watched.rmdir()  # e.g. external drive not mounted yet

    await reconcile_dir(mgr, wd)

    assert events == []
    assert mgr.stats.reconcile_runs == 0


@pytest.mark.asyncio
async def test_mass_delete_guard_keeps_docs(setup):
    state, mgr, wd, watched, events = setup
    _track(state, watched, 12, create_files=True)
    for i in range(7):  # 7/12 > 50% vanish at once
        (watched / f"f{i}.md").unlink()

    await reconcile_dir(mgr, wd)

    assert _deletes(events) == []


@pytest.mark.asyncio
async def test_small_delta_still_deletes(setup):
    state, mgr, wd, watched, events = setup
    _track(state, watched, 12, create_files=True)
    for i in range(3):
        (watched / f"f{i}.md").unlink()

    await reconcile_dir(mgr, wd)

    assert sorted(Path(e.src_path).name for e in _deletes(events)) == ["f0.md", "f1.md", "f2.md"]


@pytest.mark.asyncio
async def test_small_dir_full_delete_allowed(setup):
    """Below the min-docs floor the ratio guard is off: emptying a tiny dir is normal."""
    state, mgr, wd, watched, events = setup
    _track(state, watched, 3, create_files=True)
    for i in range(3):
        (watched / f"f{i}.md").unlink()

    await reconcile_dir(mgr, wd)

    assert len(_deletes(events)) == 3


@pytest.mark.asyncio
async def test_walk_error_skips_deletes(setup, monkeypatch):
    state, mgr, wd, watched, events = setup
    _track(state, watched, 2, create_files=True)
    (watched / "f0.md").unlink()

    import docgraph.watch.reconcile as rec
    real_walk = rec.os.walk

    def failing_walk(top, onerror=None, **kw):
        if onerror:
            onerror(PermissionError(13, "denied", str(watched / "sub")))
        yield from real_walk(top, onerror=onerror, **kw)

    monkeypatch.setattr(rec.os, "walk", failing_walk)
    await reconcile_dir(mgr, wd)

    assert _deletes(events) == []
