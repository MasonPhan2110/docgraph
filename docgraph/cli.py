import argparse
import asyncio
import logging
import socket
import sys
import threading

import uvicorn

from docgraph.config import load_config
from docgraph.web.app import create_app
from docgraph.web.deps import AppState


def cmd_rebuild_fts(args) -> int:
    """Rebuild the FTS5 index from Chroma. Returns process exit code.

    Side effects: calls SQLiteStore.init_schema() which may migrate the
    SQLite schema on existing databases (idempotent if no migration needed).

    Exit codes:
      0 — success (or nothing to rebuild)
      1 — exception during rebuild
      2 — silent partial (indexed count < total chroma count)
    """
    cfg = load_config()
    cfg.ensure_dirs()
    from docgraph.store import ChromaStore, FtsStore, SQLiteStore

    log = logging.getLogger("docgraph.rebuild_fts")
    sqlite = SQLiteStore(cfg)
    sqlite.init_schema()
    chroma = ChromaStore(cfg)
    fts = FtsStore(cfg)

    print("Counting chunks...")
    # Snapshot of chunk count BEFORE rebuild. If Chroma is written concurrently
    # (e.g., by a parallel ingest), `indexed` may diverge from this and trigger
    # exit code 2. Today DocGraph is single-writer, so the race is theoretical.
    total = chroma.count_chunks()
    print(f"Chroma has {total} chunks")
    if total == 0:
        print("Nothing to rebuild.")
        return 0

    def progress(done, t):
        print(f"  {done}/{t}")

    try:
        indexed = asyncio.run(
            fts.rebuild_from_chroma(chroma, sqlite, progress_callback=progress)
        )
    except Exception as exc:
        log.exception("Rebuild failed")
        print(f"Rebuild failed: {exc}", file=sys.stderr)
        return 1

    if indexed < total:
        print(
            f"Partial rebuild: indexed {indexed} of {total} chunks "
            f"(check Chroma collection consistency).",
            file=sys.stderr,
        )
        return 2

    print(f"Done. FTS index now has {fts.count_chunks()} chunks.")
    return 0


def _port_available(host: str, port: int) -> bool:
    """Return True if the port is free on the given host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _log_startup(cfg, log: logging.Logger, *, mcp_sse: bool) -> None:
    log.info("Web UI at http://%s:%s", cfg.web_host, cfg.web_port)
    if mcp_sse:
        log.info("MCP SSE at http://%s:%s/mcp/sse", cfg.web_host, cfg.web_port)
        log.info(
            "Cursor mcp.json: npx -y mcp-remote@latest http://%s:%s/mcp/sse",
            cfg.web_host,
            cfg.web_port,
        )


def _health_check(cfg, log: logging.Logger) -> None:
    from docgraph.embed.factory import create_embedder

    embedder = create_embedder(cfg)
    try:
        asyncio.run(embedder.health_check())
        log.info("Embedding provider OK (%s)", cfg.embed_provider)
    except Exception as exc:
        log.warning("Embedding health check failed: %s", exc)


def _run_http(cfg) -> None:
    """Run Web UI + MCP SSE on HTTP (server runs independently)."""
    app = create_app(cfg, mount_mcp=True)
    uvicorn.run(
        app,
        host=cfg.web_host,
        port=cfg.web_port,
        log_level="info",
    )


def _run_stdio(cfg) -> None:
    """Legacy: Web UI in background thread + MCP stdio for Cursor direct launch."""
    log = logging.getLogger("docgraph")
    state = AppState.create(cfg)
    app = create_app(cfg, state=state, mount_mcp=False)

    if not _port_available(cfg.web_host, cfg.web_port):
        log.warning(
            "Web UI port %s:%s is already in use; another DocGraph may be running. "
            "MCP stdio will still start, but the Web UI will be unavailable.",
            cfg.web_host,
            cfg.web_port,
        )

    web_thread = threading.Thread(
        target=lambda: uvicorn.run(
            app, host=cfg.web_host, port=cfg.web_port, log_level="warning"
        ),
        daemon=True,
        name="docgraph-web",
    )
    web_thread.start()
    _log_startup(cfg, log, mcp_sse=False)

    from docgraph.mcp.server import create_mcp_server

    mcp = create_mcp_server(state)
    mcp.run(transport="stdio")


def _run_reindex(cfg, *, all_docs: bool, doc_id: str | None, dry_run: bool) -> None:
    from docgraph.models import DocumentStatus
    from docgraph.web.deps import AppState

    state = AppState.create(cfg)
    sqlite = state.sqlite
    if all_docs:
        docs = sqlite.list_documents(status=DocumentStatus.READY)
        ids = [d.id for d in docs]
    elif doc_id:
        ids = [doc_id]
    else:
        raise SystemExit("specify --all or a doc_id")

    log = logging.getLogger("docgraph")
    if dry_run:
        for did in ids:
            log.info("would reindex: %s", did)
        return

    indexer = state.indexer()
    for did in ids:
        log.info("reindexing %s", did)
        asyncio.run(indexer.reindex_document(did))


def _run_stats_chunks(cfg) -> None:
    from docgraph.web.deps import AppState

    state = AppState.create(cfg)
    stats = state.chroma.all_chunk_token_stats()
    if not stats:
        print("No chunks indexed.")
        return
    tokens = sorted(s["tokens"] for s in stats if s["tokens"] > 0)
    if not tokens:
        print(f"{len(stats)} chunks (no token metadata yet — re-index to populate)")
        return
    n = len(tokens)
    mid = tokens[n // 2]
    p95 = tokens[int(n * 0.95)] if n > 1 else tokens[0]
    print(f"chunks: {n}  min: {tokens[0]}  median: {mid}  p95: {p95}  max: {tokens[-1]}")


def _run_serve(stdio: bool) -> None:
    cfg = load_config()
    cfg.ensure_dirs()
    logging.basicConfig(level=logging.INFO)
    log = logging.getLogger("docgraph")

    _health_check(cfg, log)

    if stdio:
        _run_stdio(cfg)
    else:
        _log_startup(cfg, log, mcp_sse=True)
        _run_http(cfg)


def main() -> None:
    parser = argparse.ArgumentParser(prog="docgraph")
    sub = parser.add_subparsers(dest="command")
    serve_parser = sub.add_parser("serve", help="Start Web UI + MCP server")
    serve_parser.add_argument(
        "--stdio",
        action="store_true",
        help="Use MCP stdio instead of HTTP SSE (Cursor launches process directly)",
    )
    sub.add_parser("rebuild-fts", help="Rebuild FTS index from Chroma (one-shot)")
    reindex_parser = sub.add_parser("reindex", help="Re-index documents")
    reindex_parser.add_argument("--all", action="store_true", help="Re-index all READY docs")
    reindex_parser.add_argument("doc_id", nargs="?", help="Single document id")
    reindex_parser.add_argument("--dry-run", action="store_true")
    stats_parser = sub.add_parser("stats", help="Index statistics")
    stats_sub = stats_parser.add_subparsers(dest="stats_command")
    stats_sub.add_parser("chunks", help="Token count distribution for indexed chunks")
    p_watch = sub.add_parser("watch", help="manage file watcher")
    p_watch.add_argument("watch_args", nargs=argparse.REMAINDER, help="<enable|disable|status|list|add|remove|reconcile> [args]")
    args = parser.parse_args()
    if args.command == "serve":
        _run_serve(stdio=args.stdio)
        return
    if args.command == "rebuild-fts":
        sys.exit(cmd_rebuild_fts(args))
    cfg = load_config()
    cfg.ensure_dirs()
    logging.basicConfig(level=logging.INFO)
    if args.command == "reindex":
        _run_reindex(
            cfg,
            all_docs=args.all,
            doc_id=args.doc_id,
            dry_run=args.dry_run,
        )
        return
    if args.command == "stats" and args.stats_command == "chunks":
        _run_stats_chunks(cfg)
        return
    if args.command == "watch":
        from docgraph.cli_watch import run_watch_command
        cfg = load_config()
        base_url = f"http://{cfg.web_host}:{cfg.web_port}"
        sys.exit(run_watch_command(args.watch_args, base_url))
    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
