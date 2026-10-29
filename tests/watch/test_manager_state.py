from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.watch.manager import WatcherManager
from docgraph.watch.types import WatcherState
from docgraph.web.deps import AppState


@pytest.fixture
def state(tmp_path: Path) -> AppState:
    cfg = Config(data_dir=tmp_path / "data")
    cfg.ensure_dirs()
    return AppState.create(cfg)


@pytest.mark.asyncio
async def test_initial_state_disabled(state: AppState):
    mgr = WatcherManager(state)
    assert mgr.state == WatcherState.DISABLED


@pytest.mark.asyncio
async def test_enable_then_disable_round_trip(state: AppState):
    mgr = WatcherManager(state)
    await mgr.enable()
    assert mgr.state == WatcherState.ENABLED
    assert state.sqlite.get_watcher_state("enabled") == "true"
    result = await mgr.disable()
    assert mgr.state == WatcherState.DISABLED
    assert state.sqlite.get_watcher_state("enabled") == "false"
    assert "queue_drained" in result


@pytest.mark.asyncio
async def test_enable_is_idempotent(state: AppState):
    mgr = WatcherManager(state)
    await mgr.enable()
    await mgr.enable()  # must not raise; remains ENABLED
    assert mgr.state == WatcherState.ENABLED
    await mgr.disable()


@pytest.mark.asyncio
async def test_disable_when_already_disabled_is_noop(state: AppState):
    mgr = WatcherManager(state)
    result = await mgr.disable()
    assert mgr.state == WatcherState.DISABLED
    assert result["queue_drained"] == 0


@pytest.mark.asyncio
async def test_concurrent_enable_returns_409_marker(state: AppState):
    """Second concurrent enable while ENABLING raises WatcherTransitionInProgress."""
    import asyncio
    from docgraph.watch.manager import WatcherTransitionInProgress

    mgr = WatcherManager(state)

    # Patch the slow internal start to give a window for the race.
    original = mgr._start_observer
    started = asyncio.Event()

    async def slow_start():
        started.set()
        await asyncio.sleep(0.1)
        await original()

    mgr._start_observer = slow_start

    task = asyncio.create_task(mgr.enable())
    await started.wait()
    with pytest.raises(WatcherTransitionInProgress):
        await mgr.enable()
    await task
    await mgr.disable()
