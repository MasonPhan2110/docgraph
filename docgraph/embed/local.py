from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ONNX Runtime's CPU arena grows to fit the largest batch and never shrinks.
# Measured on nomic-embed-text with ~512-token chunks: bs=4 plateaus at ~1.5 GB
# RSS vs ~3.5 GB at bs=16 and ~5 GB at bs=64, at the same ~2.6 chunks/s.
EMBED_BATCH_SIZE = 4


class LocalEmbedder:
    """In-process embedder via Rust (docgraph_embed / fastembed ONNX)."""

    HEALTH_MSG = (
        "Local Rust embedder unavailable. "
        "Build and install: cd crates/docgraph-embed && maturin develop --release"
    )

    def __init__(self, model: str, cache_dir: Path) -> None:
        self._model = model
        self._cache_dir = cache_dir
        self._init_lock = asyncio.Lock()
        # ONNX already uses every core per call, so concurrent callers (watcher
        # workers, uploads, queries) gain no throughput — they only stack peak
        # memory. Serialize per batch so a long document can't starve queries.
        self._infer_lock = asyncio.Lock()
        self._initialized = False

    def _import_native(self):
        try:
            import docgraph_embed
        except ImportError as exc:
            raise RuntimeError(self.HEALTH_MSG) from exc
        return docgraph_embed

    async def _ensure_init(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            native = self._import_native()
            cache = str(self._cache_dir)
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(native.init, self._model, cache)
            self._initialized = True
            dim = await asyncio.to_thread(native.embedding_dimension)
            logger.info(
                "Local embedder ready: model=%s dim=%s cache=%s",
                self._model,
                dim,
                cache,
            )

    async def embed(
        self, texts: list[str], *, for_query: bool = False
    ) -> list[list[float]]:
        if not texts:
            return []
        await self._ensure_init()
        native = self._import_native()
        from docgraph.embed.prefix import apply_embed_prefix

        prepared = apply_embed_prefix(texts, self._model, for_query=for_query)
        all_vectors: list[list[float]] = []
        for start in range(0, len(prepared), EMBED_BATCH_SIZE):
            batch = prepared[start : start + EMBED_BATCH_SIZE]
            async with self._infer_lock:
                rows = await asyncio.to_thread(native.embed, batch)
            all_vectors.extend(rows)
        return all_vectors

    async def health_check(self) -> None:
        await self._ensure_init()
        native = self._import_native()
        await asyncio.to_thread(native.health_check)
