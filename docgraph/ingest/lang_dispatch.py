from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from docgraph.ingest.chunker import chunk_code, chunk_markdown_sections
from docgraph.ingest.structured_chunker import chunk_json, chunk_yaml
from docgraph.ingest.treesitter_chunker import chunk_code_treesitter


def _make_treesitter_chunker(language: str):
    def _chunk(text: str, chunk_size: int = 512, chunk_overlap: int = 64, **kwargs):
        return chunk_code_treesitter(
            text, language, chunk_size, chunk_overlap, counter=kwargs.get("counter")
        )

    _chunk.__name__ = "chunk_code_treesitter"
    return _chunk


_REGISTRY: dict[str, Any] = {
    "markdown": chunk_markdown_sections,
    "json": chunk_json,
    "yaml": chunk_yaml,
}


def dispatch_chunker(
    language: str | None,
    *,
    code_chunker: str = "regex",
) -> Any:
    if language and language in _REGISTRY:
        return _REGISTRY[language]

    if code_chunker == "treesitter" and language:
        from docgraph.ingest.treesitter_chunker import _get_parser

        if _get_parser(language) is not None:
            return _make_treesitter_chunker(language)

    return chunk_code


def normalize_chunk_output(
    pieces: list[Any],
) -> list[tuple[str, list[str], dict[str, Any]]]:
    """Normalize chunker output to (text, heading_path, meta) tuples."""
    out: list[tuple[str, list[str], dict[str, Any]]] = []
    for piece in pieces:
        if hasattr(piece, "text"):
            heading_path = list(getattr(piece, "heading_path", []))
            meta = {
                "partial": getattr(piece, "partial", False),
                "overflow": getattr(piece, "overflow", False),
                "tokens": getattr(piece, "tokens", 0),
                "heading_path": heading_path,
            }
            out.append((piece.text, heading_path, meta))
        elif isinstance(piece, str):
            out.append((piece, [], {}))
        else:
            raise TypeError(f"unexpected chunk type: {type(piece)}")
    return out


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
