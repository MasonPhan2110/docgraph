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
    ignore_file = tmp_path / ".docgraphignore"
    ignore_file.write_text("\x00\x00invalid bytes")
    matcher = IgnoreMatcher(wd)
    # Should not raise; should not block normal files.
    assert matcher.should_ignore(tmp_path / "ok.md") is False
