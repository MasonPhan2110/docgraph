import { Fragment, useEffect, useState } from "react";
import { fetchWatchedDirs, removeWatchedDir } from "../api";
import type { WatchedDir } from "../types";
import { Button } from "./ui/Button";

interface WatchedDirsTableProps {
  refreshTick: number;
  onRemoved: () => void;
}

function shortId(id: string): string {
  return id.length > 11 ? id.slice(0, 11) : id;
}

function truncateMiddle(s: string, max = 36): string {
  if (s.length <= max) return s;
  const head = Math.ceil((max - 3) / 2);
  const tail = max - 3 - head;
  return `${s.slice(0, head)}…${s.slice(-tail)}`;
}

export function WatchedDirsTable({ refreshTick, onRemoved }: WatchedDirsTableProps) {
  const [dirs, setDirs] = useState<WatchedDir[]>([]);
  const [loading, setLoading] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [removingId, setRemovingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);  // Clear stale error from any prior failed fetch.
    fetchWatchedDirs()
      .then((d) => {
        if (!cancelled) setDirs(d);
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : "Failed to load watched dirs");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshTick]);

  const handleRemove = async (wdId: string, deleteDocs: boolean) => {
    setRemovingId(wdId);
    setError(null);
    try {
      await removeWatchedDir(wdId, deleteDocs);
      setConfirming(null);
      onRemoved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Remove failed");
    } finally {
      setRemovingId(null);
    }
  };

  if (loading && dirs.length === 0) {
    return (
      <div className="watched-dirs-table">
        <div className="watched-dirs-table__empty">Loading watched directories…</div>
      </div>
    );
  }

  if (dirs.length === 0) {
    return (
      <div className="watched-dirs-table">
        <div className="watched-dirs-table__empty">
          No watched directories yet. Add one below.
        </div>
      </div>
    );
  }

  return (
    <div className="watched-dirs-table">
      <table>
        <thead>
          <tr>
            <th scope="col">ID</th>
            <th scope="col">Path</th>
            <th scope="col">Folder</th>
            <th scope="col">Tags</th>
            <th scope="col">Docs</th>
            <th scope="col">Actions</th>
          </tr>
        </thead>
        <tbody>
          {dirs.map((d) => {
            const isConfirming = confirming === d.id;
            return (
              <Fragment key={d.id}>
                <tr>
                  <td>
                    <span className="watched-dirs-table__id" title={d.id}>
                      {shortId(d.id)}
                    </span>
                  </td>
                  <td>
                    <span className="watched-dirs-table__path" title={d.path}>
                      {truncateMiddle(d.path)}
                    </span>
                  </td>
                  <td>{d.folder || "—"}</td>
                  <td>{d.tags.length > 0 ? d.tags.join(", ") : "—"}</td>
                  <td>{d.doc_count}</td>
                  <td>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={removingId !== null && removingId !== d.id}
                      onClick={() => setConfirming(isConfirming ? null : d.id)}
                    >
                      {isConfirming ? "Cancel" : "Remove"}
                    </Button>
                  </td>
                </tr>
                {isConfirming && (
                  <tr className="watched-dirs-table__confirm-row">
                    <td colSpan={6}>
                      <div className="watched-dirs-table__confirm-text">
                        {d.doc_count > 0 ? (
                          <>
                            ⚠ Remove <code>{d.id}</code> — {d.doc_count}{" "}
                            {d.doc_count === 1 ? "doc is" : "docs are"} currently indexed
                            from this directory. Files on disk are never touched.
                          </>
                        ) : (
                          <>
                            Remove <code>{d.id}</code> — no docs linked yet.
                          </>
                        )}
                      </div>
                      <div className="watched-dirs-table__confirm-actions">
                        {d.doc_count > 0 ? (
                          <>
                            <Button
                              variant="outline"
                              size="sm"
                              disabled={removingId === d.id}
                              onClick={() => void handleRemove(d.id, false)}
                            >
                              {removingId === d.id
                                ? "Removing…"
                                : `Keep ${d.doc_count} ${d.doc_count === 1 ? "doc" : "docs"} in index`}
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              disabled={removingId === d.id}
                              onClick={() => void handleRemove(d.id, true)}
                            >
                              {removingId === d.id
                                ? "Removing…"
                                : `Delete ${d.doc_count} ${d.doc_count === 1 ? "doc" : "docs"} too`}
                            </Button>
                          </>
                        ) : (
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={removingId === d.id}
                            onClick={() => void handleRemove(d.id, false)}
                          >
                            {removingId === d.id ? "Removing…" : "Remove"}
                          </Button>
                        )}
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={removingId === d.id}
                          onClick={() => setConfirming(null)}
                        >
                          Cancel
                        </Button>
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
      {error && (
        <div
          className="watcher-status-bar__error"
          style={{ padding: "0.75rem 1rem" }}
        >
          {error}
        </div>
      )}
    </div>
  );
}
