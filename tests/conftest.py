import pytest


@pytest.fixture
def tmp_data_dir(tmp_path):
    return tmp_path / "docgraph_data"


@pytest.fixture(autouse=True)
def _disable_rerank_prewarm_for_tests(monkeypatch):
    """Skip the BGE reranker model load during TestClient lifespan.

    The default `cfg.rerank_prewarm=True` spawns a background task that
    downloads/loads the ONNX model, which can hang TestClient cleanup.
    Tests that need a real reranker can still opt in explicitly.
    """
    monkeypatch.setenv("DOCGRAPH_RERANK_PREWARM", "false")
