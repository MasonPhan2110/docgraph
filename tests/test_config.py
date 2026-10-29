import os
from pathlib import Path

import pytest
import yaml

from docgraph.config import Config, load_config, normalize_ollama_url


def test_normalize_ollama_url_localhost():
    assert normalize_ollama_url("http://localhost:11434") == "http://127.0.0.1:11434"


def test_load_config_normalizes_localhost_ollama_url(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        yaml.dump({"embedding": {"ollama_url": "http://localhost:11434"}})
    )
    monkeypatch.setenv("DOCGRAPH_DATA_DIR", str(data_dir))
    cfg = load_config()
    assert cfg.ollama_url == "http://127.0.0.1:11434"


def test_config_defaults(tmp_data_dir):
    cfg = Config(data_dir=tmp_data_dir)
    assert cfg.web_host == "127.0.0.1"
    assert cfg.ollama_url == "http://127.0.0.1:11434"
    assert cfg.web_port == 8088
    assert cfg.embed_provider == "local"
    assert cfg.local_model == "nomic-embed-text"
    assert cfg.chunk_size == 512
    assert cfg.default_top_k == 5


def test_load_config_from_yaml(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg_file = data_dir / "config.yaml"
    cfg_file.write_text(yaml.dump({
        "server": {"web_port": 9090},
        "embedding": {"provider": "openai", "openai_model": "text-embedding-3-small"},
    }))
    monkeypatch.setenv("DOCGRAPH_DATA_DIR", str(data_dir))
    cfg = load_config()
    assert cfg.web_port == 9090
    assert cfg.embed_provider == "openai"
    assert cfg.openai_model == "text-embedding-3-small"


def test_env_overrides_yaml(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(yaml.dump({"server": {"web_port": 9090}}))
    monkeypatch.setenv("DOCGRAPH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DOCGRAPH_WEB_PORT", "7777")
    cfg = load_config()
    assert cfg.web_port == 7777


class TestRerankConfig:
    def test_defaults(self, tmp_path):
        cfg = Config(data_dir=tmp_path)
        assert cfg.hybrid_enabled is True
        assert cfg.rrf_k == 60
        assert cfg.rerank_enabled is True
        assert cfg.rerank_model == "bge-reranker-v2-m3"
        assert cfg.rerank_top_n == 4
        assert cfg.rerank_timeout_sec == 15.0
        assert cfg.rerank_prewarm is True
        assert cfg.rerank_score_gap_ratio == 0.5
        assert cfg.rerank_min_floor == 0.015

    def test_validate_negative_rrf_k_raises(self, tmp_path):
        cfg = Config(data_dir=tmp_path, rrf_k=-1)
        with pytest.raises(ValueError, match="rrf_k must be >= 1"):
            cfg.validate()

    def test_validate_gap_ratio_out_of_range(self, tmp_path):
        cfg = Config(data_dir=tmp_path, rerank_score_gap_ratio=1.5)
        with pytest.raises(ValueError, match="rerank_score_gap_ratio"):
            cfg.validate()

    def test_validate_negative_floor_raises(self, tmp_path):
        cfg = Config(data_dir=tmp_path, rerank_min_floor=-0.01)
        with pytest.raises(ValueError, match="rerank_min_floor"):
            cfg.validate()

    def test_validate_top_n_must_be_positive(self, tmp_path):
        cfg = Config(data_dir=tmp_path, rerank_top_n=0)
        with pytest.raises(ValueError, match="rerank_top_n"):
            cfg.validate()

    def test_validate_timeout_must_be_positive(self, tmp_path):
        cfg = Config(data_dir=tmp_path, rerank_timeout_sec=0.0)
        with pytest.raises(ValueError, match="rerank_timeout_sec"):
            cfg.validate()

    def test_validate_coerces_prewarm_when_disabled(self, tmp_path):
        cfg = Config(data_dir=tmp_path, rerank_enabled=False, rerank_prewarm=True)
        cfg.validate()
        assert cfg.rerank_prewarm is False

    def test_env_var_overrides(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DOCGRAPH_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DOCGRAPH_HYBRID_ENABLED", "false")
        monkeypatch.setenv("DOCGRAPH_RRF_K", "30")
        monkeypatch.setenv("DOCGRAPH_RERANK_ENABLED", "false")
        monkeypatch.setenv("DOCGRAPH_RERANK_MODEL", "bge-reranker-base")
        monkeypatch.setenv("DOCGRAPH_RERANK_TOP_N", "10")
        monkeypatch.setenv("DOCGRAPH_RERANK_TIMEOUT_SEC", "5.0")
        monkeypatch.setenv("DOCGRAPH_RERANK_PREWARM", "false")
        monkeypatch.setenv("DOCGRAPH_RERANK_SCORE_GAP_RATIO", "0.7")
        monkeypatch.setenv("DOCGRAPH_RERANK_MIN_FLOOR", "0.02")
        cfg = load_config()
        assert cfg.hybrid_enabled is False
        assert cfg.rrf_k == 30
        assert cfg.rerank_enabled is False
        assert cfg.rerank_model == "bge-reranker-base"
        assert cfg.rerank_top_n == 10
        assert cfg.rerank_timeout_sec == 5.0
        assert cfg.rerank_prewarm is False
        assert cfg.rerank_score_gap_ratio == 0.7
        assert cfg.rerank_min_floor == 0.02


def test_watcher_defaults():
    cfg = Config(data_dir=Path("/tmp"))
    assert cfg.watch_debounce_sec == 2.0
    assert cfg.watch_queue_capacity == 500
    assert cfg.watch_workers == 2
    assert cfg.watch_recovery_interval_sec == 600
    assert cfg.watch_extra_text_exts == []
    assert cfg.watch_extra_binary_exts == []


def test_watcher_env_overrides(monkeypatch):
    monkeypatch.setenv("DOCGRAPH_DATA_DIR", "/tmp")
    monkeypatch.setenv("DOCGRAPH_WATCH_DEBOUNCE_SEC", "1.5")
    monkeypatch.setenv("DOCGRAPH_WATCH_QUEUE_CAPACITY", "200")
    monkeypatch.setenv("DOCGRAPH_WATCH_WORKERS", "3")
    monkeypatch.setenv("DOCGRAPH_WATCH_RECOVERY_INTERVAL_SEC", "300")
    monkeypatch.setenv("DOCGRAPH_WATCH_EXTRA_TEXT_EXTS", ".tf,.hcl")
    monkeypatch.setenv("DOCGRAPH_WATCH_EXTRA_BINARY_EXTS", ".keynote")
    cfg = load_config()
    assert cfg.watch_debounce_sec == 1.5
    assert cfg.watch_queue_capacity == 200
    assert cfg.watch_workers == 3
    assert cfg.watch_recovery_interval_sec == 300
    assert cfg.watch_extra_text_exts == [".tf", ".hcl"]
    assert cfg.watch_extra_binary_exts == [".keynote"]


def test_watcher_workers_validation():
    cfg = Config(data_dir=Path("/tmp"))
    cfg.watch_workers = 0
    with pytest.raises(ValueError, match="watch_workers"):
        cfg.validate()
    cfg.watch_workers = 33
    with pytest.raises(ValueError, match="watch_workers"):
        cfg.validate()
