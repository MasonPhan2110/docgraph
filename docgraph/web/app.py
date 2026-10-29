from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from docgraph.config import Config
from docgraph.ingest.urls import parse_url_lines, url_display_name, validate_url
from docgraph.models import DocumentRecord, DocumentStatus, SourceType
from docgraph.web.deps import AppState

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)


class _WatchDirBody(BaseModel):
    path: str
    folder: str = ""
    tags: str = ""
    ignore_globs: list[str] = []


def _check_watched_path(cfg, path_str: str, existing_dirs: list) -> Path:
    try:
        p = Path(path_str).resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail=f"path does not exist: {path_str}")
    if not p.is_dir():
        raise HTTPException(status_code=400, detail=f"path is not a directory: {path_str}")
    # Inside data_dir?
    data_dir = cfg.data_dir.resolve()
    try:
        p.relative_to(data_dir)
        raise HTTPException(status_code=400, detail="path inside docgraph data_dir, would loop")
    except ValueError:
        pass
    # Reserved roots.
    forbidden = {Path("/"), Path("/etc"), Path("/System"), Path("/Users"), Path.home()}
    if p in forbidden:
        raise HTTPException(status_code=400, detail=f"refusing to watch system path: {p}")
    # Overlap with existing watched dirs (parent or child relationship).
    for wd in existing_dirs:
        wd_path = Path(wd.path).resolve()
        try:
            p.relative_to(wd_path)
            raise HTTPException(status_code=409, detail=f"path overlaps with watched dir {wd.id}")
        except ValueError:
            pass
        try:
            wd_path.relative_to(p)
            raise HTTPException(status_code=409, detail=f"path overlaps with watched dir {wd.id}")
        except ValueError:
            pass
    return p


def _doc_to_json(doc: DocumentRecord) -> dict:
    return {
        "id": doc.id,
        "filename": doc.filename,
        "folder": doc.folder,
        "tags": doc.tags,
        "status": doc.status.value,
        "chunk_count": doc.chunk_count,
        "progress_pct": doc.progress_pct,
        "progress_phase": doc.progress_phase,
        "error_message": doc.error_message,
        "source_type": doc.source_type.value,
        "source_url": doc.source_url,
        "watched_path": doc.watched_path,
    }


class ImportUrlsBody(BaseModel):
    urls: str
    folder: str = ""
    tags: str = ""


async def _run_index(state: AppState, doc_id: str, original_path: Path) -> None:
    # index_document already records ERROR status to SQLite; we log so operators
    # see the traceback rather than only the truncated DB error_message.
    try:
        await state.indexer().index_document(doc_id, original_path)
    except Exception:
        logger.exception("indexing failed for doc_id=%s", doc_id)


async def _run_import_urls(
    state: AppState, items: list[tuple[str, str]]
) -> None:
    try:
        await state.indexer().index_urls_batch(items)
    except Exception:
        logger.exception("URL batch import failed (%d items)", len(items))


def _log_task_exception(task: asyncio.Task) -> None:
    """Done-callback that logs exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Background task failed", exc_info=exc)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    state = app.state.docgraph
    cfg = state.cfg
    # Pre-warm reranker in background
    if cfg.rerank_enabled and cfg.rerank_prewarm and state.reranker is not None:
        t = asyncio.create_task(state.reranker.prewarm())
        t.add_done_callback(_log_task_exception)
    # Auto-rebuild FTS if empty + Chroma populated; warn-only if stale-but-nonempty.
    # NOTE: count_chunks() is potentially I/O-blocking; offloaded so the event loop
    # accepts connections while we read.
    # KNOWN RACE: if a user uploads a document while auto-rebuild runs, the rebuild's
    # internal clear() can wipe the new doc's FTS rows (the new doc was added to Chroma
    # AFTER our snapshot count, so it won't be re-indexed). Window is narrow (startup
    # only when FTS was empty); workaround is to run `docgraph rebuild-fts` after.
    if cfg.hybrid_enabled and state.fts is not None:
        chroma_n = await asyncio.to_thread(state.chroma.count_chunks)
        fts_n = await asyncio.to_thread(state.fts.count_chunks)
        if fts_n == 0 and chroma_n > 0:
            logger.warning(
                "FTS index empty but Chroma has %d chunks. Auto-rebuilding in background.",
                chroma_n,
            )
            t = asyncio.create_task(
                state.fts.rebuild_from_chroma(state.chroma, state.sqlite)
            )
            t.add_done_callback(_log_task_exception)
        # Drift threshold is strict `>`: exactly `max(10, 5%)` drift does NOT warn.
        # 10-chunk floor avoids noise on small corpora (e.g., chroma=20 → 5%=1, floor=10).
        elif chroma_n > 0 and abs(chroma_n - fts_n) > max(10, chroma_n * 0.05):
            logger.warning(
                "FTS index out of sync (chroma=%d, fts=%d). "
                "Run `docgraph rebuild-fts` to fix.",
                chroma_n, fts_n,
            )
    # No indexing task exists yet, so any watched doc still PROCESSING was cut
    # off by a previous crash/kill; release it before reconcile runs.
    n_reset = state.sqlite.reset_interrupted_watched_docs()
    if n_reset:
        logger.warning("watcher: reset %d watched docs interrupted mid-index", n_reset)
    # Auto-enable watcher if persisted state says so.
    persisted = state.sqlite.get_watcher_state("enabled")
    if persisted == "true":
        try:
            await state.watcher.enable()
            logger.info("watcher auto-enabled on startup")
        except Exception:
            logger.exception("watcher auto-enable failed")
    try:
        yield
    finally:
        # Stop watcher on shutdown without persisting "disabled".
        try:
            if state.watcher.state.value == "enabled":
                await state.watcher.disable(persist=False)
        except Exception:
            logger.exception("watcher disable on shutdown failed")


def create_app(
    cfg: Config,
    *,
    state: AppState | None = None,
    mount_mcp: bool = True,
) -> FastAPI:
    if state is None:
        state = AppState.create(cfg)
    app = FastAPI(title="DocGraph", version="2.0.0", lifespan=_lifespan)
    app.state.docgraph = state

    @app.get("/api/health")
    async def health(request: Request):
        st: AppState = request.app.state.docgraph
        cfg = st.cfg
        ollama_ok = True
        ollama_error = ""
        try:
            await st.embedder.health_check()
        except Exception as exc:
            ollama_ok = False
            ollama_error = str(exc)
        mcp_url = f"http://{cfg.web_host}:{cfg.web_port}/mcp/sse"

        # Reranker status: disabled | error | loading | ready
        # - disabled: cfg.rerank_enabled is False (intentional)
        # - error: cfg.rerank_enabled is True but reranker was not constructed (misconfig)
        # - loading: reranker constructed but native model not yet initialized
        # - ready: native model initialized and serving
        if not cfg.rerank_enabled:
            rerank_status = "disabled"
        elif st.reranker is None:
            rerank_status = "error"
        elif st.reranker.is_ready:
            rerank_status = "ready"
        else:
            rerank_status = "loading"

        # FTS sync (count_chunks may block; offload)
        chroma_n = await asyncio.to_thread(st.chroma.count_chunks)
        fts_n = (
            await asyncio.to_thread(st.fts.count_chunks)
            if st.fts is not None else None
        )
        fts_in_sync = (
            None if fts_n is None
            else abs(chroma_n - fts_n) <= max(10, chroma_n * 0.05)
        )

        return {
            "status": "ok",
            "ollama": {"ok": ollama_ok, "error": ollama_error},
            "embed_provider": cfg.embed_provider,
            "mcp_sse_url": mcp_url,
            "hybrid_enabled": cfg.hybrid_enabled,
            "rerank_enabled": cfg.rerank_enabled,
            "rerank_status": rerank_status,
            "chroma_chunks": chroma_n,
            "fts_chunks": fts_n,
            "fts_in_sync": fts_in_sync,
        }

    @app.get("/api/documents")
    async def list_documents(request: Request, folder: str | None = None):
        st: AppState = request.app.state.docgraph
        docs = st.sqlite.list_documents(folder=folder)
        return [_doc_to_json(d) for d in docs]

    @app.post("/api/documents", status_code=202)
    async def upload_document(
        request: Request,
        background_tasks: BackgroundTasks,
        file: UploadFile = File(...),
        folder: str = Form(""),
        tags: str = Form(""),
    ):
        st: AppState = request.app.state.docgraph
        content = await file.read()
        max_bytes = st.cfg.max_file_size_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        if not file.filename:
            raise HTTPException(status_code=415, detail="filename required")

        doc_id = f"doc_{uuid.uuid4().hex[:12]}"
        orig_path = st.files.save_original(doc_id, file.filename, content)
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        doc = DocumentRecord(
            id=doc_id,
            filename=file.filename,
            folder=folder,
            tags=tag_list,
            original_path=str(orig_path),
        )
        st.sqlite.insert_document(doc)
        st.sqlite.update_progress(doc_id, 0, "Queued for indexing (0%)")
        background_tasks.add_task(_run_index, st, doc_id, orig_path)
        return {"doc_id": doc_id, "status": "processing"}

    @app.post("/api/documents/import-urls", status_code=202)
    async def import_urls(
        request: Request,
        body: ImportUrlsBody,
        background_tasks: BackgroundTasks,
    ):
        st: AppState = request.app.state.docgraph
        raw_urls = parse_url_lines(body.urls)
        if not raw_urls:
            raise HTTPException(status_code=400, detail="no urls provided")
        if len(raw_urls) > st.cfg.max_urls_per_import:
            raise HTTPException(
                status_code=400,
                detail=f"too many urls (max {st.cfg.max_urls_per_import})",
            )
        try:
            url_list = [validate_url(u) for u in raw_urls]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        tag_list = [t.strip() for t in body.tags.split(",") if t.strip()]
        doc_ids: list[str] = []
        items: list[tuple[str, str]] = []
        for url in url_list:
            doc_id = f"doc_{uuid.uuid4().hex[:12]}"
            doc = DocumentRecord(
                id=doc_id,
                filename=url_display_name(url),
                folder=body.folder,
                tags=tag_list,
                source_type=SourceType.URL,
                source_url=url,
            )
            st.sqlite.insert_document(doc)
            st.sqlite.update_progress(doc_id, 0, "Queued for crawling (0%)")
            doc_ids.append(doc_id)
            items.append((doc_id, url))

        background_tasks.add_task(_run_import_urls, st, items)
        return {"queued": len(doc_ids), "doc_ids": doc_ids}

    @app.delete("/api/documents/{doc_id}")
    async def delete_document(request: Request, doc_id: str):
        st: AppState = request.app.state.docgraph
        if not await st.delete_doc(doc_id):
            raise HTTPException(status_code=404, detail="not found")
        return {"deleted": doc_id}

    @app.patch("/api/documents/{doc_id}")
    async def update_document(
        request: Request,
        doc_id: str,
        folder: str = Form(""),
        tags: str = Form(""),
    ):
        st: AppState = request.app.state.docgraph
        doc = st.sqlite.get_document(doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="not found")
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        # Update all three stores in lockstep to avoid metadata drift.
        st.sqlite.update_tags_folder(doc_id, tag_list, folder)
        st.chroma.update_doc_metadata(doc_id, folder, tag_list)
        if st.fts is not None:
            st.fts.update_doc_metadata(doc_id, folder, tag_list)
        return {"doc_id": doc_id, "folder": folder, "tags": tag_list}

    @app.post("/api/documents/{doc_id}/reindex", status_code=202)
    async def reindex_document(
        request: Request,
        doc_id: str,
        background_tasks: BackgroundTasks,
    ):
        st: AppState = request.app.state.docgraph
        doc = st.sqlite.get_document(doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="not found")
        if not st.sqlite.claim_for_reindex(doc_id):
            # Already PROCESSING — a prior upload or reindex is still running.
            # Spawning another BG task would race the FTS upsert path (plain
            # INSERT, no unique constraint on chunk_id) and pile up duplicates.
            raise HTTPException(
                status_code=409, detail="document is already being processed"
            )
        background_tasks.add_task(st.indexer().reindex_document, doc_id)
        return {"doc_id": doc_id, "status": "processing"}

    @app.get("/api/watch/status")
    async def watch_status(request: Request):
        st: AppState = request.app.state.docgraph
        w = st.watcher
        observer_running = (
            w._observer is not None and w._observer.is_alive()
        ) if w._observer else False
        return {
            "enabled": w.state.value == "enabled",
            "running": observer_running,
            "dirs_count": len(st.sqlite.list_watched_dirs()),
            "queue_depth": sum(q.qsize() for q in w._queues),
            "queue_capacity": st.cfg.watch_queue_capacity,
            "workers": st.cfg.watch_workers,
            "last_enabled_at": w._last_enabled_at,
            "stats": {
                "events_received": w.stats.events_received,
                "events_debounced": w.stats.events_debounced,
                "events_processed": w.stats.events_processed,
                "events_dropped_queue_full": w.stats.events_dropped_queue_full,
                "reconcile_runs": w.stats.reconcile_runs,
                "last_reconcile_at": w.stats.last_reconcile_at,
            },
        }

    @app.post("/api/watch/enable", status_code=202)
    async def watch_enable(request: Request):
        from docgraph.watch.manager import WatcherTransitionInProgress
        st: AppState = request.app.state.docgraph
        try:
            result = await st.watcher.enable()
        except WatcherTransitionInProgress as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return result

    @app.post("/api/watch/disable")
    async def watch_disable(request: Request):
        from docgraph.watch.manager import WatcherTransitionInProgress
        st: AppState = request.app.state.docgraph
        try:
            result = await st.watcher.disable()
        except WatcherTransitionInProgress as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return result

    @app.get("/api/watch/dirs")
    async def watch_list_dirs(request: Request):
        from datetime import datetime, timezone as _tz
        st: AppState = request.app.state.docgraph
        dirs = st.sqlite.list_watched_dirs()
        out = []
        for wd in dirs:
            docs = st.sqlite.list_watched_docs(prefix=wd.path)
            out.append({
                "id": wd.id,
                "path": wd.path,
                "folder": wd.folder,
                "tags": wd.tags,
                "ignore_globs": wd.ignore_globs,
                "created_at": wd.created_at,
                "doc_count": len(docs),
            })
        return {"dirs": out}

    @app.post("/api/watch/dirs", status_code=201)
    async def watch_add_dir(request: Request, body: _WatchDirBody):
        from datetime import datetime, timezone as _tz
        import uuid as _uuid_mod
        from docgraph.models import WatchedDirRecord
        st: AppState = request.app.state.docgraph
        existing = st.sqlite.list_watched_dirs()
        p = _check_watched_path(st.cfg, body.path, existing)
        tags = [t.strip() for t in body.tags.split(",") if t.strip()]
        wd_id = f"wd_{_uuid_mod.uuid4().hex[:12]}"
        wd = WatchedDirRecord(
            id=wd_id, path=str(p), folder=body.folder, tags=tags,
            ignore_globs=body.ignore_globs,
            created_at=datetime.now(_tz.utc).isoformat(),
        )
        st.sqlite.insert_watched_dir(wd)
        scheduled = False
        if st.watcher.state.value == "enabled":
            from docgraph.watch.ignore import IgnoreMatcher
            st.watcher._ignore_matchers[wd_id] = IgnoreMatcher(wd)
            st.watcher._watched_dirs_cache[wd_id] = wd
            if st.watcher.schedule_dir(wd):
                asyncio.create_task(st.watcher._reconcile_dir(wd))
                scheduled = True
        return {"id": wd_id, "path": wd.path, "scheduled": scheduled}

    @app.delete("/api/watch/dirs/{wd_id}")
    async def watch_remove_dir(request: Request, wd_id: str, delete_docs: bool = False):
        st: AppState = request.app.state.docgraph
        wd = st.sqlite.get_watched_dir(wd_id)
        if wd is None:
            raise HTTPException(status_code=404, detail="watched dir not found")
        deleted = 0
        if delete_docs:
            for doc in st.sqlite.list_watched_docs(prefix=wd.path):
                if await st.delete_doc(doc.id):
                    deleted += 1
        st.sqlite.delete_watched_dir(wd_id)
        # Drop caches first so workers stop matching events even if observer
        # mutation below has a transient gap.
        st.watcher._ignore_matchers.pop(wd_id, None)
        st.watcher._watched_dirs_cache.pop(wd_id, None)
        if st.watcher.state.value == "enabled":
            st.watcher.unschedule_dir(wd_id)
        return {"id": wd_id, "deleted_docs": deleted, "unwatched": True}

    @app.post("/api/watch/reconcile")
    async def watch_reconcile(request: Request):
        st: AppState = request.app.state.docgraph
        if st.watcher.state.value != "enabled":
            raise HTTPException(status_code=409, detail="watcher must be enabled")
        dirs = st.sqlite.list_watched_dirs()
        for wd in dirs:
            asyncio.create_task(st.watcher._reconcile_dir(wd))
        return {"reconcile_started": True, "dirs": len(dirs)}

    if mount_mcp:
        from docgraph.mcp.server import create_mcp_server

        mcp = create_mcp_server(state)
        app.mount("/mcp", mcp.sse_app())

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
