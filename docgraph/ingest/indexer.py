from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from docgraph.config import Config
from docgraph.embed.local import EMBED_BATCH_SIZE
from docgraph.embed.provider import EmbeddingProvider
from docgraph.ingest.chunker import MarkdownChunk, chunk_markdown_sections
from docgraph.ingest.code_dump import detect_repomix, infer_language, parse_repomix
from docgraph.ingest.converter import convert_file_to_markdown
from docgraph.ingest.crawler import UrlCrawler
from docgraph.ingest.lang_dispatch import dispatch_chunker, normalize_chunk_output
from docgraph.ingest.tokenizer import TokenCounter, get_token_counter
from docgraph.models import DocumentStatus, SourceType
from docgraph.store.chroma import ChromaStore
from docgraph.store.files import FileStore
from docgraph.store.fts import FtsStore
from docgraph.store.sqlite import SQLiteStore

logger = logging.getLogger(__name__)


def _read_text_prefix(path: Path, limit: int = 65536) -> str:
    """Read up to `limit` chars of a file as text, ignoring decode errors."""
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        return f.read(limit)


class Indexer:
    def __init__(
        self,
        cfg: Config,
        sqlite: SQLiteStore,
        files: FileStore,
        chroma: ChromaStore,
        embedder: EmbeddingProvider,
        fts: "FtsStore | None" = None,
        counter: TokenCounter | None = None,
    ) -> None:
        self._cfg = cfg
        self._sqlite = sqlite
        self._files = files
        self._chroma = chroma
        self._embedder = embedder
        self._fts = fts
        self._counter = counter or get_token_counter(cfg)

    def _progress(self, doc_id: str, pct: int, phase: str) -> None:
        self._sqlite.update_progress(doc_id, pct, phase)

    async def _embed_with_progress(
        self, doc_id: str, chunks: list[str]
    ) -> list[list[float]]:
        total = len(chunks)
        vectors: list[list[float]] = []
        for start in range(0, total, EMBED_BATCH_SIZE):
            batch = chunks[start : start + EMBED_BATCH_SIZE]
            vectors.extend(await self._embedder.embed(batch, for_query=False))
            done = min(start + len(batch), total)
            pct = 45 + int(50 * done / total)
            self._progress(
                doc_id,
                pct,
                f"Embedding {done}/{total} chunks ({pct}%)",
            )
        return vectors

    def _build_chroma_chunk(
        self,
        doc,
        i: int,
        text: str,
        vec: list[float],
        *,
        heading_path: list[str] | None = None,
        file_path: str | None = None,
        language: str | None = None,
        chunker_name: str | None = None,
        tokens: int = 0,
        partial: bool = False,
        overflow: bool = False,
    ) -> dict:
        metadata = {
            "doc_id": doc.id,
            "filename": doc.filename,
            "folder": doc.folder,
            "tags": json.dumps(doc.tags),
            "chunk_index": i,
            "heading_path": json.dumps(heading_path or []),
            "tokens": tokens or self._counter.count(text),
            "partial": partial,
            "overflow": overflow,
        }
        if doc.source_url:
            metadata["source_url"] = doc.source_url
        if file_path:
            metadata["file_path"] = file_path
        if language:
            metadata["language"] = language
        if chunker_name:
            metadata["chunker"] = chunker_name
        return {
            "id": f"{doc.id}_{i}",
            "embedding": vec,
            "text": text,
            "metadata": metadata,
        }

    async def index_markdown(self, doc_id: str, markdown: str) -> None:
        doc = self._sqlite.get_document(doc_id)
        if doc is None:
            raise ValueError(f"document not found: {doc_id}")
        try:
            self._progress(doc_id, 28, "Converted — splitting chunks (28%)")
            md_path = self._files.save_markdown(doc_id, markdown)
            md_chunks: list[MarkdownChunk] = await asyncio.to_thread(
                chunk_markdown_sections,
                markdown,
                self._cfg.chunk_size,
                self._cfg.chunk_overlap,
                counter=self._counter,
                heading_prefix_enabled=self._cfg.heading_prefix_enabled,
                atomic_blocks_enabled=self._cfg.atomic_blocks_enabled,
            )
            chunks = [c.text for c in md_chunks]
            if not chunks:
                raise ValueError("no chunks produced from document")
            if len(chunks) > self._cfg.max_chunks_per_doc:
                raise ValueError(
                    f"document exceeds max_chunks_per_doc "
                    f"({len(chunks)} > {self._cfg.max_chunks_per_doc}); "
                    f"increase DOCGRAPH_MAX_CHUNKS_PER_DOC or split the source"
                )

            self._progress(
                doc_id,
                42,
                f"Chunked into {len(chunks)} parts (42%)",
            )
            vectors = await self._embed_with_progress(doc_id, chunks)
            self._progress(doc_id, 96, "Saving to index (96%)")
            chroma_chunks = []
            fts_chunks = []
            for i, (mc, vec) in enumerate(zip(md_chunks, vectors)):
                if mc.overflow:
                    logger.warning(
                        "chunk %s_%d overflows budget (%d tokens)",
                        doc_id,
                        i,
                        mc.tokens,
                    )
                chroma_chunks.append(
                    self._build_chroma_chunk(
                        doc,
                        i,
                        mc.text,
                        vec,
                        heading_path=mc.heading_path,
                        tokens=mc.tokens,
                        partial=mc.partial,
                        overflow=mc.overflow,
                        chunker_name="chunk_markdown_sections",
                    )
                )
                fts_chunks.append({
                    "chunk_id": f"{doc_id}_{i}",
                    "doc_id": doc_id,
                    "folder": doc.folder,
                    "tags": json.dumps(doc.tags),
                    "chunk_index": i,
                    "text": mc.text,
                    "filename": doc.filename,
                })
            self._chroma.upsert_chunks(chroma_chunks)
            if self._fts is not None and self._cfg.hybrid_enabled:
                try:
                    self._fts.upsert_chunks(fts_chunks)
                except Exception as exc:
                    logger.warning(
                        "FTS5 upsert failed for doc_id=%s (search will degrade to vector-only): %s",
                        doc_id, exc,
                    )
            self._sqlite.update_status(
                doc_id,
                DocumentStatus.READY,
                chunk_count=len(chunks),
                markdown_path=str(md_path),
            )
        except Exception as exc:
            self._sqlite.update_status(
                doc_id,
                DocumentStatus.ERROR,
                error_message=str(exc),
            )
            raise

    async def index_code_dump(self, doc_id: str, text: str) -> None:
        doc = self._sqlite.get_document(doc_id)
        if doc is None:
            raise ValueError(f"document not found: {doc_id}")
        try:
            self._progress(doc_id, 10, "Parsing code dump (10%)")
            parsed = parse_repomix(text)
            if not parsed:
                raise ValueError("could not parse code dump")
            md_path = self._files.save_markdown(doc_id, text)
            self._progress(doc_id, 28, "Splitting code into chunks (28%)")
            chunk_texts: list[str] = []
            chunk_files: list[str] = []
            chunk_meta: list[dict] = []
            chunk_languages: list[str] = []
            chunker_names: list[str] = []

            for file_path, content in parsed:
                language = infer_language(file_path)
                chunker_fn = dispatch_chunker(
                    language, code_chunker=self._cfg.code_chunker
                )
                chunker_name = getattr(chunker_fn, "__name__", chunker_fn.__class__.__name__)
                kwargs = {
                    "counter": self._counter,
                    "heading_prefix_enabled": self._cfg.heading_prefix_enabled,
                    "atomic_blocks_enabled": self._cfg.atomic_blocks_enabled,
                }
                pieces = await asyncio.to_thread(
                    chunker_fn,
                    content,
                    self._cfg.chunk_size,
                    self._cfg.chunk_overlap,
                    **kwargs,
                )
                normalized = normalize_chunk_output(pieces)
                for text_piece, heading_path, meta in normalized:
                    chunk_texts.append(text_piece)
                    chunk_files.append(file_path)
                    chunk_meta.append({**meta, "heading_path": heading_path})
                    chunk_languages.append(language or "")
                    chunker_names.append(chunker_name)

            if not chunk_texts:
                raise ValueError("no chunks produced from code dump")
            if len(chunk_texts) > self._cfg.max_chunks_per_doc:
                raise ValueError(
                    f"document exceeds max_chunks_per_doc "
                    f"({len(chunk_texts)} > {self._cfg.max_chunks_per_doc}); "
                    f"increase DOCGRAPH_MAX_CHUNKS_PER_DOC or split the source"
                )
            self._progress(
                doc_id, 42, f"Chunked into {len(chunk_texts)} parts (42%)"
            )
            vectors = await self._embed_with_progress(doc_id, chunk_texts)
            self._progress(doc_id, 96, "Saving to index (96%)")
            chroma_chunks = []
            fts_chunks = []
            for i, (piece, vec, file_path, meta, lang, cname) in enumerate(
                zip(chunk_texts, vectors, chunk_files, chunk_meta, chunk_languages, chunker_names)
            ):
                chroma_chunks.append(
                    self._build_chroma_chunk(
                        doc,
                        i,
                        piece,
                        vec,
                        heading_path=meta.get("heading_path") or [],
                        file_path=file_path,
                        language=lang or None,
                        chunker_name=cname,
                        tokens=meta.get("tokens", 0),
                        partial=meta.get("partial", False),
                        overflow=meta.get("overflow", False),
                    )
                )
                fts_chunks.append({
                    "chunk_id": f"{doc_id}_{i}",
                    "doc_id": doc_id,
                    "folder": doc.folder,
                    "tags": json.dumps(doc.tags),
                    "chunk_index": i,
                    "text": piece,
                    "filename": doc.filename,
                })
            self._chroma.upsert_chunks(chroma_chunks)
            if self._fts is not None and self._cfg.hybrid_enabled:
                try:
                    self._fts.upsert_chunks(fts_chunks)
                except Exception as exc:
                    logger.warning(
                        "FTS5 upsert failed for doc_id=%s (search will degrade to vector-only): %s",
                        doc_id, exc,
                    )
            self._sqlite.update_status(
                doc_id,
                DocumentStatus.READY,
                chunk_count=len(chunk_texts),
                markdown_path=str(md_path),
            )
        except Exception as exc:
            self._sqlite.update_status(
                doc_id,
                DocumentStatus.ERROR,
                error_message=str(exc),
            )
            raise

    async def index_document(self, doc_id: str, original_path: Path) -> None:
        doc = self._sqlite.get_document(doc_id)
        if doc is None:
            raise ValueError(f"document not found: {doc_id}")
        logger.info(
            "indexing file doc_id=%s path=%s", doc_id, original_path.name
        )
        try:
            prefix = await asyncio.to_thread(_read_text_prefix, original_path)
            if detect_repomix(prefix):
                full_text = await asyncio.to_thread(
                    original_path.read_text, "utf-8", "ignore"
                )
                await self.index_code_dump(doc_id, full_text)
                return
            self._progress(doc_id, 5, "Converting to text (5%)")
            markdown = await asyncio.to_thread(
                convert_file_to_markdown, original_path
            )
            logger.debug(
                "converted doc_id=%s markdown_chars=%d", doc_id, len(markdown)
            )
            self._progress(doc_id, 20, "Converted to markdown (20%)")
            await self.index_markdown(doc_id, markdown)
        except Exception as exc:
            self._sqlite.update_status(
                doc_id,
                DocumentStatus.ERROR,
                error_message=str(exc),
            )
            raise

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

    async def index_url(
        self,
        doc_id: str,
        url: str,
        *,
        crawler: UrlCrawler | None = None,
    ) -> None:
        if crawler is not None:
            await self._index_url_with_crawler(doc_id, url, crawler)
            return
        async with UrlCrawler(self._cfg) as c:
            await self._index_url_with_crawler(doc_id, url, c)

    async def _index_url_with_crawler(
        self, doc_id: str, url: str, crawler: UrlCrawler
    ) -> None:
        doc = self._sqlite.get_document(doc_id)
        if doc is None:
            raise ValueError(f"document not found: {doc_id}")
        logger.info("indexing url doc_id=%s url=%s", doc_id, url)
        try:
            self._progress(doc_id, 5, "Fetching page (5%)")
            markdown, title = await crawler.crawl(url)
            logger.debug(
                "crawled doc_id=%s title=%r markdown_chars=%d",
                doc_id,
                title,
                len(markdown),
            )
            self._sqlite.update_filename(doc_id, title)
            self._progress(doc_id, 20, "Converted to markdown (20%)")
            await self.index_markdown(doc_id, markdown)
        except Exception as exc:
            self._sqlite.update_status(
                doc_id,
                DocumentStatus.ERROR,
                error_message=str(exc),
            )
            raise

    async def index_urls_batch(self, items: list[tuple[str, str]]) -> None:
        """Crawl and index multiple URLs reusing one browser session."""
        if not items:
            return
        try:
            async with UrlCrawler(self._cfg) as crawler:
                for doc_id, url in items:
                    try:
                        await self._index_url_with_crawler(doc_id, url, crawler)
                    except Exception:
                        logger.exception("URL index failed for doc_id=%s url=%s", doc_id, url)
        except Exception as exc:
            # Crawler __aenter__ itself failed (e.g. browser not installed) — no
            # per-URL handler ran, so docs would stay PROCESSING forever. Mark
            # every queued doc ERROR with the launch failure so the UI surfaces it.
            logger.exception("URL crawler failed to start; marking %d docs ERROR", len(items))
            for doc_id, _url in items:
                try:
                    self._sqlite.update_status(
                        doc_id, DocumentStatus.ERROR, error_message=str(exc),
                    )
                except Exception:
                    logger.exception("failed to mark doc %s as ERROR", doc_id)

    async def index_watched_new(
        self,
        doc_id: str,
        watched_path: Path,
        wd,  # WatchedDirRecord
        materialize: bool,
        mtime_ns: int,
    ) -> None:
        """First-time index of a file discovered in a watched dir."""
        from docgraph.models import DocumentRecord, SourceType
        doc = DocumentRecord(
            id=doc_id,
            filename=watched_path.name,
            folder=wd.folder,
            tags=list(wd.tags),
            source_type=SourceType.WATCHED,
            watched_path=str(watched_path),
            materialize=materialize,
            mtime_ns=mtime_ns,
            original_path="",
        )
        # Pre-insert before any file I/O so OSError below can mark doc ERROR.
        self._sqlite.insert_document(doc)
        self._sqlite.update_progress(doc_id, 0, "Queued for watched-index (0%)")
        try:
            if materialize:
                content = await asyncio.to_thread(watched_path.read_bytes)
                orig_path = self._files.save_original(doc_id, watched_path.name, content)
                self._sqlite.update_original_path(doc_id, str(orig_path))
                await self.index_document(doc_id, orig_path)
            else:
                text = await asyncio.to_thread(
                    watched_path.read_text, encoding="utf-8", errors="ignore"
                )
                await self.index_text_direct(doc_id, text)
        except OSError as exc:
            # Permission denied, disk full, file vanished mid-read — surface
            # to user as ERROR rather than leaving doc PROCESSING forever.
            self._sqlite.update_status(
                doc_id, DocumentStatus.ERROR, error_message=str(exc),
            )
            raise
        # The underlying index_* methods set status READY with proper
        # chunk_count + markdown_path. Do NOT re-call update_status here —
        # the default kwargs would zero those fields (see code review #9).
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
        try:
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
        except OSError as exc:
            self._sqlite.update_status(
                doc_id, DocumentStatus.ERROR, error_message=str(exc),
            )
            raise
        self._sqlite.update_mtime_ns(doc_id, mtime_ns)

    async def reindex_document(self, doc_id: str) -> None:
        doc = self._sqlite.get_document(doc_id)
        if doc is None:
            raise ValueError(f"cannot reindex: {doc_id}")
        self._chroma.delete_by_doc_id(doc_id)
        # Delete from FTS unconditionally when fts is available, regardless of
        # cfg.hybrid_enabled — purge stale rows even when hybrid is currently
        # disabled, so toggling hybrid on later doesn't resurrect ghost results.
        if self._fts is not None:
            try:
                self._fts.delete_by_doc_id(doc_id)
            except Exception as exc:
                logger.warning("FTS5 delete failed for doc_id=%s: %s", doc_id, exc)
        self._sqlite.update_status(doc_id, DocumentStatus.PROCESSING)
        self._progress(doc_id, 0, "Starting re-index (0%)")
        if doc.source_type == SourceType.WATCHED and doc.watched_path:
            wp = Path(doc.watched_path)
            if not wp.exists():
                raise ValueError(f"watched file gone: {doc.watched_path}")
            mtime_ns = wp.stat().st_mtime_ns
            await self.reindex_watched(doc_id, wp, bool(doc.materialize), mtime_ns)
        elif doc.source_type == SourceType.URL and doc.source_url:
            await self.index_url(doc_id, doc.source_url)
        elif doc.original_path:
            await self.index_document(doc_id, Path(doc.original_path))
        else:
            raise ValueError(f"cannot reindex: {doc_id}")
