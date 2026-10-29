# syntax=docker/dockerfile:1

# ──────────────────────────────────────────────────────────────
# Stage 1: build React UI -> docgraph/web/static
# ──────────────────────────────────────────────────────────────
FROM node:20-bookworm-slim AS frontend
WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install

COPY frontend/ ./
# vite outDir resolves to /app/docgraph/web/static (created automatically)
RUN npm run build


# ──────────────────────────────────────────────────────────────
# Stage 2: build Rust embedder (powers embed + reranker via
# fastembed-rs) + install Python deps (watchdog/pathspec for the
# file watcher pull in via poetry) into a venv
# ──────────────────────────────────────────────────────────────
FROM python:3.12-bookworm AS builder

ENV POETRY_VERSION=2.2.1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH=/usr/local/cargo/bin:$PATH

# Rust toolchain (for the maturin/PyO3 embedder) + build essentials
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl pkg-config libssl-dev \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal \
    && rm -rf /var/lib/apt/lists/*

RUN pip install "poetry==${POETRY_VERSION}"

WORKDIR /app

# 1) Install Python deps first (cached unless lockfile/manifest changes)
COPY pyproject.toml poetry.lock README.md ./
COPY docgraph/__init__.py ./docgraph/__init__.py
RUN poetry install --only main --no-root

# 2) Build the Rust embedder into the venv
COPY crates/ ./crates/
RUN poetry run pip install maturin \
    && poetry run maturin develop --release -m crates/docgraph-embed/Cargo.toml

# 3) Copy app source + the built frontend, then install the package itself
COPY docgraph/ ./docgraph/
COPY --from=frontend /app/docgraph/web/static ./docgraph/web/static
RUN poetry install --only main


# ──────────────────────────────────────────────────────────────
# Stage 3: slim runtime image
# ──────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

# Runtime libs needed by native wheels (pymupdf, onnxruntime, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DOCGRAPH_DATA_DIR=/data \
    DOCGRAPH_WEB_HOST=0.0.0.0 \
    DOCGRAPH_WEB_PORT=8088 \
    # Reranker model loads lazily on first /search rerank call; cached in
    # /data/models (volume) so subsequent boots reuse the ONNX file.
    DOCGRAPH_RERANK_PREWARM=false

# curl needed for HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv ./.venv
COPY --from=builder /app/docgraph ./docgraph
COPY pyproject.toml README.md ./

# Persisted data: sqlite db, chroma index, uploaded files, ONNX model cache
# (embed + reranker share /data/models). Mount additional host folders
# read-only into /watched/* if you want the watcher to track them — see
# docker-compose.yml for the pattern.
VOLUME ["/data"]
EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8088/api/health || exit 1

CMD ["docgraph", "serve"]
