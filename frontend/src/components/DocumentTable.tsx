import { useState } from "react";
import { deleteDocument, reindexDocument } from "../api";
import type { Document } from "../types";
import { StatusCell } from "./StatusCell";
import { Button } from "./ui/Button";

interface DocumentTableProps {
  documents: Document[];
  loading: boolean;
  onChanged: () => void;
}

export function DocumentTable({
  documents,
  loading,
  onChanged,
}: DocumentTableProps) {
  const [actingId, setActingId] = useState<string | null>(null);

  const runAction = async (id: string, fn: () => Promise<void>) => {
    setActingId(id);
    try {
      await fn();
      onChanged();
    } finally {
      setActingId(null);
    }
  };

  return (
    <section className="section" aria-labelledby="docs-heading">
      <div className="section-head">
        <div>
          <p className="label-mono" id="docs-heading">
            03 — Library
          </p>
          <h2 className="section-title">Documents</h2>
        </div>
        {loading && <span className="muted">Refreshing</span>}
      </div>

      {documents.length === 0 ? (
        <p className="empty-state">
          No documents yet. Upload a file, import web pages, or add a code dump
          above to build your knowledge base.
        </p>
      ) : (
        <div className="table-panel">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">File</th>
                  <th scope="col">Folder</th>
                  <th scope="col">Tags</th>
                  <th scope="col">Status</th>
                  <th scope="col">Chunks</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {documents.map((d) => (
                  <tr key={d.id}>
                    <td className="filename-cell">
                      <span title={d.watched_path || d.source_url || d.filename}>{d.filename}</span>
                      {d.source_type === "watched" && (
                        <span
                          className="badge badge-watched label-mono"
                          title={d.watched_path || undefined}
                        >
                          WATCHED
                        </span>
                      )}
                    </td>
                    <td>{d.folder || "—"}</td>
                    <td>{(d.tags || []).join(", ") || "—"}</td>
                    <td>
                      <StatusCell doc={d} />
                    </td>
                    <td className="num-cell">
                      {d.status === "processing" ? "—" : d.chunk_count}
                    </td>
                    <td className="actions-cell">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={actingId === d.id}
                        onClick={() =>
                          runAction(d.id, () => reindexDocument(d.id))
                        }
                      >
                        Re-index
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={actingId === d.id}
                        onClick={() =>
                          runAction(d.id, () => deleteDocument(d.id))
                        }
                      >
                        Delete
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}
