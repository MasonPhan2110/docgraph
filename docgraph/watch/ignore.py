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
        self._hardcoded_globs_spec = pathspec.PathSpec.from_lines(
            "gitwildmatch", list(HARDCODED_IGNORE_GLOBS)
        )

    def should_ignore(self, path: Path) -> bool:
        try:
            rel = path.relative_to(self._root)
        except ValueError:
            return True  # outside watched root
        # Layer 1: hardcoded.
        for part in rel.parts:
            if part in HARDCODED_IGNORE_DIRS:
                return True
        if path.name in HARDCODED_IGNORE_FILES:
            return True
        rel_str = str(rel)
        if self._hardcoded_globs_spec.match_file(rel_str):
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
                lines = self._docgraphignore_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                self._cached_spec = pathspec.PathSpec.from_lines("gitwildmatch", lines)
            except Exception as exc:
                logger.warning(
                    "failed to parse .docgraphignore at %s: %s",
                    self._docgraphignore_path, exc,
                )
                self._cached_spec = pathspec.PathSpec.from_lines("gitwildmatch", [])
            self._cached_mtime = mtime
        return self._cached_spec.match_file(rel_str) if self._cached_spec else False
