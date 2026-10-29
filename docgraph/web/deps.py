from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from docgraph.config import Config
from docgraph.embed.factory import create_embedder, create_reranker
from docgraph.embed.provider import EmbeddingProvider
from docgraph.ingest.indexer import Indexer
from docgraph.store import ChromaStore, FileStore, FtsStore, SQLiteStore

if TYPE_CHECKING:
    from docgraph.embed.rerank import Reranker
    from docgraph.mcp.search import SearchService
    from docgraph.watch.manager import WatcherManager

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    cfg: Config
    sqlite: SQLiteStore
    files: FileStore
    chroma: ChromaStore
    embedder: EmbeddingProvider
    fts: Optional[FtsStore] = field(default=None)
    reranker: Optional["Reranker"] = field(default=None)
    _indexer: Optional[Indexer] = field(default=None, repr=False)
    _watcher: Optional["WatcherManager"] = field(default=None, repr=False, compare=False)

    @classmethod
    def create(cls, cfg: Config) -> "AppState":
        cfg.ensure_dirs()
        sqlite = SQLiteStore(cfg)
        sqlite.init_schema()
        fts = FtsStore(cfg) if cfg.hybrid_enabled else None
        return cls(
            cfg=cfg,
            sqlite=sqlite,
            files=FileStore(cfg),
            chroma=ChromaStore(cfg),
            embedder=create_embedder(cfg),
            fts=fts,
            reranker=create_reranker(cfg),
        )

    def indexer(self) -> Indexer:
        if self._indexer is None:
            from docgraph.ingest.tokenizer import get_token_counter

            self._indexer = Indexer(
                self.cfg,
                self.sqlite,
                self.files,
                self.chroma,
                self.embedder,
                fts=self.fts,
                counter=get_token_counter(self.cfg),
            )
        return self._indexer

    @property
    def watcher(self) -> "WatcherManager":
        if self._watcher is None:
            from docgraph.watch.manager import WatcherManager
            self._watcher = WatcherManager(self)
        return self._watcher

    def search_service(self) -> "SearchService":
        from docgraph.mcp.search import SearchService  # deferred to avoid circular import
        return SearchService(
            self.cfg, self.sqlite, self.chroma, self.embedder,
            fts=self.fts, reranker=self.reranker,
        )

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
