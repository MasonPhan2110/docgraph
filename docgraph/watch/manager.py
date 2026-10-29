from __future__ import annotations

import asyncio
import logging
import threading
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path

from watchdog.observers import Observer

from docgraph.ingest.lang_dispatch import detect_materialize
from docgraph.watch.handler import DocgraphEventHandler
from docgraph.watch.ignore import IgnoreMatcher
from docgraph.watch.types import WatcherState, WatcherStats, WatchEvent

_ACTION_MAP = {
    "created": "UPSERT",
    "modified": "UPSERT",
    "deleted": "DELETE",
    "moved": "RENAME",
}

logger = logging.getLogger(__name__)


class WatcherTransitionInProgress(Exception):
    """Raised when enable/disable is called while another transition is running."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WatcherManager:
    """Owns the watcher subsystem: state machine, observer, queue, workers."""

    def __init__(self, app_state) -> None:
        self._app = app_state
        self._cfg = app_state.cfg
        self._sqlite = app_state.sqlite
        self.state: WatcherState = WatcherState.DISABLED
        self.stats = WatcherStats()
        self._lock = asyncio.Lock()
        # Sync lock guards observer mutation (schedule/unschedule) across the
        # asyncio thread (HTTP routes) and the watchdog observer thread.
        self._observer_lock = threading.Lock()
        self._observer = None
        self._watches: dict[str, object] = {}  # wd_id → ObservedWatch handle
        self._observer_handler = None
        self._workers: list[asyncio.Task] = []
        self._queues: list[asyncio.Queue] = []
        self._debounce_tasks: dict[str, asyncio.TimerHandle] = {}
        self._shutdown = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_enabled_at: str | None = None
        self._recovery_task: asyncio.Task | None = None
        # Caches populated at enable-time; kept in sync by add/remove dir routes.
        self._ignore_matchers: dict = {}
        self._watched_dirs_cache: dict = {}
        # Restore persisted state.
        persisted = self._sqlite.get_watcher_state("enabled")
        if persisted == "true":
            logger.info("watcher: persisted state is enabled, will auto-enable on startup hook")

    async def enable(self) -> dict:
        if self.state == WatcherState.ENABLED:
            return {"enabled": True, "reconcile_started": False, "dirs": len(self._sqlite.list_watched_dirs())}
        if self.state in (WatcherState.ENABLING, WatcherState.DISABLING):
            raise WatcherTransitionInProgress("watcher transition in progress")
        async with self._lock:
            if self.state == WatcherState.ENABLED:
                return {"enabled": True, "reconcile_started": False, "dirs": len(self._sqlite.list_watched_dirs())}
            self.state = WatcherState.ENABLING
            try:
                self._shutdown = asyncio.Event()
                self._loop = asyncio.get_running_loop()
                self.stats.reset()
                await self._start_observer()
                await self._start_workers()
                self._sqlite.set_watcher_state("enabled", "true")
                self._last_enabled_at = _now_iso()
                self._sqlite.set_watcher_state("last_enabled_at", self._last_enabled_at)
                self.state = WatcherState.ENABLED
                logger.info("watcher: enabled")
                # Schedule reconcile (background, non-blocking).
                dirs = self._sqlite.list_watched_dirs()
                for wd in dirs:
                    asyncio.create_task(self._reconcile_dir(wd))
                # Periodic recovery.
                self._recovery_task = asyncio.create_task(self._recovery_loop())
                return {"enabled": True, "reconcile_started": True, "dirs": len(dirs)}
            except Exception:
                self.state = WatcherState.DISABLED
                raise

    async def disable(self, *, persist: bool = True) -> dict:
        """Stop the watcher. persist=False (server shutdown) keeps the stored
        enabled flag so the next startup auto-enables again."""
        if self.state == WatcherState.DISABLED:
            return {"enabled": False, "queue_drained": 0, "queue_dropped": 0}
        if self.state in (WatcherState.ENABLING, WatcherState.DISABLING):
            raise WatcherTransitionInProgress("watcher transition in progress")
        async with self._lock:
            if self.state == WatcherState.DISABLED:
                return {"enabled": False, "queue_drained": 0, "queue_dropped": 0}
            self.state = WatcherState.DISABLING
            try:
                self._shutdown.set()
                # Cancel pending debounces.
                for h in list(self._debounce_tasks.values()):
                    h.cancel()
                self._debounce_tasks.clear()
                # Stop observer.
                if self._observer is not None:
                    self._observer.stop()
                    self._observer.join(timeout=5.0)
                    self._observer = None
                # Count remaining queue items, then cancel workers.
                drained = sum(q.qsize() for q in self._queues)
                for t in self._workers:
                    t.cancel()
                await asyncio.gather(*self._workers, return_exceptions=True)
                self._workers = []
                self._queues = []
                if self._recovery_task is not None:
                    self._recovery_task.cancel()
                    self._recovery_task = None
                if persist:
                    self._sqlite.set_watcher_state("enabled", "false")
                self.state = WatcherState.DISABLED
                logger.info("watcher: disabled, queue_drained=%d", drained)
                return {"enabled": False, "queue_drained": drained, "queue_dropped": self.stats.events_dropped_queue_full}
            except Exception:
                self.state = WatcherState.ENABLED  # roll back state
                raise

    # ---- placeholders filled in by later tasks ----
    async def _start_observer(self) -> None:
        self._observer = Observer()
        self._watches: dict[str, object] = {}  # wd_id → ObservedWatch
        self._observer_handler = DocgraphEventHandler(self, self._loop)
        for wd in self._sqlite.list_watched_dirs():
            try:
                watch = self._observer.schedule(
                    self._observer_handler, wd.path, recursive=True
                )
                self._watches[wd.id] = watch
            except Exception as exc:
                logger.warning("failed to schedule watch for %s: %s", wd.path, exc)
        self._observer.start()

    def schedule_dir(self, wd) -> bool:
        """Add a single dir to the running observer. Returns True if scheduled."""
        if self._observer is None or not self._observer.is_alive():
            return False
        with self._observer_lock:
            try:
                watch = self._observer.schedule(
                    self._observer_handler, wd.path, recursive=True
                )
                self._watches[wd.id] = watch
                return True
            except Exception:
                logger.exception("schedule_dir failed for wd=%s", wd.id)
                return False

    def unschedule_dir(self, wd_id: str) -> bool:
        """Remove a single dir from the running observer. Returns True if removed."""
        if self._observer is None:
            return False
        with self._observer_lock:
            watch = self._watches.pop(wd_id, None)
            if watch is None:
                return False
            try:
                self._observer.unschedule(watch)
                return True
            except Exception:
                logger.exception("unschedule_dir failed for wd=%s", wd_id)
                return False

    def _on_raw_event(self, event_type: str, src_path: str, dest_path: str | None) -> None:
        """Called on the asyncio loop via call_soon_threadsafe."""
        self.stats.events_received += 1
        action = _ACTION_MAP.get(event_type)
        if action is None:
            return
        self._enqueue_debounced(action, src_path, dest_path)

    def _enqueue_debounced(self, action: str, src_path: str, dest_path: str | None) -> None:
        key = src_path
        if existing := self._debounce_tasks.get(key):
            existing.cancel()
            self.stats.events_debounced += 1
        handle = self._loop.call_later(
            self._cfg.watch_debounce_sec,
            self._flush_debounce, action, src_path, dest_path,
        )
        self._debounce_tasks[key] = handle

    def _flush_debounce(self, action: str, src_path: str, dest_path: str | None) -> None:
        self._debounce_tasks.pop(src_path, None)
        event = WatchEvent(action=action, src_path=src_path, dest_path=dest_path)
        self._enqueue_direct(event)

    def _enqueue_direct(self, event: WatchEvent) -> None:
        if not self._queues:
            self.stats.events_dropped_queue_full += 1
            return
        idx = hash(event.src_path) % len(self._queues)
        try:
            self._queues[idx].put_nowait(event)
            self.stats.events_processed += 1
        except asyncio.QueueFull:
            self.stats.events_dropped_queue_full += 1
            logger.warning("watcher queue full; dropping event: %s %s", event.action, event.src_path)

    async def _start_workers(self) -> None:
        per_worker = max(1, self._cfg.watch_queue_capacity // self._cfg.watch_workers)
        self._queues = [asyncio.Queue(maxsize=per_worker) for _ in range(self._cfg.watch_workers)]
        self._workers = [
            asyncio.create_task(self._worker_loop(i))
            for i in range(self._cfg.watch_workers)
        ]
        # Cache watched dirs + ignore matchers (refresh each enable; routes
        # keep these in sync as dirs are added/removed at runtime).
        self._ignore_matchers: dict[str, IgnoreMatcher] = {}
        self._watched_dirs_cache: dict[str, "WatchedDirRecord"] = {}
        for wd in self._sqlite.list_watched_dirs():
            self._ignore_matchers[wd.id] = IgnoreMatcher(wd)
            self._watched_dirs_cache[wd.id] = wd

    async def _worker_loop(self, worker_id: int) -> None:
        queue = self._queues[worker_id]
        while not self._shutdown.is_set():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                if event.action == "UPSERT":
                    await self._handle_upsert(event.src_path)
                elif event.action == "DELETE":
                    await self._handle_delete(event.src_path)
                elif event.action == "RENAME":
                    await self._handle_rename(event.src_path, event.dest_path)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker=%d failed on %s", worker_id, event)
            finally:
                queue.task_done()

    def _lookup_wd_for_path(self, p: Path):
        # Use cache populated at enable-time + maintained by add/remove routes
        # — avoid a DB round-trip per event.
        for wd in self._watched_dirs_cache.values():
            try:
                p.relative_to(wd.path)
                return wd
            except ValueError:
                continue
        return None

    async def _handle_upsert(self, path: str) -> None:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return
        wd = self._lookup_wd_for_path(p)
        if wd is None:
            return
        matcher = self._ignore_matchers.get(wd.id) or IgnoreMatcher(wd)
        if matcher.should_ignore(p):
            return
        # Single stat() call to avoid TOCTOU between size + mtime reads.
        try:
            st = p.stat()
        except OSError:
            return
        max_bytes = self._cfg.max_file_size_mb * 1024 * 1024
        if st.st_size > max_bytes:
            logger.warning("watcher: file too large (%d bytes), skipping: %s", st.st_size, path)
            return
        mtime_ns = st.st_mtime_ns
        materialize = detect_materialize(p, self._cfg)
        if materialize is None:
            return
        indexer = self._app.indexer()
        existing = self._sqlite.get_doc_by_watched_path(str(p))
        if existing:
            if existing.mtime_ns == mtime_ns:
                return
            if not self._sqlite.claim_for_reindex(existing.id):
                return
            await indexer.reindex_watched(existing.id, p, materialize, mtime_ns)
        else:
            doc_id = f"doc_{_uuid.uuid4().hex[:12]}"
            await indexer.index_watched_new(doc_id, p, wd, materialize, mtime_ns)

    async def _handle_delete(self, path: str) -> None:
        doc = self._sqlite.get_doc_by_watched_path(path)
        if doc is None:
            return
        await self._app.delete_doc(doc.id)

    async def _handle_rename(self, src: str, dest: str | None) -> None:
        if dest is None:
            await self._handle_delete(src)
            return
        doc = self._sqlite.get_doc_by_watched_path(src)
        if doc is None:
            await self._handle_upsert(dest)
            return
        new_wd = self._lookup_wd_for_path(Path(dest))
        if new_wd is None:
            await self._app.delete_doc(doc.id)
            return
        matcher = self._ignore_matchers.get(new_wd.id) or IgnoreMatcher(new_wd)
        if matcher.should_ignore(Path(dest)):
            await self._app.delete_doc(doc.id)
            return
        self._sqlite.update_watched_path(doc.id, dest)

    async def _reconcile_dir(self, wd) -> None:
        from docgraph.watch.reconcile import reconcile_dir
        try:
            await reconcile_dir(self, wd)
        except Exception:
            logger.exception("reconcile failed for wd=%s path=%s", wd.id, wd.path)

    async def _recovery_loop(self) -> None:
        """Periodic backstop: heal dead observer thread + reconcile drift."""
        try:
            while not self._shutdown.is_set():
                await asyncio.sleep(self._cfg.watch_recovery_interval_sec)
                if self._shutdown.is_set():
                    return
                # Heal a dead observer thread before reconciling so the next
                # cycle has live event capture again.
                if (self.state == WatcherState.ENABLED
                        and self._observer is not None
                        and not self._observer.is_alive()):
                    logger.warning(
                        "watcher: observer thread died, rebuilding"
                    )
                    try:
                        # Drop the dead observer first so _start_observer
                        # builds a fresh one against current watched_dirs.
                        self._observer = None
                        await self._start_observer()
                    except Exception:
                        logger.exception("observer restart failed")
                for wd in self._sqlite.list_watched_dirs():
                    await self._reconcile_dir(wd)
        except asyncio.CancelledError:
            return
