from __future__ import annotations

import re
from pathlib import Path

from docgraph.config import Config


def sanitize_filename(name: str) -> str:
    base = Path(name).name
    base = re.sub(r"[^\w.\- ]", "_", base)
    base = base.replace(" ", "_")
    return base or "upload"


class FileStore:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    def save_original(self, doc_id: str, filename: str, content: bytes) -> Path:
        safe = sanitize_filename(filename)
        path = self._cfg.originals_dir / f"{doc_id}_{safe}"
        path.write_bytes(content)
        return path

    def save_markdown(self, doc_id: str, markdown: str) -> Path:
        path = self._cfg.markdown_dir / f"{doc_id}.md"
        path.write_text(markdown, encoding="utf-8")
        return path

    def read_markdown(self, doc_id: str) -> str:
        path = self._cfg.markdown_dir / f"{doc_id}.md"
        return path.read_text(encoding="utf-8")

    def delete_doc_files(self, doc_id: str) -> None:
        for p in self._cfg.originals_dir.glob(f"{doc_id}_*"):
            p.unlink(missing_ok=True)
        # NEVER touch paths outside data_dir — watched files are user-owned.
        md = self._cfg.markdown_dir / f"{doc_id}.md"
        md.unlink(missing_ok=True)
