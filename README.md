# DocGraph

Local document RAG server for Cursor via MCP. Upload files or import URLs, index them with a **local Rust embedder** (ONNX, no Ollama or cloud key required), and query them from Cursor through the `/document` skill.

- **Web UI** — upload files, import URLs, and manage documents
- **MCP server** — `search_documents`, `list_documents`, `get_document` exposed to Cursor
- **Local embeddings** — fast ONNX embedder written in Rust; nothing leaves your machine

---

## Quick start with Docker (recommended)

No need to install Python, Poetry, Rust, or Node — just run Docker.

```bash
docker compose up --build
```

Or with plain Docker:

```bash
docker build -t docgraph .
docker run -p 8088:8088 -v docgraph-data:/data docgraph
```

Then open:

- Web UI: http://127.0.0.1:8088
- MCP SSE: http://127.0.0.1:8088/mcp/sse

Notes:

- The **first build** is slow (it compiles the Rust embedder and downloads dependencies); later builds are cached.
- The **first run** downloads the ONNX model (~100MB) into the `/data` volume; later runs reuse it.
- All data (sqlite db, Chroma index, uploaded files, model cache) is persisted in the `docgraph-data` volume, so it survives restarts.
- For non-English documents, set `DOCGRAPH_LOCAL_MODEL=multilingual-e5-base` (uncomment it in `docker-compose.yml`) and re-index.

---

## Manual install (for development)

### Prerequisites

- Python 3.10–3.13
- [Poetry](https://python-poetry.org/)
- [Rust toolchain](https://rustup.rs/) + [maturin](https://www.maturin.rs/) (for local embeddings)
- Optional: [Ollama](https://ollama.com/) if `DOCGRAPH_EMBED_PROVIDER=ollama`

### Install

```bash
poetry install
pip install maturin
cd crates/docgraph-embed && maturin develop --release && cd ../..
```

Poetry-only setup (no global `pip install`):

```bash
poetry install
poetry add --group dev maturin
poetry run maturin develop --release -m crates/docgraph-embed/Cargo.toml
```

First run downloads the ONNX model (~100MB) to `~/.docgraph/models`.

#### Windows troubleshooting: `maturin develop` cannot overwrite module in use

If `maturin develop` fails with a message about `pip could not overwrite the installed extension module` and a leftover directory like `~ocgraph_embed`, another Python process is still holding the old compiled extension.

1. Stop all Python processes that may have imported `docgraph_embed` (for example, old `docgraph serve` terminals).
2. Delete the leftover directory in the Poetry venv site-packages (for example, `.../Lib/site-packages/~ocgraph_embed`).
3. Re-run:

```bash
poetry run maturin develop --release -m crates/docgraph-embed/Cargo.toml
```

### Optional extras

```bash
poetry install -E openai   # cloud embedding instead of local/ollama
poetry install -E crawl    # import URLs via crawl4ai
```

For URL import, install the crawl extra and set up the browser:

```bash
poetry install -E crawl
poetry run crawl4ai-setup
# or: python -m playwright install chromium
```

### Run

```bash
poetry run docgraph serve
```

- Web UI: http://127.0.0.1:8088 (React — upload files, import URLs, manage documents)
- MCP SSE: http://127.0.0.1:8088/mcp/sse

Keep this terminal running while using Cursor.

### Web UI development (React + Vite)

The UI uses a **Minimalist Monochrome** design system (Playfair Display, Source Serif 4, JetBrains Mono; pure black/white; sharp corners; line-based progress).

```bash
cd frontend
npm install
npm run dev          # http://127.0.0.1:5173 — proxies /api → :8088
npm run build        # output → docgraph/web/static/
```

Design tokens live in `frontend/src/styles/`. Run `npm run build` after UI changes.

---

## Cursor MCP configuration

Add to your Cursor MCP settings (`Settings → MCP` or `.cursor/mcp.json`).

**Recommended — run the server separately, connect via HTTP** (start the server, via Docker or `docgraph serve`, first):

```json
{
  "mcpServers": {
    "docgraph": {
      "command": "npx",
      "args": ["-y", "mcp-remote@latest", "http://127.0.0.1:8088/mcp/sse"]
    }
  }
}
```

**Alternative — let Cursor launch the process (stdio):**

```json
{
  "mcpServers": {
    "docgraph": {
      "command": "poetry",
      "args": ["run", "docgraph", "serve", "--stdio"],
      "cwd": "/path/to/docgraph"
    }
  }
}
```

Replace `/path/to/docgraph` with the absolute path to this repository on your machine.

---

## `/document` skill

Copy `skills/document/SKILL.md` to your Cursor skills directory:

- Global: `~/.cursor/skills/document/SKILL.md`
- Project: `.cursor/skills/document/SKILL.md`

Then in Cursor chat:

```
/document How do I configure the embedding provider?
/document --folder docs --tag v2 What is DocGraph v2?
```

---

## MCP tools

| Tool | Description |
|------|-------------|
| `search_documents` | Semantic search over indexed documents |
| `list_documents` | List documents with optional filters |
| `get_document` | Get full converted markdown for a document |

---

## Configuration

| Env Variable | Default | Description |
|---|---|---|
| `DOCGRAPH_DATA_DIR` | `~/.docgraph` (`/data` in Docker) | Data directory |
| `DOCGRAPH_WEB_HOST` | `127.0.0.1` (`0.0.0.0` in Docker) | Bind address for the Web UI / MCP server |
| `DOCGRAPH_WEB_PORT` | `8088` | Web UI port |
| `DOCGRAPH_EMBED_PROVIDER` | `local` | `local`, `ollama`, or `openai` |
| `DOCGRAPH_LOCAL_MODEL` | `nomic-embed-text` | Local ONNX (English, 768-dim). For multilingual: `multilingual-e5-base` |
| `DOCGRAPH_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama endpoint when provider=ollama |
| `DOCGRAPH_OLLAMA_EMBED_MODEL` | `nomic-embed-text:latest` | Ollama embedding model |
| `DOCGRAPH_CRAWL_TIMEOUT_SEC` | `30` | Per-URL crawl timeout |
| `DOCGRAPH_MAX_URLS_PER_IMPORT` | `50` | Max URLs per import batch |
| `DOCGRAPH_MAX_CHUNKS_PER_DOC` | `5000` | Hard cap on chunks per document (oversize → ERROR) |
| `DOCGRAPH_TOKENIZER_SOURCE` | `auto` | `auto`, `char-ratio`, or `tiktoken` |
| `DOCGRAPH_HEADING_PREFIX` | `true` | Prepend heading breadcrumb to markdown chunks |
| `DOCGRAPH_ATOMIC_BLOCKS` | `true` | Keep code fences and tables atomic when chunking |
| `DOCGRAPH_CODE_CHUNKER` | `regex` | `regex` or `treesitter` (requires `poetry install -E treesitter`) |
| `DOCGRAPH_DEDUP_ENABLED` | `true` | Skip duplicate chunk text within a document |
| `DOCGRAPH_MMR_LAMBDA` | `0.7` | MMR diversity (1.0 = pure relevance) |
| `DOCGRAPH_HYBRID_ENABLED` | `true` | Enable hybrid search (vector + BM25/FTS5) |
| `DOCGRAPH_RRF_K` | `60` | Reciprocal Rank Fusion constant |
| `DOCGRAPH_RERANK_ENABLED` | `true` | Enable cross-encoder reranker (`poetry install -E rerank`) |
| `DOCGRAPH_RERANK_MODEL` | `bge-reranker-v2-m3` | Reranker model (fastembed) |
| `DOCGRAPH_RERANK_TOP_N` | `4` | Number of candidates passed to the reranker. Each candidate ≈ 700ms cross-encoder forward pass on CPU — see `benchmarks/README.md` |
| `DOCGRAPH_RERANK_TIMEOUT_SEC` | `3.0` | Per-call rerank timeout (falls back to RRF order on timeout) |
| `DOCGRAPH_RERANK_PREWARM` | `true` | Warm up reranker model at server startup |
| `DOCGRAPH_RERANK_SCORE_GAP_RATIO` | `0.5` | Skip rerank when top-1 RRF dominates by this ratio |
| `DOCGRAPH_RERANK_MIN_FLOOR` | `0.015` | Skip rerank when top-1 RRF score is below this floor |

## File watcher (roadmap 3.1)

Auto-indexes files in user-configured directories. Runtime-toggleable via Web UI, CLI, or HTTP API — no restart needed.

### Quick start — Web UI

Open `http://127.0.0.1:8088`, switch to the **Folder watch** tab (4th tab in the Ingest section):

1. Fill in the **Add directory** form (path, folder, tags, optional ignore globs) → submit. Form rejects non-existent paths inline.
2. Click **Enable watcher** in the status bar.
3. Watched docs appear in the Documents table with a `[WATCHED]` badge. Hover the badge to see the full source path.
4. Click **Reconcile** any time you suspect drift (e.g. after a batch edit on disk) — forces a disk-vs-DB delta scan.
5. **Remove** a watched dir inline; choose to keep the indexed docs as orphans or delete them too.

The status bar polls `/api/watch/status` every 2s while enabled, 5s while disabled.

### Quick start — CLI

```bash
# Add a watched directory (server must be running)
docgraph watch add ~/Notes --folder notes --tags personal

# Turn the watcher on
docgraph watch enable

# Check what it's doing
docgraph watch status

# Turn it off (paths persist; re-enable later picks them up)
docgraph watch disable
```

### How it works

- **Text files** (`.md`, `.py`, `.rs`, `.json`, etc.) are referenced in place — no duplicate copy in `data_dir`.
- **Binary files** (`.pdf`, `.docx`, `.pptx`, `.xlsx`, …) are copied into `data_dir/files/originals/` as a snapshot at index time; the converted markdown is cached and regenerated only when the source file's mtime changes.
- **Unknown extensions** are skipped silently. Extend via `DOCGRAPH_WATCH_EXTRA_TEXT_EXTS` / `DOCGRAPH_WATCH_EXTRA_BINARY_EXTS`.

### Ignore patterns

Three layers compose:

1. **Hardcoded defaults** — `.git`, `node_modules`, `__pycache__`, `.venv`, `.DS_Store`, `*.pyc`, `*.swp`, `*~`, etc.
2. **`.docgraphignore`** at each watched-dir root — gitignore syntax (minus negation). Reloaded on its own mtime.
3. **Per-dir `ignore_globs`** from the add-dir API/CLI.

### Watched vs uploaded

A file at the same path can exist as **two separate docs** if you both upload it via `POST /api/documents` and watch its directory. They have independent lifecycles. Watcher docs use `source_type=watched`; upload docs use `source_type=file`.

### Config knobs

| Env var | YAML key | Default | Purpose |
|---|---|---:|---|
| `DOCGRAPH_WATCH_DEBOUNCE_SEC` | `watch.debounce_sec` | `2.0` | Per-path debounce window before enqueueing |
| `DOCGRAPH_WATCH_QUEUE_CAPACITY` | `watch.queue_capacity` | `500` | Aggregate cap across per-worker queues |
| `DOCGRAPH_WATCH_WORKERS` | `watch.workers` | `2` | Number of async ingest workers (embedding is serialized regardless; more workers only overlap file I/O + conversion) |
| `DOCGRAPH_WATCH_RECOVERY_INTERVAL_SEC` | `watch.recovery_interval_sec` | `600` | Periodic reconcile (fsevents-drop backstop) |
| `DOCGRAPH_WATCH_EXTRA_TEXT_EXTS` | `watch.extra_text_exts` | `[]` | Extra extensions to treat as native text |
| `DOCGRAPH_WATCH_EXTRA_BINARY_EXTS` | `watch.extra_binary_exts` | `[]` | Extra extensions to convert as binary |

### HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/watch/status` | Snapshot of watcher state, stats, queue depth |
| `POST` | `/api/watch/enable` | Start observer + workers, run reconcile |
| `POST` | `/api/watch/disable` | Stop observer, cancel workers, drain queue |
| `GET`  | `/api/watch/dirs` | List watched dirs (with per-dir doc count) |
| `POST` | `/api/watch/dirs` | Add a dir: body `{path, folder, tags, ignore_globs}` |
| `DELETE` | `/api/watch/dirs/{wd_id}` | Remove a dir (optional `?delete_docs=true`) |
| `POST` | `/api/watch/reconcile` | Manual reconcile across all dirs (watcher must be enabled) |

### Inside Docker

Docker volumes are declared at container creation, so the watcher inside the container can only see directories that have been bind-mounted into it. The easiest pattern is to mount your home directory read-only once, then add any subfolder runtime without restarting the container:

```yaml
# docker-compose.yml
volumes:
  - docgraph-data:/data
  - ${HOME}:/host:ro      # entire home, read-only
```

`docker compose up -d`, then in the Web UI's **Folder watch** tab, add paths under `/host/...`:

| Host path | In-container path you type in the form |
|---|---|
| `~/Notes` | `/host/Notes` |
| `~/Projects/myapp` | `/host/Projects/myapp` |

Read-only mounts are safe — watcher only reads files (spec §8.7 forbids writing user-owned paths). State (watched_dirs, docs, indexes) persists in `docgraph-data`, so container restarts are state-preserving.

For a stricter setup, mount specific folders individually:

```yaml
volumes:
  - docgraph-data:/data
  - ${HOME}/Notes:/watched/notes:ro
  - ${HOME}/Projects/docgraph:/watched/code:ro
```

Then use `/watched/notes` etc. in the form. Trade-off: adding a new path later requires editing compose + `docker compose up -d`.

On macOS / Windows Docker Desktop, fsevents propagation through the VirtIOFS/gRPC FUSE layer can sometimes lag. The 10-minute recovery reconcile (config knob `DOCGRAPH_WATCH_RECOVERY_INTERVAL_SEC`) is the backstop; you can also click **Reconcile** in the UI for an immediate sync.

### Known limitations

- Symlinks are not followed (would risk indexing loops). Documented.
- macOS fsevents can drop events under burst — the 10-minute recovery reconcile backstops this.
- Watcher does not auth its endpoints — they inherit whatever auth ships when roadmap 3.4 lands.
- Docker bind mounts are static — adding a new mount path (when not using the `${HOME}:/host:ro` pattern) requires `docker compose up -d` to recreate the container.

Re-index after changing chunk settings:

```bash
poetry run docgraph reindex --all
# or inside Docker:
docker compose exec docgraph docgraph reindex --all
```

See the [chunker improvements spec](docs/superpowers/specs/2026-06-03-chunker-improvements-design.md).

---

## Troubleshooting

**"Hybrid search returns vector-only results"**
The FTS5 index hasn't been populated (e.g., after upgrading from a pre-hybrid version).
- Automatic: server startup detects an empty FTS index and rebuilds in the background.
- Manual: `poetry run docgraph rebuild-fts`.

**"Reranker reports disabled / error"**
Check `GET /api/health` field `rerank_status`:
- `error` — reranker construction failed. Rebuild the Rust crate inside the Poetry venv:
  ```bash
  env -u CONDA_PREFIX -u CONDA_DEFAULT_ENV -u CONDA_SHLVL -u CONDA_PROMPT_MODIFIER \
    poetry run maturin develop --release \
    --manifest-path crates/docgraph-embed/Cargo.toml
  ```
- `loading` — wait 5–10s; the BGE model is downloading on first use (~600MB).
- `disabled` — set `DOCGRAPH_RERANK_ENABLED=true` or enable it in the YAML config.

**"First search after startup is slow (~10s)"**
Reranker cold-start. `rerank_prewarm=true` (default) warms the model in the background at server start; if the first query is still slow, check the server log for prewarm errors.

## Test

```bash
poetry run pytest tests/ -v
poetry run pytest tests/ -m integration -v   # optional: live embedder / Ollama
```
