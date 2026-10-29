from __future__ import annotations

import json
import sqlite3
from typing import Optional

from docgraph.config import Config
from docgraph.models import DocumentRecord, DocumentStatus, SourceType, WatchedDirRecord


class SQLiteStore:
    def __init__(self, cfg: Config) -> None:
        self._path = cfg.sqlite_path

    def _connect(self) -> sqlite3.Connection:
        # 5s busy_timeout lets SQLite wait for write locks to clear before
        # raising OperationalError("database is locked"). Replaces the spec's
        # Python-level 3× retry — SQLite handles the wait natively and the
        # bound (~5s) exceeds any reasonable lock-hold (typically <100ms).
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        # WAL lets the UI poll /api/documents while the indexer writes progress
        # without lock contention. NORMAL sync is safe enough for a local tool.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
        if "progress_pct" not in cols:
            conn.execute(
                "ALTER TABLE documents ADD COLUMN progress_pct INTEGER NOT NULL DEFAULT 0"
            )
        if "progress_phase" not in cols:
            conn.execute(
                "ALTER TABLE documents ADD COLUMN progress_phase TEXT NOT NULL DEFAULT ''"
            )
        if "source_type" not in cols:
            conn.execute(
                "ALTER TABLE documents ADD COLUMN source_type TEXT NOT NULL DEFAULT 'file'"
            )
        if "source_url" not in cols:
            conn.execute(
                "ALTER TABLE documents ADD COLUMN source_url TEXT NOT NULL DEFAULT ''"
            )
        # Hybrid search FTS5 index.
        # tokenchars '_.-' preserves identifiers like `embed_query`, `v1.5`.
        conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                chunk_id UNINDEXED,
                doc_id UNINDEXED,
                folder UNINDEXED,
                tags UNINDEXED,
                chunk_index UNINDEXED,
                text,
                filename,
                tokenize="unicode61 remove_diacritics 2 tokenchars '_.-'"
            );
            """
        )

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

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    folder TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'processing',
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    progress_pct INTEGER NOT NULL DEFAULT 0,
                    progress_phase TEXT NOT NULL DEFAULT '',
                    error_message TEXT,
                    original_path TEXT NOT NULL DEFAULT '',
                    markdown_path TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL DEFAULT 'file',
                    source_url TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_documents_folder ON documents(folder);
                CREATE INDEX IF NOT EXISTS idx_documents_created_at ON documents(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
            """)
            self._migrate_schema(conn)
        self._ensure_watcher_schema()

    def insert_document(self, doc: DocumentRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO documents
                   (id, filename, folder, tags, status, chunk_count, progress_pct,
                    progress_phase, error_message, original_path, markdown_path,
                    source_type, source_url, watched_path, materialize, mtime_ns)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    doc.id, doc.filename, doc.folder, json.dumps(doc.tags),
                    doc.status.value, doc.chunk_count, doc.progress_pct,
                    doc.progress_phase, doc.error_message,
                    doc.original_path, doc.markdown_path,
                    doc.source_type.value, doc.source_url,
                    doc.watched_path,
                    int(doc.materialize) if doc.materialize is not None else None,
                    doc.mtime_ns,
                ),
            )

    def _row_to_doc(self, row: sqlite3.Row) -> DocumentRecord:
        keys = row.keys()
        raw_materialize = row["materialize"] if "materialize" in keys else None
        return DocumentRecord(
            id=row["id"],
            filename=row["filename"],
            folder=row["folder"],
            tags=json.loads(row["tags"]),
            status=DocumentStatus(row["status"]),
            chunk_count=row["chunk_count"],
            progress_pct=int(row["progress_pct"]) if "progress_pct" in keys else 0,
            progress_phase=row["progress_phase"] if "progress_phase" in keys else "",
            error_message=row["error_message"],
            original_path=row["original_path"] or None,
            markdown_path=row["markdown_path"],
            source_type=SourceType(row["source_type"]) if "source_type" in keys else SourceType.FILE,
            source_url=row["source_url"] if "source_url" in keys else "",
            watched_path=row["watched_path"] if "watched_path" in keys else None,
            materialize=bool(raw_materialize) if raw_materialize is not None else None,
            mtime_ns=row["mtime_ns"] if "mtime_ns" in keys else None,
        )

    def get_document(self, doc_id: str) -> Optional[DocumentRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
        return self._row_to_doc(row) if row else None

    def list_documents(
        self,
        folder: Optional[str] = None,
        tag: Optional[str] = None,
        status: Optional[DocumentStatus] = None,
    ) -> list[DocumentRecord]:
        query = "SELECT * FROM documents WHERE 1=1"
        params: list = []
        if folder is not None:
            query += " AND folder = ?"
            params.append(folder)
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        docs = [self._row_to_doc(r) for r in rows]
        if tag:
            docs = [d for d in docs if tag in d.tags]
        return docs

    def claim_for_reindex(self, doc_id: str) -> bool:
        """Atomically flip status to PROCESSING iff not already PROCESSING.

        Returns True if this caller claimed the slot. False means another
        indexing pass is already in flight — caller must NOT spawn a second
        BG task or duplicate FTS rows pile up (fts upsert is plain INSERT).
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE documents SET status=?, progress_pct=0, progress_phase='' "
                "WHERE id=? AND status<>?",
                (
                    DocumentStatus.PROCESSING.value,
                    doc_id,
                    DocumentStatus.PROCESSING.value,
                ),
            )
            return cur.rowcount == 1

    def update_progress(self, doc_id: str, pct: int, phase: str = "") -> None:
        pct = max(0, min(100, int(pct)))
        with self._connect() as conn:
            conn.execute(
                "UPDATE documents SET progress_pct=?, progress_phase=? WHERE id=?",
                (pct, phase, doc_id),
            )

    def update_status(
        self,
        doc_id: str,
        status: DocumentStatus,
        chunk_count: int = 0,
        error_message: Optional[str] = None,
        markdown_path: str = "",
    ) -> None:
        with self._connect() as conn:
            if status == DocumentStatus.READY:
                conn.execute(
                    """UPDATE documents SET status=?, chunk_count=?,
                       error_message=?, markdown_path=?, progress_pct=100, progress_phase=''
                       WHERE id=?""",
                    (status.value, chunk_count, error_message, markdown_path, doc_id),
                )
            elif status == DocumentStatus.ERROR:
                # Preserve last progress_phase so the UI shows where indexing failed.
                conn.execute(
                    """UPDATE documents SET status=?, chunk_count=?,
                       error_message=?, markdown_path=?
                       WHERE id=?""",
                    (status.value, chunk_count, error_message, markdown_path, doc_id),
                )
            else:
                conn.execute(
                    """UPDATE documents SET status=?, chunk_count=?,
                       error_message=?, markdown_path=?, progress_pct=0, progress_phase=''
                       WHERE id=?""",
                    (status.value, chunk_count, error_message, markdown_path, doc_id),
                )

    def update_tags_folder(self, doc_id: str, tags: list[str], folder: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE documents SET tags=?, folder=? WHERE id=?",
                (json.dumps(tags), folder, doc_id),
            )

    def update_filename(self, doc_id: str, filename: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE documents SET filename=? WHERE id=?",
                (filename, doc_id),
            )

    def delete_document(self, doc_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))

    # ------------------------------------------------------------------
    # watched_dirs CRUD
    # ------------------------------------------------------------------

    def insert_watched_dir(self, wd: WatchedDirRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO watched_dirs (id, path, folder, tags, ignore_globs, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (wd.id, wd.path, wd.folder, json.dumps(wd.tags),
                 json.dumps(wd.ignore_globs), wd.created_at),
            )

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
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # watcher_state CRUD
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # watched-doc queries
    # ------------------------------------------------------------------

    def get_doc_by_watched_path(self, path: str) -> DocumentRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE watched_path = ?", (path,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_doc(row)

    def list_watched_docs(self, prefix: str) -> list[DocumentRecord]:
        # GLOB is case-sensitive so SQLite can use the partial unique index
        # on watched_path for a B-tree range scan. LIKE falls back to a full
        # table scan whenever case_sensitive_like is off (the default).
        exact = prefix.rstrip("/")
        glob_children = exact + "/*"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM documents "
                "WHERE watched_path = ? OR watched_path GLOB ?",
                (exact, glob_children),
            ).fetchall()
        return [self._row_to_doc(r) for r in rows]

    def update_watched_path(self, doc_id: str, new_path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE documents SET watched_path = ? WHERE id = ?",
                (new_path, doc_id),
            )

    def reset_interrupted_watched_docs(self) -> int:
        """Release watched docs left PROCESSING by a crash/kill mid-index.

        Only safe at startup, before any indexing task exists. Clearing mtime_ns
        makes the next reconcile see a delta and re-UPSERT; ERROR status lets
        claim_for_reindex take the slot.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE documents SET status=?, mtime_ns=NULL, "
                "error_message='interrupted: server stopped mid-index' "
                "WHERE source_type=? AND status=?",
                (
                    DocumentStatus.ERROR.value,
                    SourceType.WATCHED.value,
                    DocumentStatus.PROCESSING.value,
                ),
            )
            return cur.rowcount

    def update_mtime_ns(self, doc_id: str, mtime_ns: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE documents SET mtime_ns = ? WHERE id = ?",
                (mtime_ns, doc_id),
            )

    def update_original_path(self, doc_id: str, original_path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE documents SET original_path = ? WHERE id = ?",
                (original_path, doc_id),
            )
