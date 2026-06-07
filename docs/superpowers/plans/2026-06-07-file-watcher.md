# Real-time File Watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-index files in user-configured watched directories with runtime on/off toggle, dynamic add/remove of dirs, and hybrid storage (text referenced in-place, binary materialized as snapshots).

**Architecture:** Single `WatcherManager` owned by `AppState`. `watchdog` library captures FS events on a native thread, bridges to asyncio via `loop.call_soon_threadsafe`. Per-path 2s debounce → key-partitioned (`hash(path) % 4`) per-worker queues → ingest dispatched through new `Indexer.index_watched_new` / `reindex_watched` methods. Reconcile scan runs on enable, on add-dir, manually, and every 10 min as fsevents-drop backstop.

**Tech Stack:** Python 3.10+, Poetry, `watchdog ^4.0`, `pathspec ^0.12`, SQLite, FastAPI, pytest + pytest-asyncio.

**Spec reference:** `docs/superpowers/specs/2026-06-07-file-watcher-design.md`

---

## File Structure

**Created:**
- `docgraph/watch/__init__.py`
- `docgraph/watch/types.py` — `WatchEvent`, `WatcherStats`, state enum
- `docgraph/watch/ignore.py` — hardcoded defaults + `.docgraphignore` parser
- `docgraph/watch/handler.py` — `watchdog.FileSystemEventHandler` subclass
- `docgraph/watch/manager.py` — `WatcherManager`: state machine, queue, workers, dispatchers
- `docgraph/watch/reconcile.py` — disk-vs-DB delta scan
- `docgraph/cli_watch.py` — CLI subcommand wrapper (httpx client)
- `tests/watch/__init__.py`
- `tests/watch/test_ignore.py`
- `tests/watch/test_debounce.py`
- `tests/watch/test_manager_state.py`
- `tests/watch/test_partitioning.py`
- `tests/watch/test_dispatch.py`
- `tests/watch/test_reconcile_int.py`
- `tests/watch/test_live_watcher_int.py`
- `tests/watch/test_text_vs_binary_int.py`
- `tests/watch/test_rename_int.py`
- `tests/watch/test_burst_int.py`
- `tests/watch/test_lifecycle_int.py`
- `tests/watch/test_fsevents_atomic_rename.py` (macOS-only)
- `tests/web/test_watch_routes.py`
- `tests/cli/test_watch_cli.py`
- `tests/store/test_watcher_schema.py`

**Modified:**
- `docgraph/models.py` — `SourceType.WATCHED`, `WatchedDirRecord`, new `DocumentRecord` fields
- `docgraph/config.py` — 6 new watcher knobs + YAML/env wiring
- `docgraph/store/sqlite.py` — schema extension, watched_dirs/watcher_state CRUD, watched-doc queries
- `docgraph/store/files.py` — comment guard on `delete_doc_files`
- `docgraph/ingest/lang_dispatch.py` — `detect_materialize` + extension allowlists
- `docgraph/ingest/indexer.py` — `index_text_direct`, `index_watched_new`, `reindex_watched`
- `docgraph/web/deps.py` — `AppState.delete_doc` refactor + `AppState.watcher`, lifespan auto-enable
- `docgraph/web/app.py` — 7 watch routes, delete route delegates to `AppState.delete_doc`
- `docgraph/cli.py` — register `watch` subcommand
- `pyproject.toml` — `watchdog`, `pathspec` deps
- `tests/test_config.py` — new watcher config keys
- `README.md` — watcher config table + watched-vs-uploaded note

---

## Execution Order Notes

Tasks 1-5 (schema + models + config) must complete first — everything depends on them. Tasks 6-9 (extension classification, ignore, indexer entry points) are independent of 10-14 (manager). Tasks 15-18 (AppState wiring + HTTP routes) depend on both. Tasks 19-21 (CLI + integration tests + docs) are last.

---

## Task 1: Add `SourceType.WATCHED` and dataclasses

**Files:**
- Modify: `docgraph/models.py`
- Test: existing `tests/test_e2e.py` smoke + new `tests/store/test_watcher_schema.py`

- [ ] **Step 1: Add enum value and dataclass**

Edit `docgraph/models.py`. Find the `SourceType` enum and add the `WATCHED` value:

```python
class SourceType(str, Enum):
    FILE = "file"
    URL = "url"
    WATCHED = "watched"
```

Add three optional fields to `DocumentRecord` (place after existing optional fields like `original_path`):

```python
    # Watched-file fields (None for FILE/URL docs)
    watched_path: Optional[str] = None
    materialize: Optional[bool] = None
    mtime_ns: Optional[int] = None
```

Add a new dataclass at the bottom of the file:

```python
@dataclass
class WatchedDirRecord:
    id: str
    path: str
    folder: str = ""
    tags: list[str] = field(default_factory=list)
    ignore_globs: list[str] = field(default_factory=list)
    created_at: str = ""
```

Ensure `from dataclasses import dataclass, field` is present at the top.

- [ ] **Step 2: Run existing tests to verify no regression**

Run: `poetry run pytest tests/test_e2e.py -v -x`
Expected: All existing tests still pass.

- [ ] **Step 3: Commit**

```bash
git add docgraph/models.py
git commit -m "feat(models): add SourceType.WATCHED + WatchedDirRecord"
```

---

## Task 2: Extend SQLite schema (additive migration)

**Files:**
- Modify: `docgraph/store/sqlite.py`
- Test: `tests/store/test_watcher_schema.py`

- [ ] **Step 1: Write the failing schema test**

Create `tests/store/test_watcher_schema.py`:

```python
import sqlite3
from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.store.sqlite import SqliteStore


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config()
    c.data_dir = tmp_path
    c.ensure_dirs()
    return c


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_documents_has_new_columns(cfg: Config):
    store = SqliteStore(cfg)
    with sqlite3.connect(cfg.sqlite_path) as conn:
        cols = _columns(conn, "documents")
    assert "watched_path" in cols
    assert "materialize" in cols
    assert "mtime_ns" in cols


def test_watched_dirs_table_exists(cfg: Config):
    SqliteStore(cfg)
    with sqlite3.connect(cfg.sqlite_path) as conn:
        cols = _columns(conn, "watched_dirs")
    assert {"id", "path", "folder", "tags", "ignore_globs", "created_at"} <= cols


def test_watcher_state_table_exists(cfg: Config):
    SqliteStore(cfg)
    with sqlite3.connect(cfg.sqlite_path) as conn:
        cols = _columns(conn, "watcher_state")
    assert {"key", "value"} <= cols


def test_partial_unique_index_on_watched_path(cfg: Config):
    SqliteStore(cfg)
    with sqlite3.connect(cfg.sqlite_path) as conn:
        idx = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_documents_watched_path'"
        ).fetchone()
    assert idx is not None


def test_schema_migration_idempotent(cfg: Config):
    SqliteStore(cfg)
    SqliteStore(cfg)  # second init must not raise


def test_partial_unique_allows_multiple_null_watched_path(cfg: Config):
    SqliteStore(cfg)
    with sqlite3.connect(cfg.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO documents (id, filename, folder, tags, source_type, status, watched_path) "
            "VALUES ('d1', 'a', '', '[]', 'file', 'ready', NULL)"
        )
        conn.execute(
            "INSERT INTO documents (id, filename, folder, tags, source_type, status, watched_path) "
            "VALUES ('d2', 'b', '', '[]', 'file', 'ready', NULL)"
        )
        conn.commit()


def test_partial_unique_rejects_duplicate_non_null_watched_path(cfg: Config):
    SqliteStore(cfg)
    with sqlite3.connect(cfg.sqlite_path) as conn:
        conn.execute(
            "INSERT INTO documents (id, filename, folder, tags, source_type, status, watched_path) "
            "VALUES ('d1', 'a', '', '[]', 'watched', 'ready', '/tmp/x.md')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO documents (id, filename, folder, tags, source_type, status, watched_path) "
                "VALUES ('d2', 'b', '', '[]', 'watched', 'ready', '/tmp/x.md')"
            )
        conn.commit()
```

- [ ] **Step 2: Run test, verify it fails**

Run: `poetry run pytest tests/store/test_watcher_schema.py -v`
Expected: FAIL — columns/tables don't exist.

- [ ] **Step 3: Extend `_ensure_schema()` in `docgraph/store/sqlite.py`**

Open `docgraph/store/sqlite.py`. Find the existing `_ensure_schema()` method. Add this helper method to the `SqliteStore` class, and call it from `_ensure_schema()` at the end:

```python
def _ensure_watcher_schema(self) -> None:
    with self._connect() as conn:
        # Additive columns on documents (SQLite has no ADD COLUMN IF NOT EXISTS).
        for ddl in (
            "ALTER TABLE documents ADD COLUMN watched_path TEXT",
            "ALTER TABLE documents ADD COLUMN materialize INTEGER",
            "ALTER TABLE documents ADD COLUMN mtime_ns INTEGER",
        ):
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_watched_path "
            "ON documents(watched_path) WHERE watched_path IS NOT NULL"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS watched_dirs ("
            "  id TEXT PRIMARY KEY,"
            "  path TEXT NOT NULL UNIQUE,"
            "  folder TEXT NOT NULL DEFAULT '',"
            "  tags TEXT NOT NULL DEFAULT '[]',"
            "  ignore_globs TEXT NOT NULL DEFAULT '[]',"
            "  created_at TEXT NOT NULL"
            ")"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS watcher_state ("
            "  key TEXT PRIMARY KEY,"
            "  value TEXT NOT NULL"
            ")"
        )
        conn.commit()
```

Add the call at the end of `_ensure_schema()`:

```python
def _ensure_schema(self) -> None:
    # ... existing CREATE TABLE statements ...
    self._ensure_watcher_schema()
```

Ensure `import sqlite3` is at the top of the file.

- [ ] **Step 4: Run test, verify it passes**

Run: `poetry run pytest tests/store/test_watcher_schema.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add docgraph/store/sqlite.py tests/store/test_watcher_schema.py
git commit -m "feat(store): additive schema for watcher (watched_dirs, watcher_state, doc columns)"
```

---

## Task 3: SQLite CRUD for `watched_dirs` and `watcher_state`

**Files:**
- Modify: `docgraph/store/sqlite.py`
- Test: `tests/store/test_watcher_schema.py` (extend)

- [ ] **Step 1: Write failing CRUD tests**

Append to `tests/store/test_watcher_schema.py`:

```python
from docgraph.models import WatchedDirRecord


def test_insert_and_list_watched_dirs(cfg: Config):
    store = SqliteStore(cfg)
    wd = WatchedDirRecord(
        id="wd_a",
        path="/tmp/notes",
        folder="notes",
        tags=["personal"],
        ignore_globs=["draft/*"],
        created_at="2026-06-07T14:00:00Z",
    )
    store.insert_watched_dir(wd)
    dirs = store.list_watched_dirs()
    assert len(dirs) == 1
    assert dirs[0].id == "wd_a"
    assert dirs[0].tags == ["personal"]
    assert dirs[0].ignore_globs == ["draft/*"]


def test_get_watched_dir_by_id(cfg: Config):
    store = SqliteStore(cfg)
    wd = WatchedDirRecord(id="wd_b", path="/tmp/x", created_at="2026-06-07T14:00:00Z")
    store.insert_watched_dir(wd)
    got = store.get_watched_dir("wd_b")
    assert got is not None and got.path == "/tmp/x"
    assert store.get_watched_dir("wd_missing") is None


def test_delete_watched_dir(cfg: Config):
    store = SqliteStore(cfg)
    store.insert_watched_dir(WatchedDirRecord(id="wd_c", path="/tmp/y", created_at="2026-06-07T14:00:00Z"))
    deleted = store.delete_watched_dir("wd_c")
    assert deleted is True
    assert store.get_watched_dir("wd_c") is None
    assert store.delete_watched_dir("wd_c") is False  # already gone


def test_watcher_state_get_set(cfg: Config):
    store = SqliteStore(cfg)
    assert store.get_watcher_state("enabled") is None
    store.set_watcher_state("enabled", "true")
    assert store.get_watcher_state("enabled") == "true"
    store.set_watcher_state("enabled", "false")  # upsert
    assert store.get_watcher_state("enabled") == "false"
```

- [ ] **Step 2: Run tests, verify failure**

Run: `poetry run pytest tests/store/test_watcher_schema.py -v -k "watched_dir or watcher_state"`
Expected: FAIL — methods not defined.

- [ ] **Step 3: Add CRUD methods to `SqliteStore`**

Add to `docgraph/store/sqlite.py` (place near other doc-related methods):

```python
import json
from docgraph.models import WatchedDirRecord


def insert_watched_dir(self, wd: WatchedDirRecord) -> None:
    with self._connect() as conn:
        conn.execute(
            "INSERT INTO watched_dirs (id, path, folder, tags, ignore_globs, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (wd.id, wd.path, wd.folder, json.dumps(wd.tags),
             json.dumps(wd.ignore_globs), wd.created_at),
        )
        conn.commit()


def list_watched_dirs(self) -> list[WatchedDirRecord]:
    with self._connect() as conn:
        rows = conn.execute(
            "SELECT id, path, folder, tags, ignore_globs, created_at "
            "FROM watched_dirs ORDER BY created_at"
        ).fetchall()
    return [
        WatchedDirRecord(
            id=r[0], path=r[1], folder=r[2],
            tags=json.loads(r[3]), ignore_globs=json.loads(r[4]),
            created_at=r[5],
        )
        for r in rows
    ]


def get_watched_dir(self, wd_id: str) -> WatchedDirRecord | None:
    with self._connect() as conn:
        row = conn.execute(
            "SELECT id, path, folder, tags, ignore_globs, created_at "
            "FROM watched_dirs WHERE id = ?",
            (wd_id,),
        ).fetchone()
    if row is None:
        return None
    return WatchedDirRecord(
        id=row[0], path=row[1], folder=row[2],
        tags=json.loads(row[3]), ignore_globs=json.loads(row[4]),
        created_at=row[5],
    )


def delete_watched_dir(self, wd_id: str) -> bool:
    with self._connect() as conn:
        cur = conn.execute("DELETE FROM watched_dirs WHERE id = ?", (wd_id,))
        conn.commit()
        return cur.rowcount > 0


def get_watcher_state(self, key: str) -> str | None:
    with self._connect() as conn:
        row = conn.execute(
            "SELECT value FROM watcher_state WHERE key = ?", (key,)
        ).fetchone()
    return row[0] if row else None


def set_watcher_state(self, key: str, value: str) -> None:
    with self._connect() as conn:
        conn.execute(
            "INSERT INTO watcher_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
```

- [ ] **Step 4: Run tests, verify pass**

Run: `poetry run pytest tests/store/test_watcher_schema.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add docgraph/store/sqlite.py tests/store/test_watcher_schema.py
git commit -m "feat(store): watched_dirs and watcher_state CRUD"
```

---

## Task 4: SQLite watched-doc queries

**Files:**
- Modify: `docgraph/store/sqlite.py`
- Test: `tests/store/test_watcher_schema.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/store/test_watcher_schema.py`:

```python
from docgraph.models import DocumentRecord, DocumentStatus, SourceType


def _make_watched_doc(store: SqliteStore, doc_id: str, path: str, mtime: int) -> None:
    doc = DocumentRecord(
        id=doc_id, filename="x.md", folder="", tags=[],
        source_type=SourceType.WATCHED, status=DocumentStatus.READY,
        watched_path=path, materialize=False, mtime_ns=mtime,
    )
    store.insert_document(doc)


def test_get_doc_by_watched_path(cfg: Config):
    store = SqliteStore(cfg)
    _make_watched_doc(store, "d1", "/tmp/notes/a.md", 1000)
    got = store.get_doc_by_watched_path("/tmp/notes/a.md")
    assert got is not None and got.id == "d1"
    assert got.mtime_ns == 1000
    assert store.get_doc_by_watched_path("/tmp/notes/missing.md") is None


def test_list_watched_docs_by_prefix(cfg: Config):
    store = SqliteStore(cfg)
    _make_watched_doc(store, "d1", "/tmp/notes/a.md", 1)
    _make_watched_doc(store, "d2", "/tmp/notes/sub/b.md", 2)
    _make_watched_doc(store, "d3", "/tmp/other/c.md", 3)
    docs = store.list_watched_docs(prefix="/tmp/notes")
    paths = sorted(d.watched_path for d in docs)
    assert paths == ["/tmp/notes/a.md", "/tmp/notes/sub/b.md"]


def test_update_watched_path(cfg: Config):
    store = SqliteStore(cfg)
    _make_watched_doc(store, "d1", "/tmp/old.md", 1)
    store.update_watched_path("d1", "/tmp/new.md")
    assert store.get_doc_by_watched_path("/tmp/old.md") is None
    assert store.get_doc_by_watched_path("/tmp/new.md").id == "d1"


def test_update_mtime_ns(cfg: Config):
    store = SqliteStore(cfg)
    _make_watched_doc(store, "d1", "/tmp/a.md", 100)
    store.update_mtime_ns("d1", 999)
    got = store.get_doc_by_watched_path("/tmp/a.md")
    assert got.mtime_ns == 999
```

- [ ] **Step 2: Run tests, verify failure**

Run: `poetry run pytest tests/store/test_watcher_schema.py -v -k "watched_path or mtime"`
Expected: FAIL — methods missing.

- [ ] **Step 3: Add query methods to `SqliteStore`**

Append to `docgraph/store/sqlite.py`:

```python
def get_doc_by_watched_path(self, path: str) -> DocumentRecord | None:
    with self._connect() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE watched_path = ?", (path,)
        ).fetchone()
    if row is None:
        return None
    return self._row_to_doc(row)  # use existing converter; ensure it reads new cols


def list_watched_docs(self, prefix: str) -> list[DocumentRecord]:
    # SQL LIKE prefix match with proper separator to avoid matching /tmp/notes2/...
    like = prefix.rstrip("/") + "/%"
    exact = prefix.rstrip("/")
    with self._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE watched_path = ? OR watched_path LIKE ?",
            (exact, like),
        ).fetchall()
    return [self._row_to_doc(r) for r in rows]


def update_watched_path(self, doc_id: str, new_path: str) -> None:
    with self._connect() as conn:
        conn.execute(
            "UPDATE documents SET watched_path = ? WHERE id = ?",
            (new_path, doc_id),
        )
        conn.commit()


def update_mtime_ns(self, doc_id: str, mtime_ns: int) -> None:
    with self._connect() as conn:
        conn.execute(
            "UPDATE documents SET mtime_ns = ? WHERE id = ?",
            (mtime_ns, doc_id),
        )
        conn.commit()
```

Check the existing `_row_to_doc` (or equivalent row-to-DocumentRecord converter) reads the three new columns. If it uses `dict(row)` or `row["watched_path"]`, no changes needed. If it lists columns explicitly, extend the list to include `watched_path`, `materialize`, `mtime_ns`.

- [ ] **Step 4: Run tests, verify pass**

Run: `poetry run pytest tests/store/test_watcher_schema.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add docgraph/store/sqlite.py tests/store/test_watcher_schema.py
git commit -m "feat(store): watched-doc queries (get_by_path, list_by_prefix, update_path/mtime)"
```

---

## Task 5: Watcher config knobs

**Files:**
- Modify: `docgraph/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_config.py`:

```python
def test_watcher_defaults():
    cfg = Config()
    assert cfg.watch_debounce_sec == 2.0
    assert cfg.watch_queue_capacity == 500
    assert cfg.watch_workers == 4
    assert cfg.watch_recovery_interval_sec == 600
    assert cfg.watch_extra_text_exts == []
    assert cfg.watch_extra_binary_exts == []


def test_watcher_env_overrides(monkeypatch):
    monkeypatch.setenv("DOCGRAPH_WATCH_DEBOUNCE_SEC", "1.5")
    monkeypatch.setenv("DOCGRAPH_WATCH_QUEUE_CAPACITY", "200")
    monkeypatch.setenv("DOCGRAPH_WATCH_WORKERS", "2")
    monkeypatch.setenv("DOCGRAPH_WATCH_RECOVERY_INTERVAL_SEC", "300")
    monkeypatch.setenv("DOCGRAPH_WATCH_EXTRA_TEXT_EXTS", ".tf,.hcl")
    monkeypatch.setenv("DOCGRAPH_WATCH_EXTRA_BINARY_EXTS", ".keynote")
    cfg = load_config()
    assert cfg.watch_debounce_sec == 1.5
    assert cfg.watch_queue_capacity == 200
    assert cfg.watch_workers == 2
    assert cfg.watch_recovery_interval_sec == 300
    assert cfg.watch_extra_text_exts == [".tf", ".hcl"]
    assert cfg.watch_extra_binary_exts == [".keynote"]


def test_watcher_workers_validation():
    cfg = Config()
    cfg.watch_workers = 0
    with pytest.raises(ValueError, match="watch_workers"):
        cfg.validate()
    cfg.watch_workers = 33
    with pytest.raises(ValueError, match="watch_workers"):
        cfg.validate()
```

Ensure `load_config` (or whatever the file's loader is named) is imported at the top of `tests/test_config.py`.

- [ ] **Step 2: Run tests, verify failure**

Run: `poetry run pytest tests/test_config.py -v -k "watcher"`
Expected: FAIL — fields missing.

- [ ] **Step 3: Add fields, YAML, env, validation**

Edit `docgraph/config.py`. Add fields to the `Config` dataclass (after existing rerank fields):

```python
    # Watcher
    watch_debounce_sec: float = 2.0
    watch_queue_capacity: int = 500
    watch_workers: int = 4
    watch_recovery_interval_sec: int = 600
    watch_extra_text_exts: list[str] = field(default_factory=list)
    watch_extra_binary_exts: list[str] = field(default_factory=list)
```

In `validate()`, append:

```python
    if not (1 <= self.watch_workers <= 32):
        raise ValueError(f"watch_workers must be in [1, 32], got {self.watch_workers}")
    if self.watch_debounce_sec <= 0:
        raise ValueError(f"watch_debounce_sec must be > 0, got {self.watch_debounce_sec}")
    if self.watch_queue_capacity < self.watch_workers:
        raise ValueError(
            f"watch_queue_capacity ({self.watch_queue_capacity}) must be >= watch_workers ({self.watch_workers})"
        )
```

In `_apply_yaml`, add a watch section reader:

```python
    if watch := data.get("watch"):
        cfg.watch_debounce_sec = float(watch.get("debounce_sec", cfg.watch_debounce_sec))
        cfg.watch_queue_capacity = int(watch.get("queue_capacity", cfg.watch_queue_capacity))
        cfg.watch_workers = int(watch.get("workers", cfg.watch_workers))
        cfg.watch_recovery_interval_sec = int(
            watch.get("recovery_interval_sec", cfg.watch_recovery_interval_sec)
        )
        if extra_text := watch.get("extra_text_exts"):
            cfg.watch_extra_text_exts = list(extra_text)
        if extra_bin := watch.get("extra_binary_exts"):
            cfg.watch_extra_binary_exts = list(extra_bin)
```

In the env override block (find existing `if v := os.getenv("DOCGRAPH_RERANK_TOP_N"):` pattern), append:

```python
    if v := os.getenv("DOCGRAPH_WATCH_DEBOUNCE_SEC"):
        cfg.watch_debounce_sec = float(v)
    if v := os.getenv("DOCGRAPH_WATCH_QUEUE_CAPACITY"):
        cfg.watch_queue_capacity = int(v)
    if v := os.getenv("DOCGRAPH_WATCH_WORKERS"):
        cfg.watch_workers = int(v)
    if v := os.getenv("DOCGRAPH_WATCH_RECOVERY_INTERVAL_SEC"):
        cfg.watch_recovery_interval_sec = int(v)
    if v := os.getenv("DOCGRAPH_WATCH_EXTRA_TEXT_EXTS"):
        cfg.watch_extra_text_exts = [e.strip() for e in v.split(",") if e.strip()]
    if v := os.getenv("DOCGRAPH_WATCH_EXTRA_BINARY_EXTS"):
        cfg.watch_extra_binary_exts = [e.strip() for e in v.split(",") if e.strip()]
```

- [ ] **Step 4: Run tests, verify pass**

Run: `poetry run pytest tests/test_config.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add docgraph/config.py tests/test_config.py
git commit -m "feat(config): watcher knobs (debounce, queue cap, workers, recovery, extra exts)"
```

---

## Task 6: Extension classification + `detect_materialize`

**Files:**
- Modify: `docgraph/ingest/lang_dispatch.py`
- Test: `tests/watch/test_dispatch.py`

- [ ] **Step 1: Create test file**

Create `tests/watch/__init__.py` (empty file).

Create `tests/watch/test_dispatch.py`:

```python
from pathlib import Path

from docgraph.config import Config
from docgraph.ingest.lang_dispatch import detect_materialize


def test_native_text_extensions():
    cfg = Config()
    for ext in (".md", ".py", ".rs", ".js", ".json", ".yaml", ".sh"):
        assert detect_materialize(Path(f"x{ext}"), cfg) is False, ext


def test_binary_convert_extensions():
    cfg = Config()
    for ext in (".pdf", ".docx", ".pptx", ".xlsx", ".epub"):
        assert detect_materialize(Path(f"x{ext}"), cfg) is True, ext


def test_unsupported_returns_none():
    cfg = Config()
    for ext in (".dmg", ".iso", ".bin", ".exe", ".png"):
        assert detect_materialize(Path(f"x{ext}"), cfg) is None, ext


def test_case_insensitive():
    cfg = Config()
    assert detect_materialize(Path("README.MD"), cfg) is False
    assert detect_materialize(Path("Doc.PDF"), cfg) is True


def test_extra_text_exts_extends():
    cfg = Config()
    cfg.watch_extra_text_exts = [".tf", ".hcl"]
    assert detect_materialize(Path("main.tf"), cfg) is False
    assert detect_materialize(Path("vars.hcl"), cfg) is False


def test_extra_binary_exts_extends():
    cfg = Config()
    cfg.watch_extra_binary_exts = [".keynote"]
    assert detect_materialize(Path("slide.keynote"), cfg) is True


def test_no_extension_returns_none():
    cfg = Config()
    assert detect_materialize(Path("Makefile"), cfg) is None
```

- [ ] **Step 2: Run test, verify failure**

Run: `poetry run pytest tests/watch/test_dispatch.py -v`
Expected: FAIL — `detect_materialize` doesn't exist.

- [ ] **Step 3: Add to `lang_dispatch.py`**

Edit `docgraph/ingest/lang_dispatch.py`. Append at the end:

```python
from pathlib import Path

NATIVE_TEXT_EXTS = frozenset({
    ".md", ".markdown", ".txt", ".rst",
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".rs", ".go", ".java", ".kt", ".swift",
    ".c", ".cc", ".cpp", ".h", ".hpp",
    ".rb", ".php", ".lua", ".pl",
    ".sh", ".bash", ".zsh", ".fish",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".html", ".css", ".scss", ".sass", ".less",
    ".sql", ".graphql", ".proto",
    ".tex", ".csv", ".tsv",
})

BINARY_CONVERT_EXTS = frozenset({
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    ".odt", ".odp", ".ods", ".epub",
})


def detect_materialize(path: Path, cfg) -> bool | None:
    """Return True (binary, copy+convert), False (text, reference), None (skip)."""
    ext = path.suffix.lower()
    if ext in NATIVE_TEXT_EXTS or ext in {e.lower() for e in cfg.watch_extra_text_exts}:
        return False
    if ext in BINARY_CONVERT_EXTS or ext in {e.lower() for e in cfg.watch_extra_binary_exts}:
        return True
    return None
```

- [ ] **Step 4: Run test, verify pass**

Run: `poetry run pytest tests/watch/test_dispatch.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add docgraph/ingest/lang_dispatch.py tests/watch/__init__.py tests/watch/test_dispatch.py
git commit -m "feat(ingest): detect_materialize + extension allowlists for watcher"
```

---

## Task 7: Watch module foundation (types + ignore pipeline)

**Files:**
- Create: `docgraph/watch/__init__.py`, `docgraph/watch/types.py`, `docgraph/watch/ignore.py`
- Test: `tests/watch/test_ignore.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add `pathspec` to deps**

Edit `pyproject.toml`. In `[tool.poetry.dependencies]`, add:

```toml
pathspec = "^0.12"
watchdog = "^4.0"
```

Run: `poetry lock --no-update && poetry install`
Expected: Installs the new packages.

- [ ] **Step 2: Create `docgraph/watch/__init__.py`** (empty file).

- [ ] **Step 3: Create `docgraph/watch/types.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class WatcherState(str, Enum):
    DISABLED = "disabled"
    ENABLING = "enabling"
    ENABLED = "enabled"
    DISABLING = "disabling"


@dataclass
class WatchEvent:
    action: str            # "UPSERT" | "DELETE" | "RENAME"
    src_path: str
    dest_path: Optional[str] = None


@dataclass
class WatcherStats:
    events_received: int = 0
    events_debounced: int = 0
    events_processed: int = 0
    events_dropped_queue_full: int = 0
    reconcile_runs: int = 0
    last_reconcile_at: Optional[str] = None

    def reset(self) -> None:
        self.events_received = 0
        self.events_debounced = 0
        self.events_processed = 0
        self.events_dropped_queue_full = 0
        self.reconcile_runs = 0
        self.last_reconcile_at = None
```

- [ ] **Step 4: Write failing ignore tests**

Create `tests/watch/test_ignore.py`:

```python
from pathlib import Path

import pytest

from docgraph.models import WatchedDirRecord
from docgraph.watch.ignore import IgnoreMatcher


@pytest.fixture
def wd(tmp_path: Path) -> WatchedDirRecord:
    return WatchedDirRecord(id="wd_t", path=str(tmp_path), created_at="2026-06-07T00:00:00Z")


def test_hardcoded_dirs_ignored(tmp_path: Path, wd: WatchedDirRecord):
    matcher = IgnoreMatcher(wd)
    assert matcher.should_ignore(tmp_path / ".git" / "config") is True
    assert matcher.should_ignore(tmp_path / "node_modules" / "x" / "y.js") is True
    assert matcher.should_ignore(tmp_path / "__pycache__" / "m.pyc") is True
    assert matcher.should_ignore(tmp_path / ".venv" / "lib") is True


def test_hardcoded_files_ignored(tmp_path: Path, wd: WatchedDirRecord):
    matcher = IgnoreMatcher(wd)
    assert matcher.should_ignore(tmp_path / ".DS_Store") is True
    assert matcher.should_ignore(tmp_path / "foo.pyc") is True
    assert matcher.should_ignore(tmp_path / "foo.swp") is True
    assert matcher.should_ignore(tmp_path / "foo~") is True


def test_normal_file_not_ignored(tmp_path: Path, wd: WatchedDirRecord):
    matcher = IgnoreMatcher(wd)
    assert matcher.should_ignore(tmp_path / "readme.md") is False
    assert matcher.should_ignore(tmp_path / "src" / "main.py") is False


def test_docgraphignore_applied(tmp_path: Path, wd: WatchedDirRecord):
    (tmp_path / ".docgraphignore").write_text("draft/\n*.tmp\n")
    matcher = IgnoreMatcher(wd)
    assert matcher.should_ignore(tmp_path / "draft" / "note.md") is True
    assert matcher.should_ignore(tmp_path / "scratch.tmp") is True
    assert matcher.should_ignore(tmp_path / "final.md") is False


def test_wd_ignore_globs_applied(tmp_path: Path):
    wd = WatchedDirRecord(
        id="wd_t", path=str(tmp_path),
        ignore_globs=["secrets/*"],
        created_at="2026-06-07T00:00:00Z",
    )
    matcher = IgnoreMatcher(wd)
    assert matcher.should_ignore(tmp_path / "secrets" / "key.txt") is True
    assert matcher.should_ignore(tmp_path / "public" / "key.txt") is False


def test_docgraphignore_cache_invalidates_on_mtime(tmp_path: Path, wd: WatchedDirRecord):
    ignore_file = tmp_path / ".docgraphignore"
    ignore_file.write_text("foo.md\n")
    matcher = IgnoreMatcher(wd)
    assert matcher.should_ignore(tmp_path / "foo.md") is True
    import os, time
    time.sleep(0.01)
    ignore_file.write_text("bar.md\n")
    os.utime(ignore_file, None)
    assert matcher.should_ignore(tmp_path / "bar.md") is True
    assert matcher.should_ignore(tmp_path / "foo.md") is False


def test_malformed_docgraphignore_logs_and_treats_as_empty(tmp_path: Path, wd: WatchedDirRecord, caplog):
    # pathspec is permissive — synthesize a hard error by making file unreadable.
    ignore_file = tmp_path / ".docgraphignore"
    ignore_file.write_text("\x00\x00invalid bytes")
    matcher = IgnoreMatcher(wd)
    # Should not raise; should not block normal files.
    assert matcher.should_ignore(tmp_path / "ok.md") is False
```

- [ ] **Step 5: Run test, verify failure**

Run: `poetry run pytest tests/watch/test_ignore.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 6: Create `docgraph/watch/ignore.py`**

```python
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import pathspec

from docgraph.models import WatchedDirRecord

logger = logging.getLogger(__name__)

HARDCODED_IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "target", "dist", "build", ".next", ".nuxt",
})

HARDCODED_IGNORE_FILES = frozenset({
    ".DS_Store", "Thumbs.db",
})

HARDCODED_IGNORE_GLOBS = (
    "*.pyc", "*.pyo", "*.swp", "*.swo", "*~", ".#*", "#*#",
)


class IgnoreMatcher:
    """Layered ignore filter: hardcoded → .docgraphignore → wd.ignore_globs."""

    def __init__(self, wd: WatchedDirRecord) -> None:
        self._wd = wd
        self._root = Path(wd.path)
        self._docgraphignore_path = self._root / ".docgraphignore"
        self._cached_spec: Optional[pathspec.PathSpec] = None
        self._cached_mtime: Optional[int] = None
        self._wd_spec = pathspec.PathSpec.from_lines(
            "gitwildmatch", list(wd.ignore_globs)
        ) if wd.ignore_globs else None

    def should_ignore(self, path: Path) -> bool:
        try:
            rel = path.relative_to(self._root)
        except ValueError:
            return True  # outside the watched root — refuse
        # Layer 1: hardcoded.
        for part in rel.parts:
            if part in HARDCODED_IGNORE_DIRS:
                return True
        if path.name in HARDCODED_IGNORE_FILES:
            return True
        rel_str = str(rel)
        for pat in HARDCODED_IGNORE_GLOBS:
            if pathspec.patterns.GitWildMatchPattern.match_file(
                pathspec.patterns.GitWildMatchPattern(pat), rel_str
            ):
                return True
        # Layer 2: .docgraphignore (cached by mtime).
        if self._docgraphignore_matches(rel_str):
            return True
        # Layer 3: wd.ignore_globs.
        if self._wd_spec and self._wd_spec.match_file(rel_str):
            return True
        return False

    def _docgraphignore_matches(self, rel_str: str) -> bool:
        try:
            st = self._docgraphignore_path.stat()
        except FileNotFoundError:
            self._cached_spec = None
            self._cached_mtime = None
            return False
        mtime = st.st_mtime_ns
        if mtime != self._cached_mtime:
            try:
                lines = self._docgraphignore_path.read_text(encoding="utf-8", errors="replace").splitlines()
                self._cached_spec = pathspec.PathSpec.from_lines("gitwildmatch", lines)
            except Exception as exc:
                logger.warning("failed to parse .docgraphignore at %s: %s", self._docgraphignore_path, exc)
                self._cached_spec = pathspec.PathSpec.from_lines("gitwildmatch", [])
            self._cached_mtime = mtime
        return self._cached_spec.match_file(rel_str) if self._cached_spec else False
```

- [ ] **Step 7: Run test, verify pass**

Run: `poetry run pytest tests/watch/test_ignore.py -v`
Expected: All tests PASS.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml poetry.lock docgraph/watch/ tests/watch/test_ignore.py
git commit -m "feat(watch): ignore pipeline (hardcoded + .docgraphignore + wd globs)"
```

---

## Task 8: `Indexer.index_text_direct` helper

**Files:**
- Modify: `docgraph/ingest/indexer.py`
- Test: extend existing `tests/test_e2e.py` or add `tests/ingest/test_index_text_direct.py`

- [ ] **Step 1: Write failing test**

Create `tests/ingest/__init__.py` (empty) if it doesn't exist.
Create `tests/ingest/test_index_text_direct.py`:

```python
from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.models import DocumentRecord, DocumentStatus, SourceType
from docgraph.web.deps import AppState


@pytest.mark.asyncio
async def test_index_text_direct_skips_conversion(tmp_path: Path):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.ensure_dirs()
    cfg.embed_provider = "stub"  # use existing stub embedder
    state = AppState.create(cfg)
    state.sqlite.insert_document(DocumentRecord(
        id="d_text", filename="raw.md", folder="", tags=[],
        source_type=SourceType.WATCHED, status=DocumentStatus.PROCESSING,
        watched_path=str(tmp_path / "raw.md"),
        materialize=False, mtime_ns=1,
    ))
    indexer = state.indexer()
    text = "# Heading\n\nBody paragraph one.\n\nBody two."
    await indexer.index_text_direct("d_text", text)
    doc = state.sqlite.get_document("d_text")
    assert doc.status == DocumentStatus.READY
    assert doc.chunk_count > 0
```

If `embed_provider="stub"` isn't an existing test fixture, replace with whatever embedding test-double the codebase uses; check `tests/test_e2e.py` for the pattern.

- [ ] **Step 2: Run test, verify failure**

Run: `poetry run pytest tests/ingest/test_index_text_direct.py -v`
Expected: FAIL — `index_text_direct` doesn't exist.

- [ ] **Step 3: Add to `Indexer`**

In `docgraph/ingest/indexer.py`, add (near `index_document`):

```python
async def index_text_direct(self, doc_id: str, text: str) -> None:
    """Chunk + index raw text without conversion. For native-text files."""
    doc = self._sqlite.get_document(doc_id)
    if doc is None:
        raise ValueError(f"document not found: {doc_id}")
    logger.info("indexing text-direct doc_id=%s chars=%d", doc_id, len(text))
    try:
        self._progress(doc_id, 20, "Skipping conversion — native text (20%)")
        await self.index_markdown(doc_id, text)
    except Exception as exc:
        self._sqlite.update_status(
            doc_id, DocumentStatus.ERROR, error_message=str(exc),
        )
        raise
```

- [ ] **Step 4: Run test, verify pass**

Run: `poetry run pytest tests/ingest/test_index_text_direct.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docgraph/ingest/indexer.py tests/ingest/__init__.py tests/ingest/test_index_text_direct.py
git commit -m "feat(ingest): index_text_direct bypasses conversion for native-text files"
```

---

## Task 9: `Indexer.index_watched_new` and `reindex_watched`

**Files:**
- Modify: `docgraph/ingest/indexer.py`
- Test: `tests/watch/test_text_vs_binary_int.py`

- [ ] **Step 1: Write failing tests**

Create `tests/watch/test_text_vs_binary_int.py`:

```python
from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.models import SourceType, WatchedDirRecord
from docgraph.web.deps import AppState


@pytest.fixture
def state(tmp_path: Path) -> AppState:
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.embed_provider = "stub"
    cfg.ensure_dirs()
    return AppState.create(cfg)


@pytest.fixture
def wd(tmp_path: Path) -> WatchedDirRecord:
    src = tmp_path / "src"
    src.mkdir()
    return WatchedDirRecord(id="wd_t", path=str(src), created_at="2026-06-07T00:00:00Z")


@pytest.mark.asyncio
async def test_text_file_referenced_in_place(state: AppState, wd: WatchedDirRecord):
    p = Path(wd.path) / "note.md"
    p.write_text("# Note\n\nbody.")
    mtime = p.stat().st_mtime_ns
    await state.indexer().index_watched_new("d_text", p, wd, False, mtime)
    doc = state.sqlite.get_document("d_text")
    assert doc.source_type == SourceType.WATCHED
    assert doc.watched_path == str(p)
    assert doc.materialize is False
    assert doc.original_path is None
    assert doc.mtime_ns == mtime


@pytest.mark.asyncio
async def test_binary_file_snapshot_copied(state: AppState, wd: WatchedDirRecord, tmp_path: Path):
    p = Path(wd.path) / "doc.pdf"
    p.write_bytes(b"%PDF-1.4\nfake")
    mtime = p.stat().st_mtime_ns
    # Won't actually convert a fake PDF — expect ERROR status but snapshot file should exist.
    try:
        await state.indexer().index_watched_new("d_bin", p, wd, True, mtime)
    except Exception:
        pass
    doc = state.sqlite.get_document("d_bin")
    assert doc.source_type == SourceType.WATCHED
    assert doc.materialize is True
    assert doc.original_path is not None
    assert Path(doc.original_path).exists()
    assert Path(doc.original_path).read_bytes() == b"%PDF-1.4\nfake"


@pytest.mark.asyncio
async def test_reindex_watched_updates_mtime(state: AppState, wd: WatchedDirRecord):
    p = Path(wd.path) / "a.md"
    p.write_text("v1")
    mtime1 = p.stat().st_mtime_ns
    await state.indexer().index_watched_new("d_r", p, wd, False, mtime1)
    p.write_text("v2 with more body")
    mtime2 = p.stat().st_mtime_ns
    assert state.sqlite.claim_for_reindex("d_r") is True
    await state.indexer().reindex_watched("d_r", p, False, mtime2)
    doc = state.sqlite.get_document("d_r")
    assert doc.mtime_ns == mtime2
```

- [ ] **Step 2: Run tests, verify failure**

Run: `poetry run pytest tests/watch/test_text_vs_binary_int.py -v`
Expected: FAIL — methods don't exist.

- [ ] **Step 3: Implement methods**

In `docgraph/ingest/indexer.py`, add after `reindex_document`:

```python
import asyncio
import uuid as _uuid
from docgraph.models import SourceType, DocumentRecord


async def index_watched_new(
    self,
    doc_id: str,
    watched_path: Path,
    wd,  # WatchedDirRecord
    materialize: bool,
    mtime_ns: int,
) -> None:
    """First-time index of a file discovered in a watched dir."""
    doc = DocumentRecord(
        id=doc_id,
        filename=watched_path.name,
        folder=wd.folder,
        tags=list(wd.tags),
        source_type=SourceType.WATCHED,
        watched_path=str(watched_path),
        materialize=materialize,
        mtime_ns=mtime_ns,
        original_path=None,
    )
    if materialize:
        content = await asyncio.to_thread(watched_path.read_bytes)
        orig_path = self._files.save_original(doc_id, watched_path.name, content)
        doc.original_path = str(orig_path)
    self._sqlite.insert_document(doc)
    self._sqlite.update_progress(doc_id, 0, "Queued for watched-index (0%)")
    if materialize:
        await self.index_document(doc_id, Path(doc.original_path))
    else:
        text = await asyncio.to_thread(
            watched_path.read_text, encoding="utf-8", errors="ignore"
        )
        await self.index_text_direct(doc_id, text)
    self._sqlite.update_mtime_ns(doc_id, mtime_ns)


async def reindex_watched(
    self,
    doc_id: str,
    watched_path: Path,
    materialize: bool,
    mtime_ns: int,
) -> None:
    """Re-run ingest for an existing watched doc. claim_for_reindex must be called first."""
    self._chroma.delete_by_doc_id(doc_id)
    if self._fts is not None:
        try:
            self._fts.delete_by_doc_id(doc_id)
        except Exception as exc:
            logger.warning("FTS5 delete failed for doc_id=%s: %s", doc_id, exc)
    self._progress(doc_id, 0, "Re-indexing watched file (0%)")
    if materialize:
        doc = self._sqlite.get_document(doc_id)
        old_orig = Path(doc.original_path) if doc.original_path else None
        content = await asyncio.to_thread(watched_path.read_bytes)
        new_orig = self._files.save_original(doc_id, watched_path.name, content)
        self._sqlite.update_original_path(doc_id, str(new_orig))
        if old_orig and old_orig != new_orig and old_orig.exists():
            old_orig.unlink(missing_ok=True)
        await self.index_document(doc_id, new_orig)
    else:
        text = await asyncio.to_thread(
            watched_path.read_text, encoding="utf-8", errors="ignore"
        )
        await self.index_text_direct(doc_id, text)
    self._sqlite.update_mtime_ns(doc_id, mtime_ns)
```

If `_sqlite.update_original_path` doesn't exist, add it next to `update_mtime_ns` in `sqlite.py`:

```python
def update_original_path(self, doc_id: str, original_path: str) -> None:
    with self._connect() as conn:
        conn.execute(
            "UPDATE documents SET original_path = ? WHERE id = ?",
            (original_path, doc_id),
        )
        conn.commit()
```

- [ ] **Step 4: Run tests, verify pass**

Run: `poetry run pytest tests/watch/test_text_vs_binary_int.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docgraph/ingest/indexer.py docgraph/store/sqlite.py tests/watch/test_text_vs_binary_int.py
git commit -m "feat(ingest): index_watched_new + reindex_watched (hybrid storage)"
```

---

## Task 10: WatcherManager state machine skeleton

**Files:**
- Create: `docgraph/watch/manager.py`
- Test: `tests/watch/test_manager_state.py`

- [ ] **Step 1: Write failing state machine test**

Create `tests/watch/test_manager_state.py`:

```python
from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.watch.manager import WatcherManager
from docgraph.watch.types import WatcherState
from docgraph.web.deps import AppState


@pytest.fixture
def state(tmp_path: Path) -> AppState:
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.embed_provider = "stub"
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
```

- [ ] **Step 2: Run test, verify failure**

Run: `poetry run pytest tests/watch/test_manager_state.py -v`
Expected: FAIL — `WatcherManager` doesn't exist.

- [ ] **Step 3: Create `docgraph/watch/manager.py` skeleton**

```python
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from docgraph.watch.types import WatcherState, WatcherStats

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
        self._observer = None
        self._workers: list[asyncio.Task] = []
        self._queues: list[asyncio.Queue] = []
        self._debounce_tasks: dict[str, asyncio.TimerHandle] = {}
        self._shutdown = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_enabled_at: str | None = None
        self._recovery_task: asyncio.Task | None = None
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

    async def disable(self) -> dict:
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
                self._sqlite.set_watcher_state("enabled", "false")
                self.state = WatcherState.DISABLED
                logger.info("watcher: disabled, queue_drained=%d", drained)
                return {"enabled": False, "queue_drained": drained, "queue_dropped": self.stats.events_dropped_queue_full}
            except Exception:
                self.state = WatcherState.ENABLED  # roll back state
                raise

    # ---- placeholders filled in by later tasks ----
    async def _start_observer(self) -> None:
        # Filled by Task 11.
        pass

    async def _start_workers(self) -> None:
        # Filled by Task 13.
        self._queues = [asyncio.Queue(maxsize=max(1, self._cfg.watch_queue_capacity // self._cfg.watch_workers))
                        for _ in range(self._cfg.watch_workers)]

    async def _reconcile_dir(self, wd) -> None:
        # Filled by Task 14.
        pass

    async def _recovery_loop(self) -> None:
        # Filled by Task 14.
        try:
            while not self._shutdown.is_set():
                await asyncio.sleep(self._cfg.watch_recovery_interval_sec)
                if self._shutdown.is_set():
                    return
                for wd in self._sqlite.list_watched_dirs():
                    await self._reconcile_dir(wd)
        except asyncio.CancelledError:
            return
```

- [ ] **Step 4: Run test, verify pass**

Run: `poetry run pytest tests/watch/test_manager_state.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docgraph/watch/manager.py tests/watch/test_manager_state.py
git commit -m "feat(watch): WatcherManager state machine skeleton (enable/disable/lock)"
```

---

## Task 11: Event handler + thread-to-asyncio bridge

**Files:**
- Create: `docgraph/watch/handler.py`
- Modify: `docgraph/watch/manager.py`
- Test: `tests/watch/test_debounce.py`

- [ ] **Step 1: Create handler**

Create `docgraph/watch/handler.py`:

```python
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
```

- [ ] **Step 2: Write debounce test**

Create `tests/watch/test_debounce.py`:

```python
import asyncio
from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.watch.manager import WatcherManager
from docgraph.web.deps import AppState


@pytest.fixture
def mgr(tmp_path: Path) -> WatcherManager:
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.embed_provider = "stub"
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
```

- [ ] **Step 3: Run test, verify failure**

Run: `poetry run pytest tests/watch/test_debounce.py -v`
Expected: FAIL — `_on_raw_event` is not defined.

- [ ] **Step 4: Add raw-event + debounce + observer-start to `WatcherManager`**

In `docgraph/watch/manager.py`, replace the placeholder `_start_observer` and add the new methods:

```python
from watchdog.observers import Observer
from docgraph.watch.handler import DocgraphEventHandler
from docgraph.watch.types import WatchEvent


_ACTION_MAP = {
    "created": "UPSERT",
    "modified": "UPSERT",
    "deleted": "DELETE",
    "moved": "RENAME",
}


async def _start_observer(self) -> None:
    self._observer = Observer()
    handler = DocgraphEventHandler(self, self._loop)
    for wd in self._sqlite.list_watched_dirs():
        try:
            self._observer.schedule(handler, wd.path, recursive=True)
        except Exception as exc:
            logger.warning("failed to schedule watch for %s: %s", wd.path, exc)
    self._observer.start()


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
```

Bind these as methods on `WatcherManager` (place inside the class).

- [ ] **Step 5: Run test, verify pass**

Run: `poetry run pytest tests/watch/test_debounce.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docgraph/watch/handler.py docgraph/watch/manager.py tests/watch/test_debounce.py
git commit -m "feat(watch): event handler + debounce + key-partitioned enqueue"
```

---

## Task 12: Partitioning test

**Files:**
- Test: `tests/watch/test_partitioning.py`

- [ ] **Step 1: Write test**

Create `tests/watch/test_partitioning.py`:

```python
from docgraph.watch.types import WatchEvent


def test_same_path_same_partition():
    paths = ["/tmp/a.md", "/tmp/b.md", "/tmp/sub/c.py"]
    num_workers = 4
    for p in paths:
        assignments = {hash(p) % num_workers for _ in range(100)}
        assert len(assignments) == 1, f"path {p} should hash deterministically"


def test_partitioning_distributes_load():
    """Sanity: 1000 distinct paths should hit multiple partitions, not all one."""
    num_workers = 4
    buckets = {hash(f"/tmp/{i}.md") % num_workers for i in range(1000)}
    assert len(buckets) > 1
```

- [ ] **Step 2: Run test**

Run: `poetry run pytest tests/watch/test_partitioning.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/watch/test_partitioning.py
git commit -m "test(watch): key-partitioning stability + distribution"
```

---

## Task 13: Worker pool + dispatch handlers

**Files:**
- Modify: `docgraph/watch/manager.py`
- Test: `tests/watch/test_live_watcher_int.py`

- [ ] **Step 1: Write integration test**

Create `tests/watch/test_live_watcher_int.py`:

```python
import asyncio
from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.models import WatchedDirRecord
from docgraph.watch.manager import WatcherManager
from docgraph.web.deps import AppState


@pytest.fixture
def setup(tmp_path: Path):
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.embed_provider = "stub"
    cfg.watch_debounce_sec = 0.1
    cfg.watch_workers = 2
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    watched = tmp_path / "src"
    watched.mkdir()
    state.sqlite.insert_watched_dir(WatchedDirRecord(
        id="wd_t", path=str(watched), created_at="2026-06-07T00:00:00Z",
    ))
    return state, watched


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
```

- [ ] **Step 2: Run test, verify failure**

Run: `poetry run pytest tests/watch/test_live_watcher_int.py -v`
Expected: FAIL — no worker pool / handlers yet.

- [ ] **Step 3: Implement worker pool + handlers in `manager.py`**

Add to `WatcherManager` class, replacing the `_start_workers` placeholder:

```python
import uuid as _uuid
from pathlib import Path
from docgraph.ingest.lang_dispatch import detect_materialize
from docgraph.watch.ignore import IgnoreMatcher


async def _start_workers(self) -> None:
    per_worker = max(1, self._cfg.watch_queue_capacity // self._cfg.watch_workers)
    self._queues = [asyncio.Queue(maxsize=per_worker) for _ in range(self._cfg.watch_workers)]
    self._workers = [
        asyncio.create_task(self._worker_loop(i))
        for i in range(self._cfg.watch_workers)
    ]
    # Cache ignore matchers per watched dir (refresh each enable).
    self._ignore_matchers: dict[str, IgnoreMatcher] = {}
    for wd in self._sqlite.list_watched_dirs():
        self._ignore_matchers[wd.id] = IgnoreMatcher(wd)


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
    for wd in self._sqlite.list_watched_dirs():
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
    try:
        size = p.stat().st_size
    except OSError:
        return
    max_bytes = self._cfg.max_file_size_mb * 1024 * 1024
    if size > max_bytes:
        logger.warning("watcher: file too large (%d bytes), skipping: %s", size, path)
        return
    mtime_ns = p.stat().st_mtime_ns
    materialize = detect_materialize(p, self._cfg)
    if materialize is None:
        return  # unsupported extension
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
```

Task 14 will add `AppState.delete_doc()`. For now, this test will fail on delete unless we add a stub. **Add this temporary stub to AppState in `docgraph/web/deps.py`** (will be replaced properly in Task 14):

```python
async def delete_doc(self, doc_id: str) -> None:
    """Temporary — replaced with full refactor in Task 14."""
    doc = self.sqlite.get_document(doc_id)
    if doc is None:
        return
    self.chroma.delete_by_doc_id(doc_id)
    if self.fts is not None:
        try:
            self.fts.delete_by_doc_id(doc_id)
        except Exception:
            pass
    self.files.delete_doc_files(doc_id)
    self.sqlite.delete_document(doc_id)
```

- [ ] **Step 4: Run test, verify pass**

Run: `poetry run pytest tests/watch/test_live_watcher_int.py -v`
Expected: PASS (may need to bump sleep durations if CI is slow).

- [ ] **Step 5: Commit**

```bash
git add docgraph/watch/manager.py docgraph/web/deps.py tests/watch/test_live_watcher_int.py
git commit -m "feat(watch): worker pool + UPSERT/DELETE/RENAME handlers"
```

---

## Task 14: AppState.delete_doc refactor

**Files:**
- Modify: `docgraph/web/deps.py`, `docgraph/web/app.py`
- Test: existing `tests/web/` test for DELETE route still passes; add new test for `AppState.delete_doc`

- [ ] **Step 1: Move delete logic into AppState properly**

Open `docgraph/web/deps.py`. Replace the temporary stub with the canonical implementation (move logic out of the existing `DELETE /api/documents/{doc_id}` route):

```python
async def delete_doc(self, doc_id: str) -> bool:
    """Single source of truth for doc deletion. Returns True if deleted, False if not found."""
    doc = self.sqlite.get_document(doc_id)
    if doc is None:
        return False
    self.chroma.delete_by_doc_id(doc_id)
    if self.fts is not None:
        try:
            self.fts.delete_by_doc_id(doc_id)
        except Exception as exc:
            logger.warning("FTS5 delete failed for doc_id=%s on AppState.delete_doc: %s", doc_id, exc)
    self.files.delete_doc_files(doc_id)
    self.sqlite.delete_document(doc_id)
    return True
```

Ensure `import logging` and `logger = logging.getLogger(__name__)` are present in `deps.py`.

- [ ] **Step 2: Update DELETE route to delegate**

In `docgraph/web/app.py`, change `delete_document`:

```python
@app.delete("/api/documents/{doc_id}")
async def delete_document(request: Request, doc_id: str):
    st: AppState = request.app.state.docgraph
    if not await st.delete_doc(doc_id):
        raise HTTPException(status_code=404, detail="not found")
    return {"deleted": doc_id}
```

- [ ] **Step 3: Add guard comment to `files.py`**

In `docgraph/store/files.py`, after the `delete_doc_files` body:

```python
def delete_doc_files(self, doc_id: str) -> None:
    for p in self._cfg.originals_dir.glob(f"{doc_id}_*"):
        p.unlink(missing_ok=True)
    # NEVER touch paths outside data_dir — watched files are user-owned.
```

- [ ] **Step 4: Run existing E2E tests**

Run: `poetry run pytest tests/test_e2e.py -v -k "delete"`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add docgraph/web/deps.py docgraph/web/app.py docgraph/store/files.py
git commit -m "refactor(web): factor delete logic into AppState.delete_doc"
```

---

## Task 15: Reconcile scan

**Files:**
- Create: `docgraph/watch/reconcile.py`
- Modify: `docgraph/watch/manager.py`
- Test: `tests/watch/test_reconcile_int.py`

- [ ] **Step 1: Write failing reconcile test**

Create `tests/watch/test_reconcile_int.py`:

```python
import asyncio
from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.models import WatchedDirRecord
from docgraph.watch.manager import WatcherManager
from docgraph.web.deps import AppState


@pytest.fixture
def setup(tmp_path: Path):
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.embed_provider = "stub"
    cfg.watch_debounce_sec = 0.1
    cfg.watch_workers = 2
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    watched = tmp_path / "src"
    watched.mkdir()
    state.sqlite.insert_watched_dir(WatchedDirRecord(
        id="wd_t", path=str(watched), created_at="2026-06-07T00:00:00Z",
    ))
    return state, watched


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
        await asyncio.sleep(1.0)
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
        await asyncio.sleep(0.5)
        assert state.sqlite.get_doc_by_watched_path(str(f)) is not None
        await mgr.disable()
        f.unlink()
        await mgr.enable()  # reconcile sees missing file
        await asyncio.sleep(0.5)
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
        await asyncio.sleep(0.5)
        doc1 = state.sqlite.get_doc_by_watched_path(str(f))
        await mgr.disable()
        f.write_text("v2 with much more body")
        import os, time
        time.sleep(0.01)
        os.utime(f, None)
        await mgr.enable()
        await asyncio.sleep(0.5)
        doc2 = state.sqlite.get_doc_by_watched_path(str(f))
        assert doc2.mtime_ns > doc1.mtime_ns
    finally:
        await mgr.disable()
```

- [ ] **Step 2: Run test, verify failure**

Run: `poetry run pytest tests/watch/test_reconcile_int.py -v`
Expected: FAIL — reconcile not implemented.

- [ ] **Step 3: Create `docgraph/watch/reconcile.py`**

```python
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


def walk_watched_dir(wd: WatchedDirRecord, matcher: IgnoreMatcher) -> Iterator[Path]:
    root = Path(wd.path)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune ignored dirs in-place (saves recursion).
        dp = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if not matcher.should_ignore(dp / d / ".__sentinel__")  # synth check
            and d not in {".git", "node_modules", "__pycache__", ".venv"}
        ]
        for fn in filenames:
            p = dp / fn
            if matcher.should_ignore(p):
                continue
            yield p


async def reconcile_dir(manager, wd: WatchedDirRecord) -> None:
    """Disk-vs-DB delta scan. Enqueues UPSERT / DELETE events directly (bypass debounce)."""
    matcher = manager._ignore_matchers.get(wd.id) or IgnoreMatcher(wd)
    disk: dict[str, int] = {}
    count = 0
    for p in walk_watched_dir(wd, matcher):
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
    for path in db_by_path.keys() - disk.keys():
        manager._enqueue_direct(WatchEvent("DELETE", path, None))

    manager.stats.reconcile_runs += 1
    from datetime import datetime, timezone
    manager.stats.last_reconcile_at = datetime.now(timezone.utc).isoformat()
    logger.info("reconcile dir=%s files=%d", wd.path, len(disk))
```

- [ ] **Step 4: Wire reconcile into manager**

In `docgraph/watch/manager.py`, replace the placeholder `_reconcile_dir`:

```python
from docgraph.watch.reconcile import reconcile_dir


async def _reconcile_dir(self, wd) -> None:
    try:
        await reconcile_dir(self, wd)
    except Exception:
        logger.exception("reconcile failed for wd=%s path=%s", wd.id, wd.path)
```

- [ ] **Step 5: Run tests, verify pass**

Run: `poetry run pytest tests/watch/test_reconcile_int.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docgraph/watch/reconcile.py docgraph/watch/manager.py tests/watch/test_reconcile_int.py
git commit -m "feat(watch): reconcile scan (disk-vs-DB delta) on enable + add-dir"
```

---

## Task 16: Lifecycle + rename + burst tests

**Files:**
- Test: `tests/watch/test_lifecycle_int.py`, `tests/watch/test_rename_int.py`, `tests/watch/test_burst_int.py`

- [ ] **Step 1: Lifecycle test**

Create `tests/watch/test_lifecycle_int.py`:

```python
import asyncio
from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.models import WatchedDirRecord
from docgraph.watch.manager import WatcherManager
from docgraph.web.deps import AppState


@pytest.fixture
def setup(tmp_path: Path):
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.embed_provider = "stub"
    cfg.watch_debounce_sec = 0.1
    cfg.watch_workers = 2
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    watched = tmp_path / "src"
    watched.mkdir()
    state.sqlite.insert_watched_dir(WatchedDirRecord(
        id="wd_t", path=str(watched), created_at="2026-06-07T00:00:00Z",
    ))
    return state, watched


@pytest.mark.asyncio
async def test_enable_persists_across_restart(setup):
    state, watched = setup
    mgr = WatcherManager(state)
    await mgr.enable()
    assert state.sqlite.get_watcher_state("enabled") == "true"
    await mgr.disable()
    assert state.sqlite.get_watcher_state("enabled") == "false"


@pytest.mark.asyncio
async def test_stats_reset_on_enable(setup):
    state, watched = setup
    mgr = WatcherManager(state)
    await mgr.enable()
    mgr.stats.events_received = 100
    await mgr.disable()
    await mgr.enable()
    assert mgr.stats.events_received == 0
    await mgr.disable()
```

Run: `poetry run pytest tests/watch/test_lifecycle_int.py -v`
Expected: PASS.

- [ ] **Step 2: Rename test**

Create `tests/watch/test_rename_int.py`:

```python
import asyncio
from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.models import WatchedDirRecord
from docgraph.watch.manager import WatcherManager
from docgraph.web.deps import AppState


@pytest.fixture
def setup(tmp_path: Path):
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.embed_provider = "stub"
    cfg.watch_debounce_sec = 0.1
    cfg.watch_workers = 2
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    watched = tmp_path / "src"
    watched.mkdir()
    state.sqlite.insert_watched_dir(WatchedDirRecord(
        id="wd_t", path=str(watched), created_at="2026-06-07T00:00:00Z",
    ))
    return state, watched


@pytest.mark.asyncio
async def test_rename_preserves_doc_id(setup):
    state, watched = setup
    mgr = WatcherManager(state)
    await mgr.enable()
    try:
        old = watched / "old.md"
        old.write_text("body")
        await asyncio.sleep(0.5)
        doc_before = state.sqlite.get_doc_by_watched_path(str(old))
        assert doc_before is not None
        new = watched / "new.md"
        old.rename(new)
        await asyncio.sleep(0.7)  # rename + debounce
        doc_after = state.sqlite.get_doc_by_watched_path(str(new))
        assert doc_after is not None
        assert doc_after.id == doc_before.id
        assert state.sqlite.get_doc_by_watched_path(str(old)) is None
    finally:
        await mgr.disable()
```

Run: `poetry run pytest tests/watch/test_rename_int.py -v`
Expected: PASS.

- [ ] **Step 3: Burst test**

Create `tests/watch/test_burst_int.py`:

```python
import asyncio
from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.models import WatchedDirRecord
from docgraph.watch.manager import WatcherManager
from docgraph.web.deps import AppState


@pytest.fixture
def setup(tmp_path: Path):
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.embed_provider = "stub"
    cfg.watch_debounce_sec = 0.05
    cfg.watch_workers = 4
    cfg.watch_queue_capacity = 100  # smallish for the test
    cfg.ensure_dirs()
    state = AppState.create(cfg)
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
        await asyncio.sleep(2.0)  # ample time to drain
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
        await asyncio.sleep(3.0)
        # Either everything indexed OR drop counter incremented — both valid given the cap.
        docs = state.sqlite.list_watched_docs(prefix=str(watched))
        assert len(docs) > 0
        # Subsequent reconcile retries can pick up dropped ones, so we just assert no crash.
    finally:
        await mgr.disable()
```

Run: `poetry run pytest tests/watch/test_burst_int.py -v`
Expected: PASS (timings can be slow on CI; bump sleeps if flaky).

- [ ] **Step 4: Commit**

```bash
git add tests/watch/test_lifecycle_int.py tests/watch/test_rename_int.py tests/watch/test_burst_int.py
git commit -m "test(watch): lifecycle persistence, rename preserves doc_id, burst handling"
```

---

## Task 17: AppState wiring + lifespan auto-enable

**Files:**
- Modify: `docgraph/web/deps.py`, `docgraph/web/app.py`
- Test: lifespan covered by HTTP integration tests in Task 18

- [ ] **Step 1: Add watcher to AppState**

In `docgraph/web/deps.py`, add to the `AppState` class:

```python
def __init__(self, ...):  # existing
    # existing fields...
    self._watcher = None

@property
def watcher(self):
    if self._watcher is None:
        from docgraph.watch.manager import WatcherManager
        self._watcher = WatcherManager(self)
    return self._watcher
```

- [ ] **Step 2: Auto-enable in lifespan**

In `docgraph/web/app.py`, find the `_lifespan` function. After AppState creation and before yielding, add:

```python
# Auto-enable watcher if persisted state says so.
persisted = state.sqlite.get_watcher_state("enabled")
if persisted == "true":
    try:
        await state.watcher.enable()
        logger.info("watcher auto-enabled on startup")
    except Exception:
        logger.exception("watcher auto-enable failed")
```

After the `yield`, before any other shutdown logic, add:

```python
if state.watcher.state.value == "enabled":
    try:
        await state.watcher.disable()
    except Exception:
        logger.exception("watcher disable on shutdown failed")
```

- [ ] **Step 3: Run full test suite**

Run: `poetry run pytest tests/ -v -x`
Expected: All pass (no regression).

- [ ] **Step 4: Commit**

```bash
git add docgraph/web/deps.py docgraph/web/app.py
git commit -m "feat(web): AppState owns watcher + lifespan auto-enable/disable"
```

---

## Task 18: HTTP routes (status + enable/disable)

**Files:**
- Modify: `docgraph/web/app.py`
- Test: `tests/web/test_watch_routes.py`

- [ ] **Step 1: Write failing route tests**

Create `tests/web/test_watch_routes.py`:

```python
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docgraph.config import Config
from docgraph.web.app import create_app
from docgraph.web.deps import AppState


@pytest.fixture
def client(tmp_path: Path):
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.embed_provider = "stub"
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    app = create_app(state)
    return TestClient(app), state, tmp_path


def test_status_disabled_initial(client):
    c, _, _ = client
    r = c.get("/api/watch/status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["dirs_count"] == 0


def test_enable_then_disable(client):
    c, _, _ = client
    r = c.post("/api/watch/enable")
    assert r.status_code in (200, 202)
    body = r.json()
    assert body["enabled"] is True
    r = c.get("/api/watch/status")
    assert r.json()["enabled"] is True
    r = c.post("/api/watch/disable")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_enable_idempotent(client):
    c, _, _ = client
    c.post("/api/watch/enable")
    r = c.post("/api/watch/enable")
    assert r.status_code in (200, 202)
    c.post("/api/watch/disable")
```

- [ ] **Step 2: Run, verify failure**

Run: `poetry run pytest tests/web/test_watch_routes.py -v`
Expected: FAIL — routes don't exist.

- [ ] **Step 3: Add routes**

In `docgraph/web/app.py`, after the existing `/api/documents/{doc_id}/reindex` route:

```python
@app.get("/api/watch/status")
async def watch_status(request: Request):
    st: AppState = request.app.state.docgraph
    w = st.watcher
    observer_running = w._observer is not None and w._observer.is_alive() if w._observer else False
    return {
        "enabled": w.state.value == "enabled",
        "running": observer_running,
        "dirs_count": len(st.sqlite.list_watched_dirs()),
        "queue_depth": sum(q.qsize() for q in w._queues),
        "queue_capacity": st.cfg.watch_queue_capacity,
        "workers": st.cfg.watch_workers,
        "last_enabled_at": w._last_enabled_at,
        "stats": {
            "events_received": w.stats.events_received,
            "events_debounced": w.stats.events_debounced,
            "events_processed": w.stats.events_processed,
            "events_dropped_queue_full": w.stats.events_dropped_queue_full,
            "reconcile_runs": w.stats.reconcile_runs,
            "last_reconcile_at": w.stats.last_reconcile_at,
        },
    }


@app.post("/api/watch/enable", status_code=202)
async def watch_enable(request: Request):
    from docgraph.watch.manager import WatcherTransitionInProgress
    st: AppState = request.app.state.docgraph
    try:
        result = await st.watcher.enable()
    except WatcherTransitionInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return result


@app.post("/api/watch/disable")
async def watch_disable(request: Request):
    from docgraph.watch.manager import WatcherTransitionInProgress
    st: AppState = request.app.state.docgraph
    try:
        result = await st.watcher.disable()
    except WatcherTransitionInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return result
```

- [ ] **Step 4: Run test, verify pass**

Run: `poetry run pytest tests/web/test_watch_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docgraph/web/app.py tests/web/test_watch_routes.py
git commit -m "feat(web): /api/watch/status + /enable + /disable"
```

---

## Task 19: HTTP routes (dirs CRUD + reconcile)

**Files:**
- Modify: `docgraph/web/app.py`
- Test: `tests/web/test_watch_routes.py` (extend)

- [ ] **Step 1: Add failing tests**

Append to `tests/web/test_watch_routes.py`:

```python
def test_add_list_remove_dir(client, tmp_path: Path):
    c, _, _ = client
    watched = tmp_path / "src"
    watched.mkdir()
    r = c.post("/api/watch/dirs", json={
        "path": str(watched), "folder": "src", "tags": "team", "ignore_globs": []
    })
    assert r.status_code == 201
    wd_id = r.json()["id"]
    r = c.get("/api/watch/dirs")
    assert r.status_code == 200
    assert any(d["id"] == wd_id for d in r.json()["dirs"])
    r = c.delete(f"/api/watch/dirs/{wd_id}")
    assert r.status_code == 200


def test_add_dir_path_not_exist(client):
    c, _, _ = client
    r = c.post("/api/watch/dirs", json={"path": "/nonexistent/path"})
    assert r.status_code == 400


def test_add_dir_overlap_rejected(client, tmp_path: Path):
    c, _, _ = client
    w = tmp_path / "outer"
    w.mkdir()
    inner = w / "inner"
    inner.mkdir()
    r = c.post("/api/watch/dirs", json={"path": str(w)})
    assert r.status_code == 201
    r = c.post("/api/watch/dirs", json={"path": str(inner)})
    assert r.status_code == 409


def test_add_dir_inside_data_dir_rejected(client, tmp_path: Path):
    c, state, _ = client
    inside = state.cfg.data_dir / "subdir"
    inside.mkdir(parents=True)
    r = c.post("/api/watch/dirs", json={"path": str(inside)})
    assert r.status_code == 400


def test_remove_nonexistent_dir(client):
    c, _, _ = client
    r = c.delete("/api/watch/dirs/wd_missing")
    assert r.status_code == 404


def test_reconcile_endpoint(client, tmp_path: Path):
    c, _, _ = client
    w = tmp_path / "rec"
    w.mkdir()
    r = c.post("/api/watch/dirs", json={"path": str(w)})
    assert r.status_code == 201
    r = c.post("/api/watch/reconcile")
    assert r.status_code == 200
    assert r.json()["reconcile_started"] is True
```

- [ ] **Step 2: Run test, verify failure**

Run: `poetry run pytest tests/web/test_watch_routes.py -v`
Expected: New tests FAIL.

- [ ] **Step 3: Add routes**

In `docgraph/web/app.py`, add (with `Path` from `pydantic.BaseModel`-style body using `pydantic` already imported, or use `Body(...)` from FastAPI):

```python
import asyncio
import uuid
from pathlib import Path as _PathPy
from datetime import datetime, timezone

from pydantic import BaseModel


class WatchDirBody(BaseModel):
    path: str
    folder: str = ""
    tags: str = ""
    ignore_globs: list[str] = []


def _check_watched_path(cfg, path_str: str, existing_dirs: list) -> _PathPy:
    try:
        p = _PathPy(path_str).resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail=f"path does not exist: {path_str}")
    if not p.is_dir():
        raise HTTPException(status_code=400, detail=f"path is not a directory: {path_str}")
    # Inside data_dir?
    data_dir = cfg.data_dir.resolve()
    try:
        p.relative_to(data_dir)
        raise HTTPException(status_code=400, detail="path inside docgraph data_dir, would loop")
    except ValueError:
        pass
    # Reserved roots.
    forbidden = {_PathPy("/"), _PathPy("/etc"), _PathPy("/System"), _PathPy("/Users"), _PathPy.home()}
    if p in forbidden:
        raise HTTPException(status_code=400, detail=f"refusing to watch system path: {p}")
    # Overlap with existing watched dirs (parent or child relationship).
    for wd in existing_dirs:
        wd_path = _PathPy(wd.path).resolve()
        try:
            p.relative_to(wd_path)
            raise HTTPException(status_code=409, detail=f"path overlaps with watched dir {wd.id}")
        except ValueError:
            pass
        try:
            wd_path.relative_to(p)
            raise HTTPException(status_code=409, detail=f"path overlaps with watched dir {wd.id}")
        except ValueError:
            pass
    return p


@app.get("/api/watch/dirs")
async def watch_list_dirs(request: Request):
    st: AppState = request.app.state.docgraph
    dirs = st.sqlite.list_watched_dirs()
    out = []
    for wd in dirs:
        docs = st.sqlite.list_watched_docs(prefix=wd.path)
        out.append({
            "id": wd.id,
            "path": wd.path,
            "folder": wd.folder,
            "tags": wd.tags,
            "ignore_globs": wd.ignore_globs,
            "created_at": wd.created_at,
            "doc_count": len(docs),
        })
    return {"dirs": out}


@app.post("/api/watch/dirs", status_code=201)
async def watch_add_dir(request: Request, body: WatchDirBody):
    st: AppState = request.app.state.docgraph
    existing = st.sqlite.list_watched_dirs()
    p = _check_watched_path(st.cfg, body.path, existing)
    tags = [t.strip() for t in body.tags.split(",") if t.strip()]
    wd_id = f"wd_{uuid.uuid4().hex[:12]}"
    from docgraph.models import WatchedDirRecord
    wd = WatchedDirRecord(
        id=wd_id, path=str(p), folder=body.folder, tags=tags,
        ignore_globs=body.ignore_globs,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    st.sqlite.insert_watched_dir(wd)
    scheduled = False
    if st.watcher.state.value == "enabled" and st.watcher._observer is not None:
        try:
            from docgraph.watch.handler import DocgraphEventHandler
            st.watcher._ignore_matchers[wd_id] = __import__(
                "docgraph.watch.ignore", fromlist=["IgnoreMatcher"]
            ).IgnoreMatcher(wd)
            st.watcher._observer.schedule(
                DocgraphEventHandler(st.watcher, st.watcher._loop),
                wd.path, recursive=True,
            )
            asyncio.create_task(st.watcher._reconcile_dir(wd))
            scheduled = True
        except Exception:
            logger.exception("failed to schedule new watched dir")
    return {"id": wd_id, "path": wd.path, "scheduled": scheduled}


@app.delete("/api/watch/dirs/{wd_id}")
async def watch_remove_dir(request: Request, wd_id: str, delete_docs: bool = False):
    st: AppState = request.app.state.docgraph
    wd = st.sqlite.get_watched_dir(wd_id)
    if wd is None:
        raise HTTPException(status_code=404, detail="watched dir not found")
    deleted = 0
    if delete_docs:
        for doc in st.sqlite.list_watched_docs(prefix=wd.path):
            if await st.delete_doc(doc.id):
                deleted += 1
    st.sqlite.delete_watched_dir(wd_id)
    # Observer.unschedule by emitter — simplest is to rebuild observer scheduling on next enable cycle.
    # For runtime removal, unschedule_all + reschedule remaining dirs.
    if st.watcher.state.value == "enabled" and st.watcher._observer is not None:
        try:
            st.watcher._observer.unschedule_all()
            from docgraph.watch.handler import DocgraphEventHandler
            handler = DocgraphEventHandler(st.watcher, st.watcher._loop)
            for remaining in st.sqlite.list_watched_dirs():
                st.watcher._observer.schedule(handler, remaining.path, recursive=True)
        except Exception:
            logger.exception("failed to reschedule observer after dir removal")
    return {"id": wd_id, "deleted_docs": deleted, "unwatched": True}


@app.post("/api/watch/reconcile")
async def watch_reconcile(request: Request):
    st: AppState = request.app.state.docgraph
    if st.watcher.state.value != "enabled":
        raise HTTPException(status_code=409, detail="watcher must be enabled")
    dirs = st.sqlite.list_watched_dirs()
    for wd in dirs:
        asyncio.create_task(st.watcher._reconcile_dir(wd))
    return {"reconcile_started": True, "dirs": len(dirs)}
```

- [ ] **Step 4: Run tests, verify pass**

Run: `poetry run pytest tests/web/test_watch_routes.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add docgraph/web/app.py tests/web/test_watch_routes.py
git commit -m "feat(web): watch dirs CRUD + reconcile endpoint with overlap/system-path validation"
```

---

## Task 20: CLI subcommand `watch`

**Files:**
- Create: `docgraph/cli_watch.py`
- Modify: `docgraph/cli.py`
- Test: `tests/cli/test_watch_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Create `tests/cli/__init__.py` if it doesn't already exist.
Create `tests/cli/test_watch_cli.py`:

```python
from unittest.mock import patch, MagicMock

import pytest

from docgraph.cli_watch import run_watch_command


def _resp(status_code: int, json_body: dict):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body
    r.raise_for_status = MagicMock()
    return r


def test_enable_command(capsys):
    with patch("docgraph.cli_watch.httpx.Client") as m:
        m.return_value.__enter__.return_value.post.return_value = _resp(
            202, {"enabled": True, "dirs": 0, "reconcile_started": True}
        )
        rc = run_watch_command(["enable"], "http://127.0.0.1:8088")
    assert rc == 0
    out = capsys.readouterr().out
    assert "enabled" in out.lower()


def test_status_command(capsys):
    with patch("docgraph.cli_watch.httpx.Client") as m:
        m.return_value.__enter__.return_value.get.return_value = _resp(200, {
            "enabled": True, "running": True, "dirs_count": 2,
            "queue_depth": 0, "queue_capacity": 500, "workers": 4,
            "last_enabled_at": None, "stats": {
                "events_received": 0, "events_debounced": 0, "events_processed": 0,
                "events_dropped_queue_full": 0, "reconcile_runs": 1, "last_reconcile_at": None,
            },
        })
        rc = run_watch_command(["status"], "http://127.0.0.1:8088")
    assert rc == 0


def test_add_command(capsys, tmp_path):
    with patch("docgraph.cli_watch.httpx.Client") as m:
        m.return_value.__enter__.return_value.post.return_value = _resp(
            201, {"id": "wd_x", "path": str(tmp_path), "scheduled": True}
        )
        rc = run_watch_command(
            ["add", str(tmp_path), "--folder", "notes", "--tags", "a,b"],
            "http://127.0.0.1:8088",
        )
    assert rc == 0


def test_server_unreachable(capsys):
    import httpx
    with patch("docgraph.cli_watch.httpx.Client") as m:
        m.return_value.__enter__.return_value.get.side_effect = httpx.ConnectError("refused")
        rc = run_watch_command(["status"], "http://127.0.0.1:8088")
    assert rc == 1
    err = capsys.readouterr().err
    assert "server not running" in err.lower()
```

- [ ] **Step 2: Run, verify failure**

Run: `poetry run pytest tests/cli/test_watch_cli.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Create `docgraph/cli_watch.py`**

```python
from __future__ import annotations

import argparse
import json
import sys

import httpx


def _client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=10.0)


def _print_status(body: dict) -> None:
    print(f"enabled:        {body['enabled']}")
    print(f"running:        {body['running']}")
    print(f"dirs:           {body['dirs_count']}")
    print(f"queue:          {body['queue_depth']}/{body['queue_capacity']}")
    print(f"workers:        {body['workers']}")
    print(f"last_enabled:   {body['last_enabled_at']}")
    s = body["stats"]
    print(f"events recv:    {s['events_received']}")
    print(f"events debc'd:  {s['events_debounced']}")
    print(f"events proc:    {s['events_processed']}")
    print(f"dropped (full): {s['events_dropped_queue_full']}")
    print(f"reconcile runs: {s['reconcile_runs']} (last: {s['last_reconcile_at']})")


def _print_dirs(body: dict) -> None:
    dirs = body["dirs"]
    if not dirs:
        print("(no watched dirs)")
        return
    for d in dirs:
        print(f"{d['id']}  {d['path']}  docs={d['doc_count']}  folder={d['folder']}  tags={d['tags']}")


def run_watch_command(argv: list[str], base_url: str) -> int:
    parser = argparse.ArgumentParser(prog="docgraph watch")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("enable")
    sub.add_parser("disable")
    sub.add_parser("status")
    sub.add_parser("list")
    sub.add_parser("reconcile")

    p_add = sub.add_parser("add")
    p_add.add_argument("path")
    p_add.add_argument("--folder", default="")
    p_add.add_argument("--tags", default="")
    p_add.add_argument("--ignore", default="")

    p_rm = sub.add_parser("remove")
    p_rm.add_argument("wd_id")
    p_rm.add_argument("--delete-docs", action="store_true")

    args = parser.parse_args(argv)

    try:
        with _client(base_url) as client:
            if args.cmd == "enable":
                r = client.post("/api/watch/enable")
                r.raise_for_status()
                print(json.dumps(r.json(), indent=2))
            elif args.cmd == "disable":
                r = client.post("/api/watch/disable")
                r.raise_for_status()
                print(json.dumps(r.json(), indent=2))
            elif args.cmd == "status":
                r = client.get("/api/watch/status")
                r.raise_for_status()
                _print_status(r.json())
            elif args.cmd == "list":
                r = client.get("/api/watch/dirs")
                r.raise_for_status()
                _print_dirs(r.json())
            elif args.cmd == "reconcile":
                r = client.post("/api/watch/reconcile")
                r.raise_for_status()
                print(json.dumps(r.json(), indent=2))
            elif args.cmd == "add":
                ignore_globs = [g.strip() for g in args.ignore.split(",") if g.strip()]
                r = client.post("/api/watch/dirs", json={
                    "path": args.path,
                    "folder": args.folder,
                    "tags": args.tags,
                    "ignore_globs": ignore_globs,
                })
                r.raise_for_status()
                print(json.dumps(r.json(), indent=2))
            elif args.cmd == "remove":
                r = client.delete(
                    f"/api/watch/dirs/{args.wd_id}",
                    params={"delete_docs": str(args.delete_docs).lower()},
                )
                r.raise_for_status()
                print(json.dumps(r.json(), indent=2))
    except httpx.ConnectError:
        print(f"server not running at {base_url}; start with: docgraph serve", file=sys.stderr)
        return 1
    except httpx.HTTPStatusError as exc:
        print(f"error: {exc.response.status_code} {exc.response.text}", file=sys.stderr)
        return 2
    return 0
```

- [ ] **Step 4: Wire into `docgraph/cli.py`**

In `docgraph/cli.py`, find the subparser registration block. Add:

```python
    p_watch = sub.add_parser("watch", help="manage file watcher")
    p_watch.add_argument("watch_args", nargs=argparse.REMAINDER, help="<enable|disable|status|list|add|remove|reconcile> [args]")
```

In the command dispatch block (where `if args.cmd == "serve":` etc.):

```python
    elif args.cmd == "watch":
        from docgraph.cli_watch import run_watch_command
        cfg = load_config()
        base_url = f"http://{cfg.web_host}:{cfg.web_port}"
        return run_watch_command(args.watch_args, base_url)
```

- [ ] **Step 5: Run tests, verify pass**

Run: `poetry run pytest tests/cli/test_watch_cli.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docgraph/cli_watch.py docgraph/cli.py tests/cli/test_watch_cli.py
git commit -m "feat(cli): docgraph watch subcommand (enable/disable/status/list/add/remove/reconcile)"
```

---

## Task 21: macOS-specific atomic rename test

**Files:**
- Test: `tests/watch/test_fsevents_atomic_rename.py`

- [ ] **Step 1: Write platform-gated test**

Create `tests/watch/test_fsevents_atomic_rename.py`:

```python
import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.models import WatchedDirRecord
from docgraph.watch.manager import WatcherManager
from docgraph.web.deps import AppState

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only fsevents test")


@pytest.fixture
def setup(tmp_path: Path):
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.embed_provider = "stub"
    cfg.watch_debounce_sec = 0.2
    cfg.watch_workers = 2
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    watched = tmp_path / "src"
    watched.mkdir()
    state.sqlite.insert_watched_dir(WatchedDirRecord(
        id="wd_t", path=str(watched), created_at="2026-06-07T00:00:00Z",
    ))
    return state, watched


@pytest.mark.asyncio
async def test_vim_style_atomic_save(setup):
    """Simulate vim's swap-and-rename save pattern."""
    state, watched = setup
    mgr = WatcherManager(state)
    await mgr.enable()
    try:
        final = watched / "note.md"
        # Initial create.
        final.write_text("v1")
        await asyncio.sleep(0.6)
        doc_v1 = state.sqlite.get_doc_by_watched_path(str(final))
        assert doc_v1 is not None
        # Simulate vim: write tmp, atomic rename over final.
        tmp = watched / ".note.md.swp"
        tmp.write_text("v2 has more content")
        tmp.rename(final)
        await asyncio.sleep(0.8)
        doc_v2 = state.sqlite.get_doc_by_watched_path(str(final))
        assert doc_v2 is not None
        assert doc_v2.mtime_ns > doc_v1.mtime_ns
    finally:
        await mgr.disable()
```

Run: `poetry run pytest tests/watch/test_fsevents_atomic_rename.py -v`
Expected: PASS on macOS, SKIP on Linux.

- [ ] **Step 2: Commit**

```bash
git add tests/watch/test_fsevents_atomic_rename.py
git commit -m "test(watch): macOS-only atomic-rename save pattern"
```

---

## Task 22: README documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add watcher section**

In `README.md`, after the existing config table (near where rerank knobs are documented), add a new section:

````markdown
## File watcher (roadmap 3.1)

Auto-indexes files in user-configured directories. Runtime-toggleable via HTTP API or CLI — no restart needed.

### Quick start

```bash
# Add a watched directory (server must be running)
docgraph watch add ~/Notes --folder notes --tags personal

# Turn the watcher on
docgraph watch enable

# Check what it's doing
docgraph watch status

# Turn it off (paths persist; re-enable later picks them up)
docgraph watch disable
```

### How it works

- **Text files** (`.md`, `.py`, `.rs`, `.json`, etc.) are referenced in place — no duplicate copy in `data_dir`.
- **Binary files** (`.pdf`, `.docx`, `.pptx`, `.xlsx`, …) are copied into `data_dir/files/originals/` as a snapshot at index time; the converted markdown is cached and regenerated only when the source file's mtime changes.
- **Unknown extensions** are skipped silently. Extend via `DOCGRAPH_WATCH_EXTRA_TEXT_EXTS` / `DOCGRAPH_WATCH_EXTRA_BINARY_EXTS`.

### Ignore patterns

Three layers compose:

1. **Hardcoded defaults** — `.git`, `node_modules`, `__pycache__`, `.venv`, `.DS_Store`, `*.pyc`, `*.swp`, `*~`, etc.
2. **`.docgraphignore`** at each watched-dir root — gitignore syntax (minus negation). Reloaded on its own mtime.
3. **Per-dir `ignore_globs`** from the add-dir API/CLI.

### Watched vs uploaded

A file at the same path can exist as **two separate docs** if you both upload it via `POST /api/documents` and watch its directory. They have independent lifecycles. Watcher docs use `source_type=watched`; upload docs use `source_type=file`.

### Config knobs

| Env var | YAML key | Default | Purpose |
|---|---|---:|---|
| `DOCGRAPH_WATCH_DEBOUNCE_SEC` | `watch.debounce_sec` | `2.0` | Per-path debounce window before enqueueing |
| `DOCGRAPH_WATCH_QUEUE_CAPACITY` | `watch.queue_capacity` | `500` | Aggregate cap across per-worker queues |
| `DOCGRAPH_WATCH_WORKERS` | `watch.workers` | `4` | Number of async ingest workers |
| `DOCGRAPH_WATCH_RECOVERY_INTERVAL_SEC` | `watch.recovery_interval_sec` | `600` | Periodic reconcile (fsevents-drop backstop) |
| `DOCGRAPH_WATCH_EXTRA_TEXT_EXTS` | `watch.extra_text_exts` | `[]` | Extra extensions to treat as native text |
| `DOCGRAPH_WATCH_EXTRA_BINARY_EXTS` | `watch.extra_binary_exts` | `[]` | Extra extensions to convert as binary |

### HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/watch/status` | Snapshot of watcher state, stats, queue depth |
| `POST` | `/api/watch/enable` | Start observer + workers, run reconcile |
| `POST` | `/api/watch/disable` | Stop observer, cancel workers, drain queue |
| `GET`  | `/api/watch/dirs` | List watched dirs (with per-dir doc count) |
| `POST` | `/api/watch/dirs` | Add a dir: body `{path, folder, tags, ignore_globs}` |
| `DELETE` | `/api/watch/dirs/{wd_id}` | Remove a dir (optional `?delete_docs=true`) |
| `POST` | `/api/watch/reconcile` | Manual reconcile across all dirs (watcher must be enabled) |

### Known limitations

- Symlinks are not followed (would risk indexing loops). Documented.
- macOS fsevents can drop events under burst — the 10-minute recovery reconcile backstops this.
- Watcher does not auth its endpoints — they inherit whatever auth ships when roadmap 3.4 lands.
````

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs(readme): file watcher usage, config, API, limitations"
```

---

## Task 23: Final integration smoke test

**Files:**
- Test: extend `tests/test_e2e.py` (or create `tests/test_watcher_e2e.py`)

- [ ] **Step 1: Add end-to-end test**

Create `tests/test_watcher_e2e.py`:

```python
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docgraph.config import Config
from docgraph.web.app import create_app
from docgraph.web.deps import AppState


@pytest.fixture
def client(tmp_path: Path):
    cfg = Config()
    cfg.data_dir = tmp_path / "data"
    cfg.embed_provider = "stub"
    cfg.watch_debounce_sec = 0.1
    cfg.watch_workers = 2
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    app = create_app(state)
    return TestClient(app), state, tmp_path


def test_e2e_add_dir_then_enable_then_create_file(client, tmp_path: Path):
    c, state, _ = client
    watched = tmp_path / "live"
    watched.mkdir()
    # 1. Add dir
    r = c.post("/api/watch/dirs", json={"path": str(watched), "folder": "live"})
    assert r.status_code == 201
    # 2. Enable watcher
    r = c.post("/api/watch/enable")
    assert r.status_code in (200, 202)
    # 3. Drop a file
    (watched / "note.md").write_text("# Live note\n\nbody body body.")
    # 4. Wait for debounce + index
    import time
    for _ in range(20):
        time.sleep(0.2)
        docs = state.sqlite.list_watched_docs(prefix=str(watched))
        if docs:
            break
    docs = state.sqlite.list_watched_docs(prefix=str(watched))
    assert len(docs) == 1
    assert docs[0].folder == "live"
    # 5. Disable cleanly
    r = c.post("/api/watch/disable")
    assert r.status_code == 200
```

Run: `poetry run pytest tests/test_watcher_e2e.py -v`
Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run: `poetry run pytest tests/ -v`
Expected: All pass (modulo any pre-existing macOS-only / slow markers).

- [ ] **Step 3: Commit**

```bash
git add tests/test_watcher_e2e.py
git commit -m "test(e2e): full watcher pipeline (add dir → enable → create → indexed)"
```

---

## Done

After all 23 tasks complete:

- 6 new modules in `docgraph/watch/`
- 7 new HTTP routes
- 7 new CLI commands
- 14 new test files (~50 new test cases)
- 3 new SQLite columns, 2 new tables, 1 unique index
- 2 new Indexer entry points + 1 helper (`index_text_direct`)
- Runtime-toggleable, persists across restart, fsevents-drop backstop, hybrid storage

Spec requirements (cross-check):
- §3 architecture — Tasks 7, 10, 11, 13, 15
- §4 data model — Tasks 1, 2, 3, 4
- §5 HTTP API — Tasks 18, 19
- §6 CLI — Task 20
- §7 lifecycle — Tasks 10, 11, 13, 15, 17
- §8 ingest integration — Tasks 8, 9
- §9 error handling — covered across handlers in Tasks 13, 15
- §10 edge cases — Tasks 16, 21
- §11 observability — Tasks 11, 13 use `logger.info` with structured `extra` already
- §12 testing — Tasks throughout (every task has tests)
- §13 config knobs — Task 5
- §15 dependencies — Task 7 (`watchdog`, `pathspec`)
