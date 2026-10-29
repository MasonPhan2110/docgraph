from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from docgraph.config import Config
from docgraph.models import DocumentRecord, DocumentStatus, SourceType


@pytest.mark.asyncio
async def test_index_text_direct_dispatches_to_index_markdown(tmp_path: Path):
    """index_text_direct should chunk-and-embed the raw text via the existing index_markdown path."""
    from docgraph.web.deps import AppState
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    state.sqlite.insert_document(DocumentRecord(
        id="d_text", filename="raw.md", folder="", tags=[],
        source_type=SourceType.WATCHED, status=DocumentStatus.PROCESSING,
        watched_path=str(tmp_path / "raw.md"),
        materialize=False, mtime_ns=1,
    ))
    indexer = state.indexer()
    # Stub the heavy work: replace index_markdown with an AsyncMock so we don't need
    # a working embedder. The test verifies dispatch happens with the right args.
    indexer.index_markdown = AsyncMock()  # type: ignore[method-assign]
    await indexer.index_text_direct("d_text", "# Heading\n\nbody.")
    indexer.index_markdown.assert_awaited_once_with("d_text", "# Heading\n\nbody.")


@pytest.mark.asyncio
async def test_index_text_direct_raises_on_missing_doc(tmp_path: Path):
    from docgraph.web.deps import AppState
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    state = AppState.create(cfg)
    indexer = state.indexer()
    with pytest.raises(ValueError, match="document not found"):
        await indexer.index_text_direct("d_missing", "text")
