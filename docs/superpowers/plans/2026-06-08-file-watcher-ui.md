# File Watcher UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the React UI for the file watcher backend: a 4th "Folder watch" tab in the Ingest section with full CRUD over watched directories, plus a per-doc `[WATCHED]` badge in the documents table.

**Architecture:** New composer `FolderWatchSection` with three children (`WatcherStatusBar` polls `/api/watch/status`, `WatchedDirsTable` lists + removes dirs, `AddWatchDirForm` creates dirs). Each child owns its data + loading + error state. Parent passes a `refreshTick` counter and children re-fetch on its change. No new dependencies, no test harness — v1 ships with a manual smoke-test checklist.

**Tech Stack:** React 19 + TypeScript + Vite 6 + plain CSS with design tokens. Backend on `feat/file-watcher` branch (HEAD `6b378d8`) provides 7 watcher endpoints under `/api/watch/*` plus extended `Document.source_type` + `watched_path` fields.

**Spec reference:** `docs/superpowers/specs/2026-06-08-file-watcher-ui-design.md`

---

## File Structure

**Created:**
- `frontend/src/components/FolderWatchSection.tsx` — composer (~50 LOC)
- `frontend/src/components/WatcherStatusBar.tsx` — status pill + Enable/Disable + Reconcile buttons (~120 LOC)
- `frontend/src/components/WatchedDirsTable.tsx` — table + inline remove confirmation (~150 LOC)
- `frontend/src/components/AddWatchDirForm.tsx` — path/folder/tags/ignore inputs (~100 LOC)

**Modified:**
- `frontend/src/types.ts` — extend `Document.source_type` + add `WatcherStatus`, `WatcherStats`, `WatchedDir`, `AddWatchDirRequest`
- `frontend/src/api.ts` — add 7 watcher fetch functions + `_errorFromResponse` helper, refactor `importUrls` to use it
- `frontend/src/components/IngestTabs.tsx` — add 4th tab `"watch"` rendering `<FolderWatchSection>`
- `frontend/src/components/DocumentTable.tsx` — render `[WATCHED]` badge inside the filename cell
- `frontend/src/styles/components.css` — `.badge-watched`, `.watcher-status-bar`, `.watched-dirs-table`, `.add-watch-dir-form` styles

---

## Execution Order Notes

Tasks 1-2 (types + api.ts) are pure foundation — every later task imports from them. Tasks 3-4 (badge + CSS) are independent of the new components. Tasks 5-8 build the four new components in dependency order (status bar → dirs table → form → composer). Task 9 wires the composer into IngestTabs. Task 10 is build verification + smoke test on a running server.

Do NOT skip ahead — each task ends with a successful `npm run build` (type-check + bundle).

---

## Task 1: Types extension

**Files:**
- Modify: `frontend/src/types.ts`

- [ ] **Step 1: Replace `Document` and append new interfaces**

Open `frontend/src/types.ts` and replace its full content with:

```typescript
export type DocStatus = "processing" | "ready" | "error";

export interface Document {
  id: string;
  filename: string;
  folder: string;
  tags: string[];
  status: DocStatus;
  chunk_count: number;
  progress_pct: number;
  progress_phase: string;
  error_message: string | null;
  source_type?: "file" | "url" | "watched";
  source_url?: string;
  watched_path?: string;
}

export interface HealthInfo {
  status: string;
  ollama: { ok: boolean; error: string };
  embed_provider: string;
  mcp_sse_url: string;
}

export interface UploadProgressState {
  visible: boolean;
  percent: number;
  label: string;
  detail: string;
  failed: boolean;
}

export interface WatcherStats {
  events_received: number;
  events_debounced: number;
  events_processed: number;
  events_dropped_queue_full: number;
  reconcile_runs: number;
  last_reconcile_at: string | null;
}

export interface WatcherStatus {
  enabled: boolean;
  running: boolean;
  dirs_count: number;
  queue_depth: number;
  queue_capacity: number;
  workers: number;
  last_enabled_at: string | null;
  stats: WatcherStats;
}

export interface WatchedDir {
  id: string;
  path: string;
  folder: string;
  tags: string[];
  ignore_globs: string[];
  created_at: string;
  doc_count: number;
}

export interface AddWatchDirRequest {
  path: string;
  folder?: string;
  tags?: string;
  ignore_globs?: string[];
}
```

- [ ] **Step 2: Build to verify type correctness**

Run: `cd frontend && npm run build`
Expected: No TypeScript errors. Build completes successfully.

- [ ] **Step 3: Commit**

```bash
cd /Users/nhatminhphan/Desktop/Code/AI/docgraph/.worktrees/file-watcher
git add frontend/src/types.ts
git commit -m "feat(ui): extend types for watcher (Document.source_type=watched, WatcherStatus, WatchedDir)"
```

---

## Task 2: API client + error helper

**Files:**
- Modify: `frontend/src/api.ts`

- [ ] **Step 1: Replace `api.ts` content**

Open `frontend/src/api.ts` and replace its full content with:

```typescript
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
  if (!r.ok) throw new Error("watcher status failed");
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
  if (!r.ok) throw new Error("list watched dirs failed");
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
```

- [ ] **Step 2: Build to verify**

Run: `cd frontend && npm run build`
Expected: No TypeScript errors. Build completes.

- [ ] **Step 3: Commit**

```bash
cd /Users/nhatminhphan/Desktop/Code/AI/docgraph/.worktrees/file-watcher
git add frontend/src/api.ts
git commit -m "feat(ui): watcher API client (7 fns) + shared _errorFromResponse helper"
```

---

## Task 3: `[WATCHED]` badge in DocumentTable

**Files:**
- Modify: `frontend/src/components/DocumentTable.tsx`

- [ ] **Step 1: Update the filename cell render**

Open `frontend/src/components/DocumentTable.tsx`. Find this block (around line 67):

```tsx
<td className="filename-cell">
  <span title={d.source_url || d.filename}>{d.filename}</span>
</td>
```

Replace with:

```tsx
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
```

- [ ] **Step 2: Build to verify**

Run: `cd frontend && npm run build`
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
cd /Users/nhatminhphan/Desktop/Code/AI/docgraph/.worktrees/file-watcher
git add frontend/src/components/DocumentTable.tsx
git commit -m "feat(ui): [WATCHED] badge in DocumentTable filename cell"
```

---

## Task 4: CSS for badge + watcher styles

**Files:**
- Modify: `frontend/src/styles/components.css`

- [ ] **Step 1: Append watcher styles**

Open `frontend/src/styles/components.css` and append at the end of the file:

```css
/* —— Watcher badge —— */
.badge {
  display: inline-block;
  padding: 2px 6px;
  margin-left: 0.5rem;
  border: 1px solid var(--border);
  border-radius: 2px;
  font-size: 0.7rem;
  letter-spacing: 0.05em;
  vertical-align: middle;
}

.badge-watched {
  background: var(--background);
  color: var(--foreground);
}

/* —— Folder watch tab section —— */
.folder-watch-section {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

/* —— Watcher status bar —— */
.watcher-status-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem 1rem;
  align-items: center;
  justify-content: space-between;
  padding: 1rem 1.25rem;
  border: 1px solid var(--border);
  border-radius: 2px;
}

.watcher-status-bar__info {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  min-width: 0;
  flex: 1 1 auto;
}

.watcher-status-bar__actions {
  display: flex;
  gap: 0.5rem;
  flex-shrink: 0;
}

.watcher-status-bar__main {
  font-family: var(--font-mono);
  font-size: 0.875rem;
}

.watcher-status-bar__detail {
  font-family: var(--font-mono);
  font-size: 0.75rem;
  color: var(--muted-foreground);
}

.watcher-status-bar__warn {
  color: #b45309;
}

.watcher-status-bar__error {
  color: var(--destructive, #b91c1c);
  font-size: 0.8125rem;
  margin-top: 0.5rem;
  flex-basis: 100%;
}

/* —— Watched dirs table —— */
.watched-dirs-table {
  border: 1px solid var(--border);
  border-radius: 2px;
}

.watched-dirs-table__empty {
  padding: 1.5rem;
  text-align: center;
  color: var(--muted-foreground);
  font-size: 0.875rem;
}

.watched-dirs-table table {
  width: 100%;
  border-collapse: collapse;
}

.watched-dirs-table th,
.watched-dirs-table td {
  padding: 0.6rem 0.75rem;
  text-align: left;
  font-size: 0.875rem;
  border-bottom: 1px solid var(--border);
}

.watched-dirs-table th {
  font-family: var(--font-mono);
  font-size: 0.7rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted-foreground);
}

.watched-dirs-table__confirm-row td {
  background: var(--muted, #f5f5f5);
  padding: 1rem 1.25rem;
}

.watched-dirs-table__confirm-text {
  margin-bottom: 0.75rem;
  font-size: 0.875rem;
}

.watched-dirs-table__confirm-actions {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.watched-dirs-table__path {
  font-family: var(--font-mono);
  font-size: 0.8125rem;
  max-width: 22rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.watched-dirs-table__id {
  font-family: var(--font-mono);
  font-size: 0.75rem;
  color: var(--muted-foreground);
}

/* —— Add watch dir form —— */
.add-watch-dir-form {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  padding: 1.25rem;
  border: 1px solid var(--border);
  border-radius: 2px;
}

.add-watch-dir-form__row {
  display: grid;
  grid-template-columns: 8rem 1fr;
  gap: 0.75rem;
  align-items: center;
}

.add-watch-dir-form__row label {
  font-family: var(--font-mono);
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--muted-foreground);
}

.add-watch-dir-form__row input {
  padding: 0.5rem 0.75rem;
  border: 1px solid var(--border);
  background: var(--background);
  font-family: var(--font-mono);
  font-size: 0.875rem;
}

.add-watch-dir-form__error {
  color: var(--destructive, #b91c1c);
  font-size: 0.8125rem;
}

.add-watch-dir-form__hint {
  color: var(--muted-foreground);
  font-size: 0.8125rem;
}

.add-watch-dir-form__submit {
  align-self: flex-end;
}
```

- [ ] **Step 2: Build to verify**

Run: `cd frontend && npm run build`
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
cd /Users/nhatminhphan/Desktop/Code/AI/docgraph/.worktrees/file-watcher
git add frontend/src/styles/components.css
git commit -m "feat(ui): CSS for watcher badge + status bar + dirs table + add form"
```

---

## Task 5: `WatcherStatusBar.tsx`

**Files:**
- Create: `frontend/src/components/WatcherStatusBar.tsx`

- [ ] **Step 1: Create the file**

Write to `frontend/src/components/WatcherStatusBar.tsx`:

```typescript
import { useCallback, useEffect, useState } from "react";
import {
  disableWatcher,
  enableWatcher,
  fetchWatcherStatus,
  triggerReconcile,
} from "../api";
import type { WatcherStatus } from "../types";
import { Button } from "./ui/Button";

interface WatcherStatusBarProps {
  refreshTick: number;
  onAction: () => void;
}

function formatTimestamp(iso: string | null): string {
  if (!iso) return "never";
  try {
    const d = new Date(iso);
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return `${hh}:${mm}`;
  } catch {
    return "—";
  }
}

function relativeMinutes(iso: string | null): string {
  if (!iso) return "";
  try {
    const t = new Date(iso).getTime();
    const ageMs = Date.now() - t;
    const mins = Math.floor(ageMs / 60_000);
    if (mins < 1) return " (just now)";
    if (mins < 60) return ` (${mins} min ago)`;
    const hours = Math.floor(mins / 60);
    return ` (${hours}h ago)`;
  } catch {
    return "";
  }
}

export function WatcherStatusBar({ refreshTick, onAction }: WatcherStatusBarProps) {
  const [status, setStatus] = useState<WatcherStatus | null>(null);
  const [acting, setActing] = useState<"enable" | "disable" | "reconcile" | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Polling — setTimeout chain to match App.tsx pattern.
  useEffect(() => {
    let cancelled = false;
    let timer = 0;

    const tick = async () => {
      if (cancelled) return;
      try {
        const s = await fetchWatcherStatus();
        if (!cancelled) setStatus(s);
      } catch {
        // Silent — keep last good status.
      }
      if (cancelled) return;
      const ms = status?.enabled ? 2000 : 5000;
      timer = window.setTimeout(tick, ms);
    };

    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // refreshTick forces re-fetch after sibling actions; status.enabled drives cadence.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshTick, status?.enabled]);

  const runAction = useCallback(
    async (kind: "enable" | "disable" | "reconcile", fn: () => Promise<unknown>) => {
      setActing(kind);
      setError(null);
      try {
        await fn();
        const s = await fetchWatcherStatus();
        setStatus(s);
        onAction();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Action failed");
      } finally {
        setActing(null);
      }
    },
    [onAction],
  );

  if (!status) {
    return (
      <div className="watcher-status-bar">
        <div className="watcher-status-bar__info">
          <span className="watcher-status-bar__main">Watcher: loading…</span>
        </div>
      </div>
    );
  }

  const enabled = status.enabled;
  const running = status.running;
  const queuePct = (status.queue_depth / status.queue_capacity) * 100;
  const queueWarn = queuePct >= 80;

  const observerDown = enabled && !running;

  return (
    <div className="watcher-status-bar">
      <div className="watcher-status-bar__info">
        <span className="watcher-status-bar__main">
          Watcher: {enabled ? "ENABLED" : "DISABLED"}
          {observerDown && (
            <span className="watcher-status-bar__warn"> · observer DOWN</span>
          )}
          {" · "}
          {status.dirs_count} {status.dirs_count === 1 ? "dir" : "dirs"}
          {enabled && (
            <>
              {" · queue "}
              <span className={queueWarn ? "watcher-status-bar__warn" : ""}>
                {status.queue_depth}/{status.queue_capacity}
                {queueWarn && ` (${Math.round(queuePct)}% full)`}
              </span>
              {" · "}
              {status.stats.events_processed} events processed
              {status.stats.events_dropped_queue_full > 0 && (
                <span className="watcher-status-bar__warn">
                  {" · ⚠ "}
                  {status.stats.events_dropped_queue_full} events dropped
                </span>
              )}
            </>
          )}
        </span>
        {enabled && (
          <span className="watcher-status-bar__detail">
            Last reconcile: {formatTimestamp(status.stats.last_reconcile_at)}
            {relativeMinutes(status.stats.last_reconcile_at)}
          </span>
        )}
      </div>
      <div className="watcher-status-bar__actions">
        {enabled && (
          <Button
            variant="outline"
            size="sm"
            disabled={acting !== null}
            onClick={() => void runAction("reconcile", triggerReconcile)}
          >
            {acting === "reconcile" ? "Reconciling…" : "Reconcile"}
          </Button>
        )}
        {enabled ? (
          <Button
            variant="ghost"
            size="sm"
            disabled={acting !== null}
            onClick={() => void runAction("disable", disableWatcher)}
          >
            {acting === "disable" ? "Disabling…" : "Disable watcher"}
          </Button>
        ) : (
          <Button
            variant="primary"
            size="sm"
            disabled={acting !== null}
            onClick={() => void runAction("enable", enableWatcher)}
          >
            {acting === "enable" ? "Enabling…" : "Enable watcher"}
          </Button>
        )}
      </div>
      {error && <div className="watcher-status-bar__error">{error}</div>}
    </div>
  );
}
```

- [ ] **Step 2: Build to verify**

Run: `cd frontend && npm run build`
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
cd /Users/nhatminhphan/Desktop/Code/AI/docgraph/.worktrees/file-watcher
git add frontend/src/components/WatcherStatusBar.tsx
git commit -m "feat(ui): WatcherStatusBar (polling, Enable/Disable/Reconcile)"
```

---

## Task 6: `WatchedDirsTable.tsx` (list + remove confirm)

**Files:**
- Create: `frontend/src/components/WatchedDirsTable.tsx`

- [ ] **Step 1: Create the file**

Write to `frontend/src/components/WatchedDirsTable.tsx`:

```typescript
import { useEffect, useState } from "react";
import { fetchWatchedDirs, removeWatchedDir } from "../api";
import type { WatchedDir } from "../types";
import { Button } from "./ui/Button";

interface WatchedDirsTableProps {
  refreshTick: number;
  onRemoved: () => void;
}

function shortId(id: string): string {
  // Show "wd_a1b2c3d4" (first 11 chars including "wd_" prefix).
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
              <>
                <tr key={d.id}>
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
                  <tr key={`${d.id}-confirm`} className="watched-dirs-table__confirm-row">
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
              </>
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
```

- [ ] **Step 2: Build to verify**

Run: `cd frontend && npm run build`
Expected: No errors.

Note: React 19 supports `<>...</>` fragments inside `.map()` returns. If a warning about keys appears, double-check the fragment-vs-array structure — the code above returns a Fragment per row but uses `key` on the fragment children directly via `<></>` wrapping, which is fine in React 19.

- [ ] **Step 3: Commit**

```bash
cd /Users/nhatminhphan/Desktop/Code/AI/docgraph/.worktrees/file-watcher
git add frontend/src/components/WatchedDirsTable.tsx
git commit -m "feat(ui): WatchedDirsTable with inline remove confirmation"
```

---

## Task 7: `AddWatchDirForm.tsx`

**Files:**
- Create: `frontend/src/components/AddWatchDirForm.tsx`

- [ ] **Step 1: Create the file**

Write to `frontend/src/components/AddWatchDirForm.tsx`:

```typescript
import { useState } from "react";
import { addWatchedDir, fetchWatcherStatus } from "../api";
import { Button } from "./ui/Button";

interface AddWatchDirFormProps {
  onAdded: () => void;
}

function parseCSV(s: string): string[] {
  return s
    .split(",")
    .map((x) => x.trim())
    .filter((x) => x.length > 0);
}

export function AddWatchDirForm({ onAdded }: AddWatchDirFormProps) {
  const [path, setPath] = useState("");
  const [folder, setFolder] = useState("");
  const [tags, setTags] = useState("");
  const [ignore, setIgnore] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setHint(null);
    if (!path.trim()) {
      setError("Path is required");
      return;
    }
    setSubmitting(true);
    try {
      await addWatchedDir({
        path: path.trim(),
        folder: folder.trim(),
        tags,
        ignore_globs: parseCSV(ignore),
      });
      // Check if watcher is currently disabled so we can show the enable hint.
      let watcherEnabled = false;
      try {
        const status = await fetchWatcherStatus();
        watcherEnabled = status.enabled;
      } catch {
        // Treat unknown as disabled — hint is conservative.
      }
      setPath("");
      setFolder("");
      setTags("");
      setIgnore("");
      if (!watcherEnabled) {
        setHint("✓ Directory added. Enable the watcher to start indexing.");
      } else {
        setHint("✓ Directory added.");
      }
      onAdded();
    } catch (e2) {
      setError(e2 instanceof Error ? e2.message : "Add failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form className="add-watch-dir-form" onSubmit={handleSubmit}>
      <p className="label-mono">Add directory</p>

      <div className="add-watch-dir-form__row">
        <label htmlFor="watch-path">Path *</label>
        <input
          id="watch-path"
          type="text"
          placeholder="/Users/you/Notes"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          disabled={submitting}
          required
        />
      </div>

      <div className="add-watch-dir-form__row">
        <label htmlFor="watch-folder">Folder</label>
        <input
          id="watch-folder"
          type="text"
          placeholder="notes"
          value={folder}
          onChange={(e) => setFolder(e.target.value)}
          disabled={submitting}
        />
      </div>

      <div className="add-watch-dir-form__row">
        <label htmlFor="watch-tags">Tags</label>
        <input
          id="watch-tags"
          type="text"
          placeholder="personal, important"
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          disabled={submitting}
        />
      </div>

      <div className="add-watch-dir-form__row">
        <label htmlFor="watch-ignore">Ignore globs</label>
        <input
          id="watch-ignore"
          type="text"
          placeholder="draft/*, **/.tmp"
          value={ignore}
          onChange={(e) => setIgnore(e.target.value)}
          disabled={submitting}
        />
      </div>

      {error && <div className="add-watch-dir-form__error">{error}</div>}
      {hint && !error && <div className="add-watch-dir-form__hint">{hint}</div>}

      <Button
        variant="primary"
        size="sm"
        type="submit"
        disabled={submitting}
        className="add-watch-dir-form__submit"
      >
        {submitting ? "Adding…" : "Add directory"}
      </Button>
    </form>
  );
}
```

- [ ] **Step 2: Build to verify**

Run: `cd frontend && npm run build`
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
cd /Users/nhatminhphan/Desktop/Code/AI/docgraph/.worktrees/file-watcher
git add frontend/src/components/AddWatchDirForm.tsx
git commit -m "feat(ui): AddWatchDirForm with path/folder/tags/ignore inputs"
```

---

## Task 8: `FolderWatchSection.tsx` composer

**Files:**
- Create: `frontend/src/components/FolderWatchSection.tsx`

- [ ] **Step 1: Create the file**

Write to `frontend/src/components/FolderWatchSection.tsx`:

```typescript
import { useState } from "react";
import { AddWatchDirForm } from "./AddWatchDirForm";
import { WatchedDirsTable } from "./WatchedDirsTable";
import { WatcherStatusBar } from "./WatcherStatusBar";

interface FolderWatchSectionProps {
  onChanged: () => void;
}

export function FolderWatchSection({ onChanged }: FolderWatchSectionProps) {
  const [refreshTick, setRefreshTick] = useState(0);
  const bump = () => setRefreshTick((t) => t + 1);

  return (
    <div className="folder-watch-section">
      <WatcherStatusBar refreshTick={refreshTick} onAction={bump} />
      <WatchedDirsTable
        refreshTick={refreshTick}
        onRemoved={() => {
          bump();
          onChanged();
        }}
      />
      <AddWatchDirForm
        onAdded={() => {
          bump();
          onChanged();
        }}
      />
    </div>
  );
}
```

- [ ] **Step 2: Build to verify**

Run: `cd frontend && npm run build`
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
cd /Users/nhatminhphan/Desktop/Code/AI/docgraph/.worktrees/file-watcher
git add frontend/src/components/FolderWatchSection.tsx
git commit -m "feat(ui): FolderWatchSection composer wiring status + dirs + form"
```

---

## Task 9: Add 4th tab to IngestTabs

**Files:**
- Modify: `frontend/src/components/IngestTabs.tsx`

- [ ] **Step 1: Replace the file content**

Open `frontend/src/components/IngestTabs.tsx` and replace its full content with:

```typescript
import { useState } from "react";
import { FolderWatchSection } from "./FolderWatchSection";
import { LinkImportSection } from "./LinkImportSection";
import { UploadSection } from "./UploadSection";

type IngestTab = "upload" | "web" | "code" | "watch";

const TABS: { id: IngestTab; label: string }[] = [
  { id: "upload", label: "Upload" },
  { id: "web", label: "Web pages" },
  { id: "code", label: "Code" },
  { id: "watch", label: "Folder watch" },
];

interface IngestTabsProps {
  onChanged: () => void;
}

export function IngestTabs({ onChanged }: IngestTabsProps) {
  const [tab, setTab] = useState<IngestTab>("upload");

  return (
    <section className="section ingest-section" aria-labelledby="ingest-heading">
      <p className="label-mono" id="ingest-heading">
        01 — Ingest
      </p>

      <div className="ingest-tabs" role="tablist" aria-label="Ingest source">
        {TABS.map(({ id, label }) => (
          <button
            key={id}
            type="button"
            role="tab"
            id={`ingest-tab-${id}`}
            aria-selected={tab === id}
            aria-controls={`ingest-panel-${id}`}
            className={`ingest-tab${tab === id ? " ingest-tab--active" : ""}`}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </div>

      <div
        role="tabpanel"
        id={`ingest-panel-${tab}`}
        aria-labelledby={`ingest-tab-${tab}`}
        className="ingest-panel"
      >
        {tab === "upload" && (
          <UploadSection embedded onUploaded={onChanged} variant="documents" />
        )}
        {tab === "web" && (
          <LinkImportSection embedded onImported={onChanged} />
        )}
        {tab === "code" && (
          <UploadSection embedded onUploaded={onChanged} variant="code" />
        )}
        {tab === "watch" && <FolderWatchSection onChanged={onChanged} />}
      </div>
    </section>
  );
}
```

- [ ] **Step 2: Build to verify**

Run: `cd frontend && npm run build`
Expected: No errors. The full bundle should produce updated assets in `docgraph/web/static/`.

- [ ] **Step 3: Commit**

```bash
cd /Users/nhatminhphan/Desktop/Code/AI/docgraph/.worktrees/file-watcher
git add frontend/src/components/IngestTabs.tsx
git commit -m "feat(ui): wire FolderWatchSection as 4th Ingest tab"
```

---

## Task 10: Manual smoke test on running server

**Files:**
- No code changes (verification only). If you discover bugs, add an extra fix-commit before declaring done.

- [ ] **Step 1: Build the production bundle**

Run: `cd /Users/nhatminhphan/Desktop/Code/AI/docgraph/.worktrees/file-watcher/frontend && npm run build`
Expected: Build succeeds. Files in `docgraph/web/static/` are updated.

- [ ] **Step 2: Run backend**

Run (in worktree root): `poetry run docgraph serve`
Wait for: `INFO: Application startup complete.`
Open: `http://127.0.0.1:8088`

- [ ] **Step 3: Walk the smoke-test checklist**

Verify each item in order. The browser must show what the description says. If a step fails, stop and add a fix commit before continuing.

- [ ] **3a.** Tab "Folder watch" is visible as the 4th tab in the Ingest section, after "Code".
- [ ] **3b.** Initial state (fresh sqlite or after `docgraph watch disable`): status bar shows `Watcher: DISABLED · 0 dirs configured` and an `Enable watcher` button.
- [ ] **3c.** Add a valid directory via the form (use `/tmp/docgraph-smoke` after `mkdir /tmp/docgraph-smoke`):
  - Path → `/tmp/docgraph-smoke`
  - Folder → `smoke`
  - Tags → `test`
  - Ignore globs → (empty)
  - Click `Add directory`. Expected: hint `✓ Directory added. Enable the watcher to start indexing.`. The row appears in the dirs table.
- [ ] **3d.** Add a non-existent path (e.g. `/tmp/does-not-exist-x9`):
  - Expected: inline error like `path does not exist: /tmp/does-not-exist-x9`. Form retains values.
- [ ] **3e.** Try to add an overlapping path (e.g. `/tmp/docgraph-smoke/sub` after creating that subdir):
  - Expected: inline error like `path overlaps with watched dir wd_...`. Form retains values.
- [ ] **3f.** Click `Enable watcher`. Expected: status bar transitions to `ENABLED · 1 dir · queue 0/500 · 0 events processed`. Polling visibly cadences faster (DevTools Network tab shows `/api/watch/status` every ~2s).
- [ ] **3g.** Create a test file: `echo "# smoke" > /tmp/docgraph-smoke/note.md`. Wait ~3 seconds. Expected: a new row appears in DocumentTable with filename `note.md`, folder `smoke`, tags `test`, and a `[WATCHED]` badge. Hover over the badge shows the full path as tooltip.
- [ ] **3h.** Modify the file: `echo "# smoke v2" >> /tmp/docgraph-smoke/note.md`. Wait ~3 seconds. Expected: doc row stays the same id, chunk count may update.
- [ ] **3i.** Delete the file: `rm /tmp/docgraph-smoke/note.md`. Wait ~3 seconds. Expected: the doc row disappears from DocumentTable.
- [ ] **3j.** Click `Remove` on the watched dir row. Confirm row expands. Click `Keep N docs in index` (or `Remove` if 0 docs). Expected: dir disappears from table; if any docs existed, they remain in DocumentTable but lose the `[WATCHED]` badge update on next refresh (or keep it — both behaviors are acceptable per spec v1 §4.3.4).
- [ ] **3k.** Re-add the dir, re-enable watcher, create another file, then click `Remove` → `Delete N docs too`. Expected: dir AND the docs both gone from DocumentTable.
- [ ] **3l.** Click `Disable watcher`. Expected: status bar reverts to `DISABLED`, polling slows to 5s (visible in DevTools).
- [ ] **3m.** Reload the browser. Expected: state persists — `dirs_count` and `enabled` reflect last server-side state.

- [ ] **Step 4: Commit (no-op or fix)**

If all smoke steps passed without code changes, no commit is needed here — the feature is shippable.

If you applied fixes, commit them with a clear message:

```bash
cd /Users/nhatminhphan/Desktop/Code/AI/docgraph/.worktrees/file-watcher
git add -A
git commit -m "fix(ui): smoke-test corrections"
```

- [ ] **Step 5: Final summary**

Print the commit log for the feature:

```bash
git log --oneline 6b378d8..HEAD
```

Expected: 9-10 commits covering Tasks 1-9 plus optional fix(es) from Step 4. Confirm all tasks above are checked.

---

## Done

After Task 10 completes:
- 4 new components in `frontend/src/components/`
- 5 modified files (types, api, IngestTabs, DocumentTable, components.css)
- 1 new tab `Folder watch` rendering the full CRUD UI
- 1 new `[WATCHED]` badge in DocumentTable
- Smoke checklist passes against a real backend

Spec coverage cross-check:
- §3 Architecture — Tasks 5, 6, 7, 8
- §4 Types — Task 1
- §5 API client — Task 2
- §6 Component contracts — Tasks 5, 6, 7, 8 + 3 (badge) + 9 (tabs)
- §7 Remove confirm flow — Task 6
- §8 Error matrix — Tasks 5, 6, 7 (each component handles its row)
- §9 Polling cadence — Task 5 (status bar) + existing App.tsx unchanged
- §10 Testing — Task 10 smoke checklist
- §11 Out of scope — respected (no new endpoints, no test harness, no modal, no editing)
- §12 Build verification — Task 10
