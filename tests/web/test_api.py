import io

import pytest
from httpx import ASGITransport, AsyncClient

from docgraph.config import Config
from docgraph.web.app import create_app


@pytest.fixture
def app(tmp_data_dir):
    cfg = Config(data_dir=tmp_data_dir)
    cfg.ensure_dirs()
    return create_app(cfg)


@pytest.mark.asyncio
async def test_health_endpoint(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "status" in body
    assert "mcp_sse_url" in body


def test_mcp_sse_mounted(app):
    mount_paths = [
        getattr(route, "path", "")
        for route in app.routes
        if getattr(route, "path", None)
    ]
    assert "/mcp" in mount_paths


@pytest.mark.asyncio
async def test_list_documents_empty(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/documents")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_upload_document(app):
    transport = ASGITransport(app=app)
    content = b"# Hello\n\nWorld"
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/documents",
            files={"file": ("hello.md", io.BytesIO(content), "text/markdown")},
            data={"folder": "test", "tags": "demo,md"},
        )
    assert resp.status_code == 202
    body = resp.json()
    assert "doc_id" in body
    assert body["status"] == "processing"


@pytest.mark.asyncio
async def test_upload_rejects_oversized(app):
    app.state.docgraph.cfg.max_file_size_mb = 0
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/documents",
            files={"file": ("big.md", io.BytesIO(b"x" * 100), "text/markdown")},
        )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_import_urls(app):
    transport = ASGITransport(app=app)
    body = {
        "urls": "https://example.com/a\nhttps://example.com/b",
        "folder": "web",
        "tags": "crawl",
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/documents/import-urls", json=body)
    assert resp.status_code == 202
    data = resp.json()
    assert data["queued"] == 2
    assert len(data["doc_ids"]) == 2


@pytest.mark.asyncio
async def test_import_urls_rejects_localhost(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/documents/import-urls",
            json={"urls": "http://localhost/secret"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_import_urls_rejects_empty(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/documents/import-urls",
            json={"urls": "  \n# only comments\n"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_search_endpoint_forwards_filters(app, monkeypatch):
    from docgraph.models import SearchResult

    calls = {}

    class FakeSearch:
        async def search(self, query, top_k=None, folder=None, tags=None):
            calls.update(query=query, top_k=top_k, folder=folder, tags=tags)
            return [SearchResult(text="hit", doc_id="d1", filename="a.md", folder=folder,
                                 tags=tags, chunk_index=0, score=0.9)]

    monkeypatch.setattr(app.state.docgraph, "search_service", lambda: FakeSearch())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/search", json={
            "query": "thesis", "tags": ["FPT.VN"], "folder": "tradingagents", "top_k": 7})
    assert resp.status_code == 200
    assert calls == {"query": "thesis", "top_k": 7, "folder": "tradingagents", "tags": ["FPT.VN"]}
    assert resp.json()["results"][0]["doc_id"] == "d1"


@pytest.mark.asyncio
async def test_search_endpoint_rejects_empty_query(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/search", json={"query": "  "})
    assert resp.status_code == 400
