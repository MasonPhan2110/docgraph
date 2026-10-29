from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DocumentStatus(str, Enum):
    PROCESSING = "processing"
    READY = "ready"
    ERROR = "error"


class SourceType(str, Enum):
    FILE = "file"
    URL = "url"
    WATCHED = "watched"


@dataclass
class DocumentRecord:
    id: str
    filename: str
    folder: str = ""
    tags: list[str] = field(default_factory=list)
    status: DocumentStatus = DocumentStatus.PROCESSING
    chunk_count: int = 0
    progress_pct: int = 0
    progress_phase: str = ""
    error_message: Optional[str] = None
    original_path: str = ""
    markdown_path: str = ""
    source_type: SourceType = SourceType.FILE
    source_url: str = ""
    # Watched-file fields (None for FILE/URL docs)
    watched_path: Optional[str] = None
    materialize: Optional[bool] = None
    mtime_ns: Optional[int] = None


@dataclass
class ChunkRecord:
    id: str
    doc_id: str
    text: str
    chunk_index: int
    filename: str
    folder: str
    tags: list[str]
    source_page: Optional[int] = None
    file_path: Optional[str] = None


@dataclass
class SearchResult:
    text: str
    doc_id: str
    filename: str
    folder: str
    tags: list[str]
    chunk_index: int
    score: float
    # Reciprocal Rank Fusion score from the hybrid retrieval stage. Comparable
    # across hits in the same response (unlike `score`, which may mix rerank,
    # vector and BM25 regimes). Does not reflect post-rerank reordering.
    rrf_score: float = 0.0
    source_page: Optional[int] = None
    file_path: Optional[str] = None
    heading_path: Optional[list[str]] = None
    rerank_score: Optional[float] = None


@dataclass
class WatchedDirRecord:
    id: str
    path: str
    folder: str = ""
    tags: list[str] = field(default_factory=list)
    ignore_globs: list[str] = field(default_factory=list)
    created_at: str = ""
