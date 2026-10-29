import asyncio
from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.watch.manager import WatcherManager
from docgraph.web.deps import AppState


@pytest.fixture
def mgr(tmp_path: Path) -> WatcherManager:
    cfg = Config(data_dir=tmp_path / "data")
    cfg.watch_debounce_sec = 0.05  # fast for tests
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    return WatcherManager(state)


@pytest.mark.asyncio
async def test_debounce_coalesces_same_path(mgr: WatcherManager):
    enqueued: list = []
    mgr._enqueue_direct = lambda ev: enqueued.append(ev)
    mgr._loop = asyncio.get_running_loop()
    for _ in range(10):
        mgr._on_raw_event("modified", "/tmp/x.md", None)
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.1)
    assert len(enqueued) == 1
    assert mgr.stats.events_received == 10
    assert mgr.stats.events_debounced == 9


@pytest.mark.asyncio
async def test_debounce_independent_per_path(mgr: WatcherManager):
    enqueued: list = []
    mgr._enqueue_direct = lambda ev: enqueued.append(ev)
    mgr._loop = asyncio.get_running_loop()
    mgr._on_raw_event("modified", "/tmp/a.md", None)
    mgr._on_raw_event("modified", "/tmp/b.md", None)
    await asyncio.sleep(0.1)
    paths = sorted(e.src_path for e in enqueued)
    assert paths == ["/tmp/a.md", "/tmp/b.md"]
