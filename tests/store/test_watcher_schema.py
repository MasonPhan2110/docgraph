import sqlite3
from pathlib import Path

import pytest

from docgraph.config import Config
from docgraph.models import DocumentRecord, DocumentStatus, SourceType, WatchedDirRecord
from docgraph.store.sqlite import SQLiteStore


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config(data_dir=tmp_path)
    c.ensure_dirs()
    return c


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_documents_has_new_columns(cfg: Config):
    store = SQLiteStore(cfg)
    store.init_schema()
    with sqlite3.connect(cfg.sqlite_path) as conn:
        cols = _columns(conn, "documents")
    assert "watched_path" in cols
    assert "materialize" in cols
    assert "mtime_ns" in cols


def test_watched_dirs_table_exists(cfg: Config):
    store = SQLiteStore(cfg)
    store.init_schema()
    with sqlite3.connect(cfg.sqlite_path) as conn:
        cols = _columns(conn, "watched_dirs")
    assert {"id", "path", "folder", "tags", "ignore_globs", "created_at"} <= cols


def test_watcher_state_table_exists(cfg: Config):
    store = SQLiteStore(cfg)
    store.init_schema()
    with sqlite3.connect(cfg.sqlite_path) as conn:
        cols = _columns(conn, "watcher_state")
    assert {"key", "value"} <= cols


def test_partial_unique_index_on_watched_path(cfg: Config):
    store = SQLiteStore(cfg)
    store.init_schema()
    with sqlite3.connect(cfg.sqlite_path) as conn:
        idx = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_documents_watched_path'"
        ).fetchone()
    assert idx is not None


def test_schema_migration_idempotent(cfg: Config):
    store = SQLiteStore(cfg)
    store.init_schema()
    store.init_schema()  # second init must not raise


def test_partial_unique_allows_multiple_null_watched_path(cfg: Config):
    store = SQLiteStore(cfg)
    store.init_schema()
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
    store = SQLiteStore(cfg)
    store.init_schema()
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


def test_insert_and_list_watched_dirs(cfg: Config):
    store = SQLiteStore(cfg)
    store.init_schema()
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
    store = SQLiteStore(cfg)
    store.init_schema()
    wd = WatchedDirRecord(id="wd_b", path="/tmp/x", created_at="2026-06-07T14:00:00Z")
    store.insert_watched_dir(wd)
    got = store.get_watched_dir("wd_b")
    assert got is not None and got.path == "/tmp/x"
    assert store.get_watched_dir("wd_missing") is None


def test_delete_watched_dir(cfg: Config):
    store = SQLiteStore(cfg)
    store.init_schema()
    store.insert_watched_dir(WatchedDirRecord(id="wd_c", path="/tmp/y", created_at="2026-06-07T14:00:00Z"))
    deleted = store.delete_watched_dir("wd_c")
    assert deleted is True
    assert store.get_watched_dir("wd_c") is None
    assert store.delete_watched_dir("wd_c") is False  # already gone


def test_watcher_state_get_set(cfg: Config):
    store = SQLiteStore(cfg)
    store.init_schema()
    assert store.get_watcher_state("enabled") is None
    store.set_watcher_state("enabled", "true")
    assert store.get_watcher_state("enabled") == "true"
    store.set_watcher_state("enabled", "false")  # upsert
    assert store.get_watcher_state("enabled") == "false"


def _make_watched_doc(store: SQLiteStore, doc_id: str, path: str, mtime: int) -> None:
    doc = DocumentRecord(
        id=doc_id, filename="x.md", folder="", tags=[],
        source_type=SourceType.WATCHED, status=DocumentStatus.READY,
        watched_path=path, materialize=False, mtime_ns=mtime,
    )
    store.insert_document(doc)


def test_get_doc_by_watched_path(cfg: Config):
    store = SQLiteStore(cfg)
    store.init_schema()
    _make_watched_doc(store, "d1", "/tmp/notes/a.md", 1000)
    got = store.get_doc_by_watched_path("/tmp/notes/a.md")
    assert got is not None and got.id == "d1"
    assert got.mtime_ns == 1000
    assert store.get_doc_by_watched_path("/tmp/notes/missing.md") is None


def test_list_watched_docs_by_prefix(cfg: Config):
    store = SQLiteStore(cfg)
    store.init_schema()
    _make_watched_doc(store, "d1", "/tmp/notes/a.md", 1)
    _make_watched_doc(store, "d2", "/tmp/notes/sub/b.md", 2)
    _make_watched_doc(store, "d3", "/tmp/other/c.md", 3)
    docs = store.list_watched_docs(prefix="/tmp/notes")
    paths = sorted(d.watched_path for d in docs)
    assert paths == ["/tmp/notes/a.md", "/tmp/notes/sub/b.md"]


def test_update_watched_path(cfg: Config):
    store = SQLiteStore(cfg)
    store.init_schema()
    _make_watched_doc(store, "d1", "/tmp/old.md", 1)
    store.update_watched_path("d1", "/tmp/new.md")
    assert store.get_doc_by_watched_path("/tmp/old.md") is None
    assert store.get_doc_by_watched_path("/tmp/new.md").id == "d1"


def test_update_mtime_ns(cfg: Config):
    store = SQLiteStore(cfg)
    store.init_schema()
    _make_watched_doc(store, "d1", "/tmp/a.md", 100)
    store.update_mtime_ns("d1", 999)
    got = store.get_doc_by_watched_path("/tmp/a.md")
    assert got.mtime_ns == 999


def test_reset_interrupted_watched_docs(cfg: Config):
    """A watched doc left PROCESSING by a crash already has mtime_ns recorded, so
    reconcile would skip it forever. Reset must clear mtime and leave it claimable."""
    store = SQLiteStore(cfg)
    store.init_schema()
    _make_watched_doc(store, "stuck", "/tmp/notes/a.md", 1000)
    store.update_status("stuck", DocumentStatus.PROCESSING)
    _make_watched_doc(store, "done", "/tmp/notes/b.md", 2000)
    store.insert_document(DocumentRecord(
        id="upload", filename="u.md", folder="", tags=[], status=DocumentStatus.PROCESSING,
    ))

    assert store.reset_interrupted_watched_docs() == 1

    stuck = store.get_document("stuck")
    assert stuck.status == DocumentStatus.ERROR
    assert stuck.mtime_ns is None
    assert stuck.error_message
    assert store.claim_for_reindex("stuck") is True
    assert store.get_document("done").status == DocumentStatus.READY
    assert store.get_document("done").mtime_ns == 2000
    assert store.get_document("upload").status == DocumentStatus.PROCESSING
