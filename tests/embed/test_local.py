import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from docgraph.config import Config
from docgraph.embed.local import EMBED_BATCH_SIZE, LocalEmbedder


def _fake_native():
    mod = ModuleType("docgraph_embed")
    mod._inited = False

    def init(model, cache_dir=None):
        mod._inited = True
        mod._model = model
        mod._cache = cache_dir

    def health_check():
        if not mod._inited:
            raise RuntimeError("not initialized")

    def embedding_dimension():
        return 768

    def embed(texts):
        return [[0.1, 0.2, 0.3] for _ in texts]

    mod.init = init
    mod.health_check = health_check
    mod.embedding_dimension = embedding_dimension
    mod.embed = embed
    return mod


@pytest.mark.asyncio
async def test_local_embedder_batches(tmp_data_dir, monkeypatch):
    fake = _fake_native()
    monkeypatch.setitem(sys.modules, "docgraph_embed", fake)

    cfg = Config(data_dir=tmp_data_dir)
    embedder = LocalEmbedder(cfg.local_model, cfg.local_model_dir)
    vecs = await embedder.embed(["a", "b", "c"])
    assert len(vecs) == 3
    assert len(vecs[0]) == 3
    assert fake._inited is True


@pytest.mark.asyncio
async def test_local_health_check(tmp_data_dir, monkeypatch):
    monkeypatch.setitem(sys.modules, "docgraph_embed", _fake_native())
    cfg = Config(data_dir=tmp_data_dir)
    embedder = LocalEmbedder(cfg.local_model, cfg.local_model_dir)
    await embedder.health_check()


@pytest.mark.asyncio
async def test_local_embedder_serializes_native_inference(tmp_data_dir, monkeypatch):
    """Concurrent callers (watcher workers) must not run ONNX inference in parallel:
    each overlapping call multiplies peak activation memory."""
    import asyncio
    import threading
    import time

    fake = _fake_native()
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0, "max_batch": 0}

    def embed(texts):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            state["max_batch"] = max(state["max_batch"], len(texts))
        time.sleep(0.01)
        with lock:
            state["active"] -= 1
        return [[0.1, 0.2, 0.3] for _ in texts]

    fake.embed = embed
    monkeypatch.setitem(sys.modules, "docgraph_embed", fake)

    cfg = Config(data_dir=tmp_data_dir)
    embedder = LocalEmbedder(cfg.local_model, cfg.local_model_dir)
    results = await asyncio.gather(*(embedder.embed(["x"] * 40) for _ in range(4)))

    assert all(len(r) == 40 for r in results)
    assert state["max_active"] == 1
    assert state["max_batch"] <= EMBED_BATCH_SIZE
