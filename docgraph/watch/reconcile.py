from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Iterator

from docgraph.models import WatchedDirRecord
from docgraph.watch.ignore import IgnoreMatcher
from docgraph.watch.types import WatchEvent

logger = logging.getLogger(__name__)

# Reconcile DELETEs are a backstop for missed fs events; live DELETE events are
# unaffected by these guards. Refuse a batch that would drop more than this
# share of a dir's docs (only once the dir tracks enough docs for a ratio to
# mean anything) — far more likely a half-mounted/renamed dir than real deletes.
MASS_DELETE_RATIO = 0.5
MASS_DELETE_MIN_DOCS = 10


def walk_watched_dir(
    wd: WatchedDirRecord, matcher: IgnoreMatcher, errors: list[OSError] | None = None
) -> Iterator[Path]:
    root = Path(wd.path)
    onerror = errors.append if errors is not None else None
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=onerror):
        dp = Path(dirpath)
        # Prune ignored dirs in-place (saves recursion).
        dirnames[:] = [
            d for d in dirnames
            if d not in {".git", "node_modules", "__pycache__", ".venv", ".hg", ".svn", "venv", ".tox"}
        ]
        for fn in filenames:
            p = dp / fn
            if matcher.should_ignore(p):
                continue
            yield p


async def reconcile_dir(manager, wd: WatchedDirRecord) -> None:
    """Disk-vs-DB delta scan. Enqueues UPSERT / DELETE events directly (bypass debounce)."""
    if not Path(wd.path).is_dir():
        # os.walk on a missing root yields nothing, which would read as "every
        # file deleted" and wipe the dir's docs (unmounted drive, renamed dir).
        logger.warning("reconcile skipped: watched dir missing or not a dir: %s", wd.path)
        return
    matcher = manager._ignore_matchers.get(wd.id) or IgnoreMatcher(wd)
    disk: dict[str, int] = {}
    walk_errors: list[OSError] = []
    count = 0
    for p in walk_watched_dir(wd, matcher, walk_errors):
        try:
            disk[str(p)] = p.stat().st_mtime_ns
        except FileNotFoundError:
            continue
        count += 1
        if count % 100 == 0:
            await asyncio.sleep(0)  # yield event loop

    db_docs = manager._sqlite.list_watched_docs(prefix=wd.path)
    db_by_path = {d.watched_path: d for d in db_docs}

    for path, mtime in disk.items():
        prev = db_by_path.get(path)
        if prev is None or prev.mtime_ns != mtime:
            manager._enqueue_direct(WatchEvent("UPSERT", path, None))
    missing = db_by_path.keys() - disk.keys()
    if missing and walk_errors:
        logger.warning(
            "reconcile dir=%s: %d walk errors (first: %s); skipping %d deletes",
            wd.path, len(walk_errors), walk_errors[0], len(missing),
        )
    elif (missing and len(db_by_path) >= MASS_DELETE_MIN_DOCS
          and len(missing) > len(db_by_path) * MASS_DELETE_RATIO):
        logger.warning(
            "reconcile dir=%s: refusing to delete %d of %d docs at once; "
            "remove them explicitly if intended",
            wd.path, len(missing), len(db_by_path),
        )
    else:
        for path in missing:
            manager._enqueue_direct(WatchEvent("DELETE", path, None))

    manager.stats.reconcile_runs += 1
    from datetime import datetime, timezone
    manager.stats.last_reconcile_at = datetime.now(timezone.utc).isoformat()
    logger.info("reconcile dir=%s files=%d", wd.path, len(disk))
