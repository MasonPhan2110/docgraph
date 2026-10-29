import type {
  AddWatchDirRequest,
  Document,
  HealthInfo,
  WatchedDir,
  WatcherStatus,
} from "./types";

async function _errorFromResponse(r: Response): Promise<Error> {
  try {
    const body = await r.json();
    const detail = typeof body.detail === "string" ? body.detail : `HTTP ${r.status}`;
    return new Error(detail);
  } catch {
    return new Error(`HTTP ${r.status}`);
  }
}

export async function fetchHealth(): Promise<HealthInfo> {
  const resp = await fetch("/api/health");
  if (!resp.ok) throw new Error("Health check failed");
  return resp.json();
}

export async function fetchDocuments(): Promise<Document[]> {
  const resp = await fetch("/api/documents");
  if (!resp.ok) throw new Error("Failed to load documents");
  return resp.json();
}

export async function deleteDocument(id: string): Promise<void> {
  const resp = await fetch(`/api/documents/${id}`, { method: "DELETE" });
  if (!resp.ok) throw new Error("Delete failed");
}

export async function reindexDocument(id: string): Promise<void> {
  const resp = await fetch(`/api/documents/${id}/reindex`, { method: "POST" });
  if (!resp.ok) throw new Error("Re-index failed");
}

export interface ImportUrlsResult {
  queued: number;
  doc_ids: string[];
}

export async function importUrls(
  urls: string,
  folder: string,
  tags: string,
): Promise<ImportUrlsResult> {
  const resp = await fetch("/api/documents/import-urls", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ urls, folder, tags }),
  });
  if (!resp.ok) throw await _errorFromResponse(resp);
  return resp.json();
}

export async function fetchWatcherStatus(): Promise<WatcherStatus> {
  const r = await fetch("/api/watch/status");
  if (!r.ok) throw await _errorFromResponse(r);
  return r.json();
}

export async function enableWatcher(): Promise<WatcherStatus> {
  const r = await fetch("/api/watch/enable", { method: "POST" });
  if (!r.ok) throw await _errorFromResponse(r);
  return r.json();
}

export interface DisableWatcherResult {
  enabled: false;
  queue_drained: number;
  queue_dropped: number;
}

export async function disableWatcher(): Promise<DisableWatcherResult> {
  const r = await fetch("/api/watch/disable", { method: "POST" });
  if (!r.ok) throw await _errorFromResponse(r);
  return r.json();
}

export async function fetchWatchedDirs(): Promise<WatchedDir[]> {
  const r = await fetch("/api/watch/dirs");
  if (!r.ok) throw await _errorFromResponse(r);
  const body = await r.json();
  return body.dirs;
}

export interface AddWatchedDirResult {
  id: string;
  path: string;
  scheduled: boolean;
}

export async function addWatchedDir(body: AddWatchDirRequest): Promise<AddWatchedDirResult> {
  const r = await fetch("/api/watch/dirs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw await _errorFromResponse(r);
  return r.json();
}

export interface RemoveWatchedDirResult {
  id: string;
  deleted_docs: number;
  unwatched: boolean;
}

export async function removeWatchedDir(
  wdId: string,
  deleteDocs: boolean,
): Promise<RemoveWatchedDirResult> {
  const r = await fetch(
    `/api/watch/dirs/${wdId}?delete_docs=${deleteDocs}`,
    { method: "DELETE" },
  );
  if (!r.ok) throw await _errorFromResponse(r);
  return r.json();
}

export interface ReconcileResult {
  reconcile_started: boolean;
  dirs: number;
}

export async function triggerReconcile(): Promise<ReconcileResult> {
  const r = await fetch("/api/watch/reconcile", { method: "POST" });
  if (!r.ok) throw await _errorFromResponse(r);
  return r.json();
}
