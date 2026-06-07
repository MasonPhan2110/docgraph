# Real-time File Watcher — Design

**Status:** Brainstorming complete — pending user review, then implementation plan
**Date:** 2026-06-07
**Author:** brainstorm session, DocGraph maintainer
**Scope:** Local single-user DocGraph (matches existing deployment model). Roadmap item 3.1 from `docs/superpowers/2026-06-07-roadmap-post-hybrid.md`.

---

## 1. Goal

Auto-index local files when they change, without the user manually uploading or calling `/reindex`. Watcher is a generic subsystem covering three use cases: personal notes folders (Obsidian-style markdown), code repos (handle git-checkout bursts), and shared document repositories (PDFs, etc.). Toggle-able on/off and watched paths editable at runtime — no restart required.

## 2. Non-goals

- Remote / network filesystems (S3, SMB, NFS). Local FS only.
- Distributed multi-node watcher. Single process.
- Auto-tagging from content via LLM. Tags inherited from `WatchedDir` only.
- Encrypted-at-rest watched files (separate concern, roadmap 7.3).
- Auth on watch endpoints — inherits whatever auth ships in roadmap 3.4.
- Webhook callbacks on ingest complete — UI polls status.
- Re-architecting existing upload / URL ingest flows.

## 3. Architecture overview

```
┌──────────────────────────────────────────────────────────────────┐
│ FastAPI app (existing)                                           │
│                                                                  │
│  Existing routes:                  New routes:                   │
│    POST /api/documents               POST /api/watch/enable      │
│    POST /api/documents/{id}/reindex  POST /api/watch/disable     │
│    DELETE /api/documents/{id}        GET  /api/watch/status      │
│                                       POST /api/watch/dirs       │
│                                       DELETE /api/watch/dirs/{id}│
│                                       GET  /api/watch/dirs       │
│                                       POST /api/watch/reconcile  │
└────────────────────────────┬─────────────────────────────────────┘
                             │
              ┌──────────────▼─────────────────┐
              │ WatcherManager (new)           │  ← AppState owns
              │  state: DISABLED|ENABLING|     │
              │         ENABLED|DISABLING      │
              │  observer: watchdog.Observer   │
              │  queues: 4× asyncio.Queue      │
              │   (per-worker, agg cap 500)    │
              │  workers: 4 asyncio.Task       │
              │  debounce: dict[path, Handle]  │
              └──────┬─────────────────────────┘
                     │ enqueue (debounced + partitioned)
                     ▼
              ┌──────────────────────────────┐
              │ Ingest workers (asyncio)     │
              │  UPSERT / DELETE / RENAME    │
              │  → claim_for_reindex         │
              │  → dispatch text vs binary   │
              └──────┬───────────────────────┘
                     ▼
              ┌─────────────────────────┐
              │ Indexer (existing +     │
              │ new index_watched_*)    │
              └─────────────────────────┘
                     ▼
        ┌────────────┬──────────┬──────────┐
        │  SQLite    │  Chroma  │  FTS5    │ (existing)
        └────────────┴──────────┴──────────┘
```

**Design split into two independent layers:**
- **Layer 1 — Watcher engine:** `watchdog` Python library (cross-platform: fsevents on macOS, inotify on Linux). Recovery scan every 10 min to backstop fsevents drops.
- **Layer 2 — Ingest integration:** new `SourceType.WATCHED` plus `materialize` flag. Hybrid storage: text files referenced in-place, binary files copied into `originals_dir` as before.

**New files:**
- `docgraph/watch/__init__.py`
- `docgraph/watch/manager.py` — `WatcherManager` state machine + queue + worker pool
- `docgraph/watch/handler.py` — `watchdog.FileSystemEventHandler` thread-to-asyncio bridge
- `docgraph/watch/ignore.py` — hardcoded defaults + `.docgraphignore` (pathspec)
- `docgraph/watch/reconcile.py` — full delta scan vs DB
- `docgraph/cli_watch.py` — CLI subcommand wrapper (thin httpx client)

**Modified files:**
- `docgraph/models.py` — `SourceType.WATCHED`, `WatchedDirRecord`
- `docgraph/store/sqlite.py` — new tables/columns + CRUD
- `docgraph/web/app.py` — 7 new routes
- `docgraph/web/deps.py` — `AppState` owns `WatcherManager`; factor delete logic out of the `DELETE /api/documents/{id}` route into `AppState.delete_doc()` so the watcher can call the same code path
- `docgraph/cli.py` — register `watch` subcommand
- `docgraph/ingest/indexer.py` — `index_watched_new`, `reindex_watched`, `index_text_direct`
- `docgraph/ingest/lang_dispatch.py` — `detect_materialize` + extension allowlists
- `pyproject.toml` — `watchdog ^4.0`, `pathspec ^0.12`

## 4. Data model

### 4.1 `SourceType` enum

```python
class SourceType(str, Enum):
    FILE = "file"        # existing — uploaded, owned copy in originals_dir
    URL = "url"          # existing — crawled web page
    WATCHED = "watched"  # NEW — tracked file in a watched dir
```

### 4.2 `documents` — additive columns

```sql
ALTER TABLE documents ADD COLUMN watched_path TEXT;
ALTER TABLE documents ADD COLUMN materialize INTEGER;
ALTER TABLE documents ADD COLUMN mtime_ns INTEGER;

CREATE UNIQUE INDEX idx_documents_watched_path
  ON documents(watched_path) WHERE watched_path IS NOT NULL;
```

**Invariants:**
- `source_type=WATCHED` ⟺ `watched_path IS NOT NULL` ⟺ `materialize IS NOT NULL`.
- `source_type=FILE` or `URL` → new columns are NULL.
- Partial unique index: at most one doc per absolute path. Backward-compatible with existing rows.

### 4.3 `watched_dirs` (new table)

```sql
CREATE TABLE watched_dirs (
  id TEXT PRIMARY KEY,                    -- "wd_" + uuid hex 12
  path TEXT NOT NULL UNIQUE,              -- absolute, canonicalized (resolve(strict=True))
  folder TEXT NOT NULL DEFAULT '',        -- folder tag applied to every doc under this dir
  tags TEXT NOT NULL DEFAULT '[]',        -- JSON array, propagated to each doc
  ignore_globs TEXT NOT NULL DEFAULT '[]',-- JSON, additional globs beyond defaults + .docgraphignore
  created_at TEXT NOT NULL
);
```

### 4.4 `watcher_state` (new key-value table, singleton semantics)

```sql
CREATE TABLE watcher_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- key="enabled"          value="true"|"false"
-- key="last_enabled_at"  value=ISO-8601 UTC
```

Survives restart → server boot reads `enabled=true` and auto-starts watcher.

### 4.5 Migration

DocGraph currently has no migration framework. Extend `_ensure_schema()` in `docgraph/store/sqlite.py`:
- `CREATE TABLE IF NOT EXISTS` for `watched_dirs` and `watcher_state`.
- `ALTER TABLE documents ADD COLUMN` for each new column, wrapped in try/except (`OperationalError: duplicate column name` is benign and skipped).
- `CREATE UNIQUE INDEX IF NOT EXISTS` for `idx_documents_watched_path`.

Idempotent — runs every startup, safe on existing databases. No down-migration; additive only.

### 4.6 Hybrid storage matrix

| File type | Detection | `materialize` | `watched_path` | `original_path` | `markdown_path` |
|---|---|---|:-:|---|---|
| Native text (`.md`, `.txt`, `.py`, `.js`, `.rs`, …) | extension allowlist | `0` | `/Users/x/notes/foo.md` | NULL | NULL (chunk directly) |
| Binary needing conversion (`.pdf`, `.docx`, `.pptx`, `.xlsx`, …) | extension allowlist | `1` | `/Users/x/docs/foo.pdf` | `originals_dir/{doc_id}_foo.pdf` (snapshot) | `markdown_dir/{doc_id}.md` (regenerated on mtime change) |
| Unknown extension | fall-through | skip | — | — | — |

Detection by extension only — no content sniffing (avoids I/O on every event).

## 5. HTTP API

### 5.1 `GET /api/watch/status`

```json
{
  "enabled": true,
  "running": true,
  "dirs_count": 2,
  "queue_depth": 12,                          // sum across per-worker queues
  "queue_capacity": 500,                      // aggregate cap (per-worker = capacity / workers)
  "workers": 4,
  "last_enabled_at": "2026-06-07T14:30:12Z",
  "stats": {
    "events_received": 1547,
    "events_debounced": 1390,
    "events_processed": 152,
    "events_dropped_queue_full": 5,
    "reconcile_runs": 1,
    "last_reconcile_at": "2026-06-07T14:30:14Z"
  }
}
```

Stats are in-process counters; reset to zero on disable. `running` reflects observer thread liveness (separate from `enabled` which is persisted intent).

### 5.2 `POST /api/watch/enable`

Empty body. Effects:
1. `watcher_state.enabled = "true"` (persist).
2. Spawn reconcile task per `watched_dirs` row (background).
3. Start `watchdog.Observer` + schedule each path.
4. Start asyncio worker pool.

Response `202 Accepted`:
```json
{ "enabled": true, "reconcile_started": true, "dirs": 2 }
```

Idempotent — second call when already ENABLED returns `200 OK` with same shape, no re-spawn. Concurrent enable/disable mid-transition → `409` (see §5.7).

### 5.3 `POST /api/watch/disable`

Effects:
1. `enabled = "false"`.
2. Cancel all pending debounce TimerHandles.
3. `observer.stop(); observer.join(timeout=5.0)`.
4. Cancel worker tasks; drain queue but don't block.

Response `200 OK`:
```json
{ "enabled": false, "queue_drained": 12, "queue_dropped": 8 }
// queue_drained/queue_dropped scoped to this disable call only — not cumulative.
```

### 5.4 `GET /api/watch/dirs`

```json
{
  "dirs": [
    {
      "id": "wd_a1b2c3d4e5f6",
      "path": "/Users/nhatminhphan/Notes",
      "folder": "notes",
      "tags": ["personal"],
      "ignore_globs": ["draft/*"],
      "created_at": "2026-06-07T14:25:00Z",
      "doc_count": 47
    }
  ]
}
```

`doc_count` computed via `COUNT(*) WHERE watched_path LIKE path || '/%'` per row.

### 5.5 `POST /api/watch/dirs`

```json
// Request
{
  "path": "/Users/nhatminhphan/Notes",
  "folder": "notes",
  "tags": "personal,important",
  "ignore_globs": ["draft/*", "**/.tmp"]
}
```

**Validation:**
- `path` must exist, be a directory, absolute. Canonicalize via `Path(path).resolve(strict=True)`.
- Reject if path overlaps with an existing watched dir (parent or child) → `409`.
- Reject if path is a subdir of `cfg.data_dir` (would cause indexing loop) → `400`.
- Reject system paths: `/`, `/etc`, `/System`, `~` (raw home), `/Users` (raw) → `400`.

Response `201 Created`:
```json
{ "id": "wd_a1b2c3d4e5f6", "path": "...", "scheduled": true }
```

If `enabled=true`: immediately schedule observer + kick off reconcile for new dir.
If `enabled=false`: just insert DB row.

### 5.6 `DELETE /api/watch/dirs/{wd_id}?delete_docs=false`

Query param `delete_docs`:
- `false` (default): unwatch only. Existing docs remain in index but no longer tracked.
- `true`: also delete every doc whose `watched_path` is prefix-matched by `wd.path`.

Response `200 OK`:
```json
{ "id": "wd_a1b2c3d4e5f6", "deleted_docs": 0, "unwatched": true }
```

### 5.7 Error response matrix

| Case | Status | Body |
|---|---|---|
| Path does not exist | 400 | `{"detail": "path does not exist: ..."}` |
| Path overlaps existing watched dir | 409 | `{"detail": "path overlaps with watched dir wd_..."}` |
| Path inside `data_dir` | 400 | `{"detail": "path inside docgraph data_dir, would loop"}` |
| Reserved system path | 400 | `{"detail": "refusing to watch system path: ..."}` |
| `wd_id` not found | 404 | `{"detail": "watched dir not found"}` |
| Concurrent enable/disable | 409 | `{"detail": "watcher transition in progress"}` |

### 5.8 `POST /api/watch/reconcile`

Manual trigger to run reconcile across all watched dirs without restarting watcher. Useful when user suspects fsevents drift. Returns immediately, scan runs in background.

```json
{ "reconcile_started": true, "dirs": 2 }
```

## 6. CLI surface

Thin wrapper over HTTP (`httpx.Client`, base URL from `cfg.server_url`, default `http://127.0.0.1:8088`).

```
docgraph watch enable                     → POST /api/watch/enable
docgraph watch disable                    → POST /api/watch/disable
docgraph watch status                     → GET  /api/watch/status (pretty table)
docgraph watch list                       → GET  /api/watch/dirs
docgraph watch add <PATH> [--folder X] [--tags a,b] [--ignore "p1,p2"]
                                          → POST /api/watch/dirs
docgraph watch remove <WD_ID> [--delete-docs]
                                          → DELETE /api/watch/dirs/{id}
docgraph watch reconcile                  → POST /api/watch/reconcile
```

Failure mode: server unreachable → exit code 1 with `server not running at <url>; start with: docgraph serve`.

## 7. Watcher lifecycle

### 7.1 State machine

```
       DISABLED ──enable──► ENABLING ──ok──► ENABLED
                                              │
       DISABLED ◄──ok── DISABLING ◄──disable──┘
```

Transitional states (ENABLING / DISABLING) return `409` for further enable/disable calls. `asyncio.Lock` in `WatcherManager` enforces.

**Startup hook** (`_lifespan` in `app.py`):
- Read `watcher_state.enabled`. If `true` → auto-enable.

**Shutdown hook:**
- If ENABLED → disable cleanly, wait for queue drain or timeout 5s.

### 7.2 Thread-to-asyncio bridge

`watchdog.Observer` runs a native thread. Events arrive there. The asyncio queue is not thread-safe, so the handler uses `loop.call_soon_threadsafe`:

```python
class DocgraphEventHandler(FileSystemEventHandler):
    def __init__(self, manager, loop):
        self._manager = manager
        self._loop = loop

    def on_any_event(self, event):
        if event.is_directory:
            return
        self._loop.call_soon_threadsafe(
            self._manager._on_raw_event,
            event.event_type, event.src_path, getattr(event, "dest_path", None),
        )
```

`_on_raw_event` runs on the event loop, applies ignore filter, then debounces.

### 7.3 Ignore pipeline

Three layers, evaluated in order, short-circuit on match:

```python
def _should_ignore(path: Path, wd: WatchedDir) -> bool:
    rel = path.relative_to(wd.path)
    # 1. Hardcoded defaults
    for part in rel.parts:
        if part in HARDCODED_IGNORE_DIRS:  # .git, node_modules, __pycache__, .venv
            return True
    if path.name in HARDCODED_IGNORE_FILES:  # .DS_Store
        return True
    if any(rel.match(g) for g in HARDCODED_IGNORE_GLOBS):  # *.pyc, *.swp, *~, .#*
        return True
    # 2. .docgraphignore at wd root (parsed lazily; cache invalidated on its own mtime)
    if _docgraphignore_matches(wd, rel):
        return True
    # 3. wd.ignore_globs from DB
    if any(rel.match(g) for g in wd.ignore_globs):
        return True
    return False
```

`.docgraphignore` parsed via `pathspec` library (~300 LOC, MIT). Gitignore syntax minus negation. Cache key: `(wd.id, .docgraphignore mtime)`. Parse failure → log warning, treat as empty.

### 7.4 Debounce (per-path, 2s)

```python
def _enqueue_debounced(self, action, src_path, dest_path):
    if existing := self._debounce_tasks.get(src_path):
        existing.cancel()
        self._stats.events_debounced += 1
    handle = self._loop.call_later(
        DEBOUNCE_SECONDS,
        self._flush_debounce, action, src_path, dest_path,
    )
    self._debounce_tasks[src_path] = handle
```

Property: N events on the same path within 2s → 1 enqueue. Counter `events_debounced` tracks the N–1 coalesced.

### 7.5 Worker pool (4 workers, key-partitioned)

```python
async def _worker_loop(self, worker_id: int):
    queue = self._queues[worker_id]
    while not self._shutdown.is_set():
        try:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        try:
            await self._handle_event(event)
        except Exception:
            logger.exception("worker=%d failed on %s", worker_id, event)
        finally:
            queue.task_done()
```

**Partitioning:** `hash(path) % num_workers` selects which worker queue receives the event. Guarantees events for the same path are processed in order by a single worker (no cross-worker reorder), so delete-then-create on a rename never races against the unique index. Each per-worker queue is bounded; the aggregate cap is 500 events across all queues.

### 7.6 Upsert handler — text vs binary dispatch

```python
async def _handle_upsert(self, path: str):
    p = Path(path)
    if not p.exists():
        return
    wd = self._lookup_wd_for_path(p)
    if wd is None or self._should_ignore(p, wd):
        return
    if p.stat().st_size > self._cfg.max_file_size_mb * 1024 * 1024:
        logger.warning("watcher: file too large, skipping: %s", path)
        return
    mtime_ns = p.stat().st_mtime_ns
    materialize = _detect_materialize(p)
    if materialize is None:
        return  # unsupported extension

    existing = self._sqlite.get_doc_by_watched_path(str(p))
    if existing:
        if existing.mtime_ns == mtime_ns:
            return  # no-op
        if not self._sqlite.claim_for_reindex(existing.id):
            return  # already PROCESSING; next debounce flush retries
        await self._indexer.reindex_watched(existing.id, p, materialize, mtime_ns)
    else:
        doc_id = f"doc_{uuid.uuid4().hex[:12]}"
        await self._indexer.index_watched_new(doc_id, p, wd, materialize, mtime_ns)
```

### 7.7 Delete and rename handlers

```python
async def _handle_delete(self, path: str):
    doc = self._sqlite.get_doc_by_watched_path(path)
    if doc is None:
        return
    await self._app_state.delete_doc(doc.id)

async def _handle_rename(self, src: str, dest: str):
    doc = self._sqlite.get_doc_by_watched_path(src)
    if doc is None:
        await self._handle_upsert(dest)
        return
    new_wd = self._lookup_wd_for_path(Path(dest))
    if new_wd is None or self._should_ignore(Path(dest), new_wd):
        await self._app_state.delete_doc(doc.id)
        return
    # Update path in place — preserves doc_id, chunks, embeddings.
    self._sqlite.update_watched_path(doc.id, dest)
```

On macOS, fsevents sometimes emits `deleted(src) + created(dest)` instead of `moved`. Key partitioning ensures serial order on the same worker, so DELETE clears the unique index before UPSERT inserts.

### 7.8 Reconcile scan

Runs on enable, on add-dir-while-enabled, on manual `/api/watch/reconcile`, and every 10 minutes as fsevents-drop backstop.

```python
async def _reconcile(self, wd: WatchedDir):
    self._stats.reconcile_runs += 1
    disk: dict[str, int] = {}
    count = 0
    for p in self._walk(wd.path, wd):  # applies ignore filter
        try:
            disk[str(p)] = p.stat().st_mtime_ns
        except FileNotFoundError:
            continue
        count += 1
        if count % 100 == 0:
            await asyncio.sleep(0)  # yield event loop

    db_docs = self._sqlite.list_watched_docs(prefix=wd.path)
    db_by_path = {d.watched_path: d for d in db_docs}

    for path, mtime in disk.items():
        prev = db_by_path.get(path)
        if prev is None or prev.mtime_ns != mtime:
            self._enqueue_direct(WatchEvent("UPSERT", path, None))
    for path in db_by_path.keys() - disk.keys():
        self._enqueue_direct(WatchEvent("DELETE", path, None))
    self._stats.last_reconcile_at = _now_iso()
```

Reconcile bypasses debounce (it produces a deduped snapshot, no need to coalesce). Overflow on partitioned queue → drop with warning; next reconcile retries.

### 7.9 Shutdown sequence

1. `self._shutdown.set()`.
2. `observer.stop(); observer.join(timeout=5.0)`.
3. Cancel all debounce TimerHandles.
4. `tasks.cancel(); await asyncio.gather(*tasks, return_exceptions=True)`.
5. Log final stats.
6. State → DISABLED.

Workers receiving `CancelledError` mid-event abort. `claim_for_reindex` will be released by the indexer's status flow when the exception bubbles. Next enable's reconcile picks up any partial state.

## 8. Ingest integration

### 8.1 Two new entry points on `Indexer`

```python
async def index_watched_new(
    self, doc_id, watched_path, wd, materialize, mtime_ns,
):
    """First-time index of a file discovered in a watched dir."""

async def reindex_watched(
    self, doc_id, watched_path, materialize, mtime_ns,
):
    """Re-run ingest for a watched doc whose mtime changed."""
```

`reindex_watched` differs from existing `reindex_document` in two ways:
- Reads from `watched_path` (text branch) or refreshes the snapshot copy in `originals_dir` (binary branch).
- Updates `mtime_ns` after success; does not update `watched_path`.

### 8.2 `index_watched_new` flow

```python
async def index_watched_new(self, doc_id, watched_path, wd, materialize, mtime_ns):
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
        text = await asyncio.to_thread(watched_path.read_text, "utf-8", "ignore")
        await self.index_text_direct(doc_id, text)
    self._sqlite.update_mtime_ns(doc_id, mtime_ns)
```

### 8.3 `reindex_watched` flow

```python
async def reindex_watched(self, doc_id, watched_path, materialize, mtime_ns):
    # claim_for_reindex already called by watcher; status = PROCESSING.
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
        text = await asyncio.to_thread(watched_path.read_text, "utf-8", "ignore")
        await self.index_text_direct(doc_id, text)
    self._sqlite.update_mtime_ns(doc_id, mtime_ns)
```

### 8.4 `index_text_direct` — bypass conversion

Native-text files (`.md`, `.py`, etc.) don't need the markdown conversion step. `markitdown` may even degrade them (wrapping code in unnecessary fences). New helper:

```python
async def index_text_direct(self, doc_id: str, text: str) -> None:
    doc = self._sqlite.get_document(doc_id)
    if doc is None:
        raise ValueError(f"document not found: {doc_id}")
    self._progress(doc_id, 20, "Skipping conversion — native text (20%)")
    await self.index_markdown(doc_id, text)  # existing chunk + embed flow
```

`index_markdown` already does chunking + embedding + upsert — reused as-is.

### 8.5 Extension classification

```python
# docgraph/ingest/lang_dispatch.py  (extend existing file)

NATIVE_TEXT_EXTS = frozenset({
    ".md", ".markdown", ".txt", ".rst",
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".rs", ".go", ".java", ".kt", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".rb", ".php", ".lua", ".pl", ".sh", ".bash", ".zsh", ".fish",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".html", ".css", ".scss", ".sass", ".less",
    ".sql", ".graphql", ".proto",
    ".tex", ".csv", ".tsv",
})

BINARY_CONVERT_EXTS = frozenset({
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
    ".odt", ".odp", ".ods", ".epub",
})

def detect_materialize(path: Path) -> bool | None:
    ext = path.suffix.lower()
    if ext in NATIVE_TEXT_EXTS:
        return False
    if ext in BINARY_CONVERT_EXTS:
        return True
    return None
```

Config knobs to extend without code change:
- `DOCGRAPH_WATCH_EXTRA_TEXT_EXTS=".tf,.hcl,.dockerfile"`
- `DOCGRAPH_WATCH_EXTRA_BINARY_EXTS="..."`

Both unioned with the frozensets at startup.

### 8.6 Snapshot semantics for binary

When `materialize=True`, the copy in `originals_dir` is a snapshot at index time. Subsequent watched-file changes trigger a fresh snapshot copy + reconvert. Reasons:
- PDF → markdown conversion is expensive; only redo on mtime change.
- Deleting the doc cleans up `originals_dir/{doc_id}_*` safely — never touches the user's watched file.
- Decouples ingest cache invariants (`original_path` = blob for chunker) from watcher dedup invariant (`watched_path` = identity).

### 8.7 Delete semantics — watched files are user-owned

`AppState.delete_doc(doc_id)` calls `st.files.delete_doc_files(doc_id)`, which globs `originals_dir/{doc_id}_*` and unlinks. This already only touches `data_dir`. Add an explicit comment in `files.py` to prevent future regressions:

```python
def delete_doc_files(self, doc_id: str) -> None:
    for p in self._cfg.originals_dir.glob(f"{doc_id}_*"):
        p.unlink(missing_ok=True)
    # NEVER touch paths outside data_dir — watched files are user-owned.
```

### 8.8 Coexistence with `POST /api/documents` upload

A user could upload a file (`SourceType.FILE`) whose path is also being watched (`SourceType.WATCHED`). Two separate docs result — no cross-check. Rationale:
- Upload is a one-shot snapshot; watcher tracks lifecycle.
- Intentional: user may want a snapshot version independent from the live file.

Documented in README under "watched vs uploaded".

### 8.9 Concurrency with `claim_for_reindex`

Existing `claim_for_reindex` is an atomic SQLite `UPDATE … SET status=PROCESSING WHERE status IN (READY, ERROR)` returning row count. Properties:
- Watcher fires event mid-indexing → `claim` returns False → worker skips. `mtime_ns` in DB still has the old value, so next debounce flush sees a mismatch and retries.
- HTTP `/reindex` racing with watcher reindex → same mechanism, only one wins.
- Cross-FS rename (DELETE + UPSERT) → key partitioning serializes them on one worker; unique index never sees both rows.

## 9. Error handling matrix

| Failure mode | Detection | Handling | User-visible |
|---|---|---|---|
| Watched path vanishes mid-upsert | `if not p.exists()` | Early return; reconcile picks up DELETE later | Doc eventually deleted |
| Permission denied on read | `OSError` in `read_text`/`read_bytes` | Catch, log warning, doc → ERROR with detail | Status=ERROR |
| Convert failure (corrupt PDF) | Exception in `convert_file_to_markdown` | Existing flow: doc → ERROR | Status=ERROR |
| `claim_for_reindex` returns False | Worker checks return | Skip event, debug log. Reconcile/recovery scan retries | Silent (correct) |
| Observer thread dies | `observer.is_alive()` poll in status endpoint | Status exposes `running=false`. Auto-restart thread after 10s if `enabled=true` | `running:false` then `true` after recovery |
| Queue full (>500 pending across partitions) | `put_nowait` → `QueueFull` | Drop event, `events_dropped_queue_full++`, warn | Status exposes counter |
| `.docgraphignore` parse error | `pathspec.parse` raise | Catch, log warning, treat as empty rules | Log only |
| Disk full on snapshot save | `OSError` in `save_original` | Doc → ERROR | Status=ERROR |
| SQLite locked | `OperationalError` | Retry 3× with backoff (50ms, 200ms, 1s); fail → log + ERROR | Status=ERROR after retries |
| Watched dir unmounted/deleted | Observer fires error event | Log error, keep DB row, status endpoint exposes; user must DELETE via API | Doc rows remain but watcher idle for that dir |
| Symlink-based overlap bypass | `resolve(strict=True)` at add-time | Canonical path used for overlap check | 409 on overlap |

## 10. Edge cases

- **Atomic save by editors:** vim/VSCode use `write tmp → rename tmp → orig`. Watchdog produces `created(tmp) + moved(tmp → orig)` or `created(tmp) + deleted(orig) + moved(tmp → orig)`. Debounce coalesces. Editor swap-file patterns (`*.swp`, `*~`, `.#*`) belong in `HARDCODED_IGNORE_GLOBS`.
- **Symlinks:** `Observer.schedule(recursive=True)` does not follow symlinks by default. Keep that — avoids loops. Documented in README.
- **Files exceeding `max_file_size_mb`:** Skipped in `_handle_upsert` with warning, same limit as upload endpoint.
- **Identical content at different paths:** Both indexed. Identity is path, not content (per §4.2 invariants). MMR diversification (`mmr_lambda` already configured) deduplicates in search results.
- **Watched dir inside `data_dir`:** Rejected at add-time. Would otherwise create indexing loops.
- **macOS fsevents drops:** 10-minute recovery reconcile is the backstop. User can also `docgraph watch reconcile` manually.
- **Adding a 100K-file dir:** Reconcile bounded by `asyncio.sleep(0)` every 100 paths. POST returns 201 immediately; reconcile runs in background. User watches `events_processed` rise in status.

## 11. Observability

Reuse the `search_metrics`-style structured logging pattern already in `mcp/search.py`:

```python
logger.info("watch_event", extra={
    "action": event.action,
    "path_hash": _hash_for_log(event.src_path),  # privacy: hash, not raw path
    "wd_id": wd.id,
    "materialize": materialize,
    "result": "indexed" | "skipped_noop" | "skipped_unsupported" | "error",
    "duration_ms": int((time.monotonic() - t0) * 1000),
})
```

Ready to plug into structured JSON logging when roadmap 4.1 ships.

## 12. Testing strategy

### 12.1 Unit (no filesystem)

| File | Test |
|---|---|
| `tests/watch/test_ignore.py` | Hardcoded list match, `.docgraphignore` parse, glob match |
| `tests/watch/test_debounce.py` | Per-path coalesce, cancel-and-replace, independence across paths |
| `tests/watch/test_manager_state.py` | State transitions, idempotent enable, 409 in ENABLING |
| `tests/watch/test_partitioning.py` | `hash(path) % N` stability, same path → same worker |
| `tests/watch/test_dispatch.py` | `detect_materialize` per extension, unsupported returns None |

### 12.2 Integration (`tmp_path` fixture)

| File | Test |
|---|---|
| `tests/watch/test_reconcile_int.py` | Create dir + 3 files, reconcile → 3 docs. mtime change → reindex. Delete → cascade |
| `tests/watch/test_live_watcher_int.py` | Start manager, write file, wait debounce window, assert ready. `.docgraphignore` live |
| `tests/watch/test_text_vs_binary_int.py` | `.md` → reference (`original_path` None). `.pdf` → materialize (copy in `originals_dir`) |
| `tests/watch/test_rename_int.py` | Move file → path updated, doc_id preserved, no reindex |
| `tests/watch/test_burst_int.py` | Drop 200 files in 100ms, assert no overflow under cap, drop counter increments above cap |
| `tests/watch/test_lifecycle_int.py` | enable → add dir → disable → re-enable → reconcile runs again, stats reset |

### 12.3 API (`httpx.AsyncClient` via FastAPI test app)

| File | Test |
|---|---|
| `tests/web/test_watch_routes.py` | Each endpoint in §5, success path + error matrix |
| `tests/web/test_watch_routes.py::test_overlap_reject` | POST overlapping path → 409 |
| `tests/web/test_watch_routes.py::test_disable_drains_queue` | Enable, enqueue, disable → status shows correct `queue_drained` |
| `tests/web/test_watch_routes.py::test_data_dir_reject` | POST path inside `data_dir` → 400 |

### 12.4 CLI

| File | Test |
|---|---|
| `tests/cli/test_watch_cli.py` | Each command in §6, mock httpx + assert URL/payload/exit code |
| `tests/cli/test_watch_cli.py::test_server_unreachable` | Server down → exit 1, clear message |

### 12.5 macOS-specific (skipped elsewhere)

| File | Test |
|---|---|
| `tests/watch/test_fsevents_atomic_rename.py` | `pytest.mark.skipif(sys.platform != "darwin")`. Simulate vim save pattern, assert exactly 1 UPSERT after debounce |

### 12.6 Explicit non-tests (YAGNI)

- No 1M-event stress tests — bounded queue + dropped-counter is the verifier.
- No tests of `watchdog` library internals — trust upstream.
- No tests of `pathspec` parser logic — trust upstream.

## 13. Configuration knobs

Added to `docgraph/config.py`:

```python
# Watcher
watch_debounce_sec: float = 2.0
watch_queue_capacity: int = 500
watch_workers: int = 4
watch_recovery_interval_sec: int = 600  # 10 min fsevents-drop backstop
watch_extra_text_exts: list[str] = []
watch_extra_binary_exts: list[str] = []
```

All overridable via `DOCGRAPH_WATCH_*` env vars (same pattern as existing config).

## 14. Out of scope (explicit YAGNI)

- Webhook callback when ingest completes (poll status instead).
- Distributed multi-node watcher (depends on roadmap 3.5 sharding).
- LLM-driven auto-tagging of watched docs.
- Remote filesystems (S3, SMB).
- Auth specific to watch endpoints (inherit from roadmap 3.4).
- Encrypted watched files (roadmap 7.3 territory).
- Polling fallback for unsupported FS — `watchdog` already includes `PollingObserver`; bring it in only if a user reports a real failure.

## 15. Dependencies

- `watchdog ^4.0` — cross-platform file system events. Mature, BSD-licensed.
- `pathspec ^0.12` — gitignore-style pattern matching. MIT, no transitive deps.

Both pinned in `pyproject.toml`. Total install footprint < 200 KB.
