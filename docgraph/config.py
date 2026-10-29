from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _expand_path(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def normalize_ollama_url(url: str) -> str:
    """Use 127.0.0.1 instead of localhost (Ollama on Windows often listens on IPv4 only)."""
    return url.replace("://localhost:", "://127.0.0.1:").replace("://localhost/", "://127.0.0.1/")


@dataclass
class Config:
    data_dir: Path
    web_host: str = "127.0.0.1"
    web_port: int = 8088
    embed_provider: str = "local"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "nomic-embed-text:latest"
    local_model: str = "nomic-embed-text"
    openai_api_key: str = ""
    openai_model: str = "text-embedding-3-small"
    chunk_size: int = 512
    chunk_overlap: int = 64
    max_file_size_mb: int = 50
    default_top_k: int = 5
    min_score: float = 0.3
    crawl_timeout_sec: int = 30
    max_urls_per_import: int = 50
    max_chunks_per_doc: int = 5000
    # Chunking
    tokenizer_source: str = "auto"
    heading_prefix_enabled: bool = True
    atomic_blocks_enabled: bool = True
    code_chunker: str = "regex"
    dedup_enabled: bool = True
    mmr_lambda: float = 0.7
    # Hybrid search
    hybrid_enabled: bool = True
    rrf_k: int = 60
    # Reranker
    rerank_enabled: bool = True
    rerank_model: str = "bge-reranker-v2-m3"
    rerank_top_n: int = 4
    rerank_timeout_sec: float = 15.0
    rerank_prewarm: bool = True
    # Auto-rerank gate
    rerank_score_gap_ratio: float = 0.5
    rerank_min_floor: float = 0.015
    # Watcher
    watch_debounce_sec: float = 2.0
    watch_queue_capacity: int = 500
    watch_workers: int = 2
    watch_recovery_interval_sec: int = 600
    watch_extra_text_exts: list[str] = field(default_factory=list)
    watch_extra_binary_exts: list[str] = field(default_factory=list)

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "data.db"

    @property
    def chroma_path(self) -> Path:
        return self.data_dir / "chroma"

    @property
    def originals_dir(self) -> Path:
        return self.data_dir / "files" / "originals"

    @property
    def markdown_dir(self) -> Path:
        return self.data_dir / "files" / "markdown"

    @property
    def local_model_dir(self) -> Path:
        return self.data_dir / "models"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.chroma_path.mkdir(parents=True, exist_ok=True)
        self.originals_dir.mkdir(parents=True, exist_ok=True)
        self.markdown_dir.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        """Resolve conflicts and enforce bounds. Raise on invalid values."""
        if not self.rerank_enabled and self.rerank_prewarm:
            self.rerank_prewarm = False  # silent coerce — incoherent but harmless
        if self.rrf_k < 1:
            raise ValueError(f"rrf_k must be >= 1, got {self.rrf_k}")
        if not (0.0 <= self.rerank_score_gap_ratio <= 1.0):
            raise ValueError(
                f"rerank_score_gap_ratio must be in [0, 1], got {self.rerank_score_gap_ratio}"
            )
        if self.rerank_min_floor < 0:
            raise ValueError(
                f"rerank_min_floor must be >= 0, got {self.rerank_min_floor}"
            )
        if self.rerank_top_n < 1:
            raise ValueError(f"rerank_top_n must be >= 1, got {self.rerank_top_n}")
        if self.rerank_timeout_sec <= 0:
            raise ValueError(
                f"rerank_timeout_sec must be > 0, got {self.rerank_timeout_sec}"
            )
        if not (1 <= self.watch_workers <= 32):
            raise ValueError(f"watch_workers must be in [1, 32], got {self.watch_workers}")
        if self.watch_debounce_sec <= 0:
            raise ValueError(f"watch_debounce_sec must be > 0, got {self.watch_debounce_sec}")
        if self.watch_queue_capacity < self.watch_workers:
            raise ValueError(
                f"watch_queue_capacity ({self.watch_queue_capacity}) must be >= watch_workers ({self.watch_workers})"
            )


def _apply_yaml(cfg: Config, data: dict[str, Any]) -> None:
    if server := data.get("server"):
        cfg.web_host = server.get("web_host", cfg.web_host)
        cfg.web_port = int(server.get("web_port", cfg.web_port))
    if storage := data.get("storage"):
        if data_dir := storage.get("data_dir"):
            cfg.data_dir = _expand_path(data_dir)
    if embedding := data.get("embedding"):
        cfg.embed_provider = embedding.get("provider", cfg.embed_provider)
        cfg.ollama_url = embedding.get("ollama_url", cfg.ollama_url)
        cfg.ollama_model = embedding.get("ollama_model", cfg.ollama_model)
        cfg.local_model = embedding.get("local_model", cfg.local_model)
        cfg.openai_api_key = embedding.get("openai_api_key", cfg.openai_api_key)
        cfg.openai_model = embedding.get("openai_model", cfg.openai_model)
    if ingest := data.get("ingest"):
        cfg.chunk_size = int(ingest.get("chunk_size", cfg.chunk_size))
        cfg.chunk_overlap = int(ingest.get("chunk_overlap", cfg.chunk_overlap))
        cfg.max_file_size_mb = int(ingest.get("max_file_size_mb", cfg.max_file_size_mb))
        cfg.max_chunks_per_doc = int(
            ingest.get("max_chunks_per_doc", cfg.max_chunks_per_doc)
        )
        cfg.tokenizer_source = ingest.get("tokenizer_source", cfg.tokenizer_source)
        cfg.heading_prefix_enabled = bool(
            ingest.get("heading_prefix_enabled", cfg.heading_prefix_enabled)
        )
        cfg.atomic_blocks_enabled = bool(
            ingest.get("atomic_blocks_enabled", cfg.atomic_blocks_enabled)
        )
        cfg.code_chunker = ingest.get("code_chunker", cfg.code_chunker)
    if crawl := data.get("crawl"):
        cfg.crawl_timeout_sec = int(crawl.get("timeout_sec", cfg.crawl_timeout_sec))
        cfg.max_urls_per_import = int(crawl.get("max_urls_per_import", cfg.max_urls_per_import))
    if search := data.get("search"):
        cfg.default_top_k = int(search.get("default_top_k", cfg.default_top_k))
        cfg.min_score = float(search.get("min_score", cfg.min_score))
        cfg.dedup_enabled = bool(search.get("dedup_enabled", cfg.dedup_enabled))
        cfg.mmr_lambda = float(search.get("mmr_lambda", cfg.mmr_lambda))
        cfg.hybrid_enabled = bool(search.get("hybrid_enabled", cfg.hybrid_enabled))
        cfg.rrf_k = int(search.get("rrf_k", cfg.rrf_k))
        if rerank := search.get("rerank"):
            cfg.rerank_enabled = bool(rerank.get("enabled", cfg.rerank_enabled))
            cfg.rerank_model = str(rerank.get("model", cfg.rerank_model))
            cfg.rerank_top_n = int(rerank.get("top_n", cfg.rerank_top_n))
            cfg.rerank_timeout_sec = float(rerank.get("timeout_sec", cfg.rerank_timeout_sec))
            cfg.rerank_prewarm = bool(rerank.get("prewarm", cfg.rerank_prewarm))
            if gate := rerank.get("gate"):
                cfg.rerank_score_gap_ratio = float(
                    gate.get("score_gap_ratio", cfg.rerank_score_gap_ratio)
                )
                cfg.rerank_min_floor = float(
                    gate.get("min_floor", cfg.rerank_min_floor)
                )
    if watch := data.get("watch"):
        cfg.watch_debounce_sec = float(watch.get("debounce_sec", cfg.watch_debounce_sec))
        cfg.watch_queue_capacity = int(watch.get("queue_capacity", cfg.watch_queue_capacity))
        cfg.watch_workers = int(watch.get("workers", cfg.watch_workers))
        cfg.watch_recovery_interval_sec = int(
            watch.get("recovery_interval_sec", cfg.watch_recovery_interval_sec)
        )
        if extra_text := watch.get("extra_text_exts"):
            cfg.watch_extra_text_exts = list(extra_text)
        if extra_bin := watch.get("extra_binary_exts"):
            cfg.watch_extra_binary_exts = list(extra_bin)


def _apply_env(cfg: Config) -> None:
    if v := os.getenv("DOCGRAPH_DATA_DIR"):
        cfg.data_dir = _expand_path(v)
    if v := os.getenv("DOCGRAPH_WEB_HOST"):
        cfg.web_host = v
    if v := os.getenv("DOCGRAPH_WEB_PORT"):
        cfg.web_port = int(v)
    if v := os.getenv("DOCGRAPH_EMBED_PROVIDER"):
        cfg.embed_provider = v
    if v := os.getenv("DOCGRAPH_OLLAMA_URL"):
        cfg.ollama_url = v
    if v := os.getenv("DOCGRAPH_OLLAMA_EMBED_MODEL"):
        cfg.ollama_model = v
    if v := os.getenv("DOCGRAPH_LOCAL_MODEL"):
        cfg.local_model = v
    if v := os.getenv("DOCGRAPH_OPENAI_API_KEY"):
        cfg.openai_api_key = v
    if v := os.getenv("DOCGRAPH_CHUNK_SIZE"):
        cfg.chunk_size = int(v)
    if v := os.getenv("DOCGRAPH_MAX_FILE_MB"):
        cfg.max_file_size_mb = int(v)
    if v := os.getenv("DOCGRAPH_MAX_CHUNKS_PER_DOC"):
        cfg.max_chunks_per_doc = int(v)
    if v := os.getenv("DOCGRAPH_CRAWL_TIMEOUT_SEC"):
        cfg.crawl_timeout_sec = int(v)
    if v := os.getenv("DOCGRAPH_MAX_URLS_PER_IMPORT"):
        cfg.max_urls_per_import = int(v)
    if v := os.getenv("DOCGRAPH_CHUNK_OVERLAP"):
        cfg.chunk_overlap = int(v)
    if v := os.getenv("DOCGRAPH_TOKENIZER_SOURCE"):
        cfg.tokenizer_source = v
    if v := os.getenv("DOCGRAPH_HEADING_PREFIX"):
        cfg.heading_prefix_enabled = v.lower() in ("1", "true", "yes")
    if v := os.getenv("DOCGRAPH_ATOMIC_BLOCKS"):
        cfg.atomic_blocks_enabled = v.lower() in ("1", "true", "yes")
    if v := os.getenv("DOCGRAPH_CODE_CHUNKER"):
        cfg.code_chunker = v
    if v := os.getenv("DOCGRAPH_DEDUP_ENABLED"):
        cfg.dedup_enabled = v.lower() in ("1", "true", "yes")
    if v := os.getenv("DOCGRAPH_MMR_LAMBDA"):
        cfg.mmr_lambda = float(v)

    def _bool(v: str) -> bool:
        return v.strip().lower() in ("1", "true", "yes", "on")

    if v := os.getenv("DOCGRAPH_HYBRID_ENABLED"):
        cfg.hybrid_enabled = _bool(v)
    if v := os.getenv("DOCGRAPH_RRF_K"):
        cfg.rrf_k = int(v)
    if v := os.getenv("DOCGRAPH_RERANK_ENABLED"):
        cfg.rerank_enabled = _bool(v)
    if v := os.getenv("DOCGRAPH_RERANK_MODEL"):
        cfg.rerank_model = v
    if v := os.getenv("DOCGRAPH_RERANK_TOP_N"):
        cfg.rerank_top_n = int(v)
    if v := os.getenv("DOCGRAPH_RERANK_TIMEOUT_SEC"):
        cfg.rerank_timeout_sec = float(v)
    if v := os.getenv("DOCGRAPH_RERANK_PREWARM"):
        cfg.rerank_prewarm = _bool(v)
    if v := os.getenv("DOCGRAPH_RERANK_SCORE_GAP_RATIO"):
        cfg.rerank_score_gap_ratio = float(v)
    if v := os.getenv("DOCGRAPH_RERANK_MIN_FLOOR"):
        cfg.rerank_min_floor = float(v)
    if v := os.getenv("DOCGRAPH_WATCH_DEBOUNCE_SEC"):
        cfg.watch_debounce_sec = float(v)
    if v := os.getenv("DOCGRAPH_WATCH_QUEUE_CAPACITY"):
        cfg.watch_queue_capacity = int(v)
    if v := os.getenv("DOCGRAPH_WATCH_WORKERS"):
        cfg.watch_workers = int(v)
    if v := os.getenv("DOCGRAPH_WATCH_RECOVERY_INTERVAL_SEC"):
        cfg.watch_recovery_interval_sec = int(v)
    if v := os.getenv("DOCGRAPH_WATCH_EXTRA_TEXT_EXTS"):
        cfg.watch_extra_text_exts = [e.strip() for e in v.split(",") if e.strip()]
    if v := os.getenv("DOCGRAPH_WATCH_EXTRA_BINARY_EXTS"):
        cfg.watch_extra_binary_exts = [e.strip() for e in v.split(",") if e.strip()]


def load_config() -> Config:
    data_dir = _expand_path(os.getenv("DOCGRAPH_DATA_DIR", "~/.docgraph"))
    cfg = Config(data_dir=data_dir)
    yaml_path = cfg.data_dir / "config.yaml"
    if yaml_path.exists():
        with yaml_path.open(encoding="utf-8") as f:
            _apply_yaml(cfg, yaml.safe_load(f) or {})
    _apply_env(cfg)
    cfg.ollama_url = normalize_ollama_url(cfg.ollama_url)
    cfg.validate()
    return cfg
