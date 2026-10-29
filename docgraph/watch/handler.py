from __future__ import annotations

import asyncio
import logging
from watchdog.events import FileSystemEventHandler

logger = logging.getLogger(__name__)


class DocgraphEventHandler(FileSystemEventHandler):
    """Watchdog handler that forwards events from the observer thread to the asyncio loop."""

    def __init__(self, manager, loop: asyncio.AbstractEventLoop) -> None:
        self._manager = manager
        self._loop = loop

    def on_any_event(self, event) -> None:
        if event.is_directory:
            return
        event_type = event.event_type  # 'created' | 'modified' | 'deleted' | 'moved'
        src = event.src_path
        dest = getattr(event, "dest_path", None)
        try:
            self._loop.call_soon_threadsafe(self._manager._on_raw_event, event_type, src, dest)
        except RuntimeError:
            # loop closed during shutdown — drop silently.
            pass
