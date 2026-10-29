# File Watcher UI — Design

**Status:** Brainstorming complete — pending user review, then implementation plan
**Date:** 2026-06-08
**Author:** brainstorm session, DocGraph maintainer
**Scope:** React frontend for the file watcher backend (spec at `docs/superpowers/specs/2026-06-07-file-watcher-design.md`). Adds a "Folder watch" tab to the existing Ingest section, with full CRUD over watched directories and a per-doc badge in the documents table.

---

## 1. Goal

Expose the file watcher feature in the browser UI so users can enable/disable the watcher, add/remove watched directories, trigger a manual reconcile, and distinguish watched docs from uploaded/URL docs — all without dropping to a terminal.

## 2. Non-goals

- Bootstrap a test harness for the frontend (no Vitest/RTL in this PR — separate concern).
- Edit an existing watched dir's folder/tags/ignore_globs (remove + re-add for v1).
- Visually distinguish "orphan watched" docs (watched dir removed with `delete_docs=false`).
- Drag-drop folder to add (browser FS access limitations).
- Server-sent events / WebSocket — keep polling pattern that already exists for `/api/documents`.
- Bulk operations (multi-select dirs to remove).
- Mobile-responsive design — DocGraph is a desktop tool.
- i18n — match existing English-only strings.
- Per-dir reconcile (only all-dirs Reconcile button).
- Backend changes — the watcher API was finalized in roadmap 3.1 (`feat/file-watcher` branch).

## 3. Architecture

```
App
├── Header
└── main
    ├── IngestTabs                     ← MODIFY: add 4th tab
    │   ├── tab=upload  → UploadSection
    │   ├── tab=web     → LinkImportSection
    │   ├── tab=code    → UploadSection variant=code
    │   └── tab=watch   → FolderWatchSection    ← NEW
    │       ├── WatcherStatusBar       ← NEW
    │       ├── WatchedDirsTable       ← NEW
    │       └── AddWatchDirForm        ← NEW
    └── DocumentTable                  ← MODIFY: [WATCHED] badge cell
```

**Composer pattern:** `FolderWatchSection` owns no fetched state — only a `refreshTick` counter that children re-fetch on. Each child manages its own data + loading + error state. This matches the existing `UploadSection`/`LinkImportSection` pattern (each ingest tab owns its slice).

**State ownership:**
- Watcher status (enabled, queue, stats) → `WatcherStatusBar` local state, polled.
- Watched dirs list → `WatchedDirsTable` local state, re-fetch on `refreshTick` change.
- Form input → `AddWatchDirForm` local state, controlled inputs.
- Per-doc watched info → `DocumentTable` reads from existing `Document.source_type === "watched"` (already on `feat/file-watcher` backend).

**Files:**

```
NEW:
  frontend/src/components/FolderWatchSection.tsx       (~50 LOC composer)
  frontend/src/components/WatcherStatusBar.tsx         (~120 LOC + polling)
  frontend/src/components/WatchedDirsTable.tsx         (~150 LOC + confirm flow)
  frontend/src/components/AddWatchDirForm.tsx          (~100 LOC + validation)

MODIFY:
  frontend/src/components/IngestTabs.tsx               (+5 LOC for 4th tab)
  frontend/src/components/DocumentTable.tsx            (+8 LOC for badge)
  frontend/src/api.ts                                  (+85 LOC for 7 fns + error helper)
  frontend/src/types.ts                                (+25 LOC for new interfaces)
  frontend/src/styles/components.css                   (+30 LOC for badge + status styles)
```

Total: ~570 LOC new, ~50 LOC modified.

## 4. Types

Extend `frontend/src/types.ts`:

```typescript
// MODIFY existing Document
export interface Document {
  // ... existing fields ...
  source_type?: "file" | "url" | "watched";  // add "watched"
  watched_path?: string;                       // present only when source_type=watched
}

// NEW
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
  tags?: string;            // comma-separated to match existing upload pattern
  ignore_globs?: string[];
}
```

## 5. API client

Add to `frontend/src/api.ts`:

```typescript
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

export async function disableWatcher(): Promise<{ enabled: false; queue_drained: number; queue_dropped: number }> {
  const r = await fetch("/api/watch/disable", { method: "POST" });
  if (!r.ok) throw await _errorFromResponse(r);
  return r.json();
}

export async function fetchWatchedDirs(): Promise<WatchedDir[]> {
  const r = await fetch("/api/watch/dirs");
  if (!r.ok) throw new Error("list watched dirs failed");
  return (await r.json()).dirs;
}

export async function addWatchedDir(body: AddWatchDirRequest): Promise<{ id: string; path: string; scheduled: boolean }> {
  const r = await fetch("/api/watch/dirs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw await _errorFromResponse(r);
  return r.json();
}

export async function removeWatchedDir(wdId: string, deleteDocs: boolean): Promise<{ deleted_docs: number }> {
  const r = await fetch(`/api/watch/dirs/${wdId}?delete_docs=${deleteDocs}`, { method: "DELETE" });
  if (!r.ok) throw await _errorFromResponse(r);
  return r.json();
}

export async function triggerReconcile(): Promise<{ reconcile_started: boolean; dirs: number }> {
  const r = await fetch("/api/watch/reconcile", { method: "POST" });
  if (!r.ok) throw await _errorFromResponse(r);
  return r.json();
}

// Shared error helper — extracts {detail} from FastAPI HTTPException responses.
// Also refactor importUrls to use this helper.
async function _errorFromResponse(r: Response): Promise<Error> {
  try {
    const body = await r.json();
    const detail = typeof body.detail === "string" ? body.detail : `HTTP ${r.status}`;
    return new Error(detail);
  } catch {
    return new Error(`HTTP ${r.status}`);
  }
}
```

## 6. Component contracts

### 6.1 `FolderWatchSection.tsx`

```typescript
interface FolderWatchSectionProps {
  onChanged: () => void;   // bubble up to App so DocumentTable refreshes after CRUD
}
```

Owns a single `refreshTick` counter. Increments on every add/remove/enable/disable, passed down to children as a dep. Children re-fetch on tick change.

### 6.2 `WatcherStatusBar.tsx`

**Props:**
```typescript
interface WatcherStatusBarProps {
  refreshTick: number;
  onAction: () => void;
}
```

**State:** `status: WatcherStatus | null`, `error: string | null`, `acting: "enable" | "disable" | "reconcile" | null`.

**Polling:** `setTimeout` chain (matches `App.tsx` document polling pattern). Cadence: `2000ms` when `status.enabled === true`, `5000ms` when disabled. Silent failure on fetch error (keep last good status).

**Render — enabled state:**
```
┌─────────────────────────────────────────────────────────────────────────┐
│ Watcher: ENABLED · 3 dirs · queue 0/500 · 47 events processed           │
│ Last reconcile: 14:32 (2 min ago)                                       │
│                                          [Reconcile]  [Disable watcher] │
└─────────────────────────────────────────────────────────────────────────┘
```

**Render — disabled state:**
```
┌─────────────────────────────────────────────────────────────────────────┐
│ Watcher: DISABLED · 3 dirs configured                                   │
│                                                       [Enable watcher]  │
└─────────────────────────────────────────────────────────────────────────┘
```

**Warning states:**
- `enabled === true && running === false`: amber `observer DOWN` indicator, suggest disable+enable.
- `queue_depth > 0.8 * queue_capacity`: amber `(84% full)` annotation; if `events_dropped_queue_full > 0`, also show `⚠ N events dropped`.

Buttons disabled while `acting !== null`. Inline error message line below row on enable/disable failure.

### 6.3 `WatchedDirsTable.tsx`

**Props:**
```typescript
interface WatchedDirsTableProps {
  refreshTick: number;
  onRemoved: () => void;
}
```

**State:** `dirs: WatchedDir[]`, `loading`, `confirming: string | null` (wd_id of row currently in confirm state), `error: string | null`.

Re-fetches on `refreshTick` change. Only one row can be `confirming` at a time — clicking Remove on a different row switches focus (cancels prior).

**Render columns:** `ID | Path | Folder | Tags | Docs | Actions`

- ID column displays first 8 chars (`wd_a1b2c3d4`); tooltip full id.
- Path column truncates middle (`/Users/me/.../proj`); tooltip full path.
- Tags shown comma-joined; `—` if empty.
- Docs column shows `doc_count` from API.

### 6.4 `AddWatchDirForm.tsx`

**Props:**
```typescript
interface AddWatchDirFormProps {
  onAdded: () => void;
}
```

**State:** `path`, `folder`, `tags`, `ignore` (CSV strings), `submitting: boolean`, `error: string | null`.

**Inputs:**
- Path (required) — plain text input, placeholder `/Users/you/Notes`.
- Folder (optional) — plain text.
- Tags (optional) — CSV string (matches existing upload form).
- Ignore globs (optional) — CSV string in the form, split client-side into `string[]` before POST (backend expects array, unlike `tags` which is CSV string per existing upload-form convention).

**Submit flow:**
1. Validate `path.trim() !== ""` client-side.
2. POST `/api/watch/dirs` with body parsed (`ignore_globs` array, `tags` raw string).
3. On 2xx: clear form, call `onAdded()`.
4. On 4xx: show `error.message` (raw detail from `_errorFromResponse`) inline, keep form values.

**Render placeholder:**
```
┌─ Add directory ────────────────────────────────────────────────────────┐
│ Path *        [/Users/you/Notes_______________________________]        │
│ Folder        [notes______________________________________]            │
│ Tags          [personal, important______________________]              │
│ Ignore globs  [draft/*, **/.tmp__________________________]             │
│                                                                        │
│ [Inline error here on 400/409]                                         │
│                                                       [Add directory]  │
└────────────────────────────────────────────────────────────────────────┘
```

If watcher is currently disabled and add succeeds, show hint below form:
> `✓ Directory added. Enable the watcher to start indexing.`

### 6.5 `DocumentTable.tsx` modification

In the existing `<td className="filename-cell">`, append the badge conditionally:

```tsx
<td className="filename-cell">
  <span title={d.watched_path || d.source_url || d.filename}>{d.filename}</span>
  {d.source_type === "watched" && (
    <span className="badge badge-watched label-mono" title={d.watched_path}>
      WATCHED
    </span>
  )}
</td>
```

`.badge-watched` CSS — small pill, monospace, thin border, `padding: 2px 6px`, monochrome to match `label-mono`. Style added in `components.css`.

## 7. Remove confirmation flow

Inline expand row beneath the dir being removed — no modal, matches editorial design language.

**Default flow (doc_count > 0):**
```
┌────────────┬─────────────────────┬───────┬──────────┬───────┬─────────┐
│ wd_a1b2... │ /Users/me/Notes     │ notes │ personal │ 47    │ Remove  │
├────────────┴─────────────────────┴───────┴──────────┴───────┴─────────┤
│ ⚠ Remove `wd_a1b2c3d4e5f6` — 47 docs are currently indexed from this  │
│   directory. Files on disk are never touched.                          │
│                                                                        │
│   [Keep 47 docs in index]  [Delete 47 docs too]  [Cancel]              │
└────────────────────────────────────────────────────────────────────────┘
```

- `Keep 47 docs in index` → `DELETE …?delete_docs=false` — docs become orphans, search still finds them.
- `Delete 47 docs too` → `DELETE …?delete_docs=true` — chunks + Chroma + FTS + originals snapshots all removed.
- `Cancel` → close confirm row, no API call.

**Simplified flow when `doc_count === 0`:**
```
│ Remove `wd_e7f8g9` — no docs linked yet.                              │
│                                                                        │
│   [Remove]  [Cancel]                                                   │
```

`delete_docs=false` sent (doesn't matter when count is zero).

While DELETE is in-flight, all three buttons disabled and inline spinner shown.

## 8. Error matrix

| Scenario | UI behavior |
|---|---|
| AddWatchDirForm POST 400 (path missing / inside data_dir / system path) | Inline error below form, raw `detail`; form keeps values. |
| AddWatchDirForm POST 409 (overlap) | Inline error: `"Overlaps with wd_xxx at /parent. Remove parent first."` |
| Enable/Disable 409 (transition in progress) | Inline error in status bar, auto-retry once after 2s. |
| Remove 404 (wd_id removed from another session) | Re-fetch dirs list, brief toast: `"Watched dir already removed, list refreshed"`. |
| Reconcile when disabled | Reconcile button is grey-disabled when `status.enabled === false`; backend 409 is defense-in-depth. |
| Network error / server down | Status polling silent-fails (keep last good state). Action buttons surface clear error on click. |
| Watcher `running: false` while `enabled: true` | Amber `observer DOWN` indicator + button to disable/enable to rebuild. Auto-recovery still runs server-side. |

## 9. Polling cadence

Match existing `App.tsx` pattern (setTimeout chain, no setInterval):

- **`/api/watch/status`:** 2000ms when enabled, 5000ms when disabled. Polled by `WatcherStatusBar`.
- **`/api/watch/dirs`:** not polled — re-fetched only on `refreshTick` change (after add/remove/enable/disable).
- **`/api/documents`:** unchanged (existing 1500ms while processing, 5000ms idle).

Three independent polling timers; do not coalesce into a single status endpoint — each component owns its lifecycle.

## 10. Testing strategy

**v1 (this PR):** Manual smoke test via checklist below. No new test infrastructure added.

**v2 (future PR):** Add Vitest + React Testing Library + jsdom. Out of scope here.

**Smoke test checklist (must pass before merge):**

- [ ] Tab "Folder watch" appears as 4th tab in Ingest section.
- [ ] Initial state: watcher disabled, dirs list empty.
- [ ] Add directory with valid path → 201, dir appears in table.
- [ ] Add directory that doesn't exist → 400 inline error, form retains values.
- [ ] Add directory overlapping existing → 409 inline error.
- [ ] Enable watcher → status bar transitions, polling speeds up to 2s.
- [ ] Create test file in watched dir → ~3s later appears in DocumentTable with `[WATCHED]` badge.
- [ ] Modify file → progress phase update, mtime increases.
- [ ] Delete file → row disappears from DocumentTable.
- [ ] Remove watched dir (keep docs) → orphan docs still listed.
- [ ] Remove watched dir (delete docs) → row and docs both gone.
- [ ] Disable watcher → status bar updates, polling slows to 5s.
- [ ] Reload browser → state persists (watcher state + dirs from DB).

**Backend test coverage** (already passing on `feat/file-watcher`): 178 tests covering API behavior. FE smoke checklist focuses on wire-up, not re-verifying backend logic.

## 11. Out of scope (cross-check)

Already enumerated in §2 Non-goals. Repeating critical ones:

- No backend changes (watcher API frozen on `feat/file-watcher`).
- No new HTTP endpoint (no autocomplete `GET /api/watch/suggest`).
- No Vitest harness bootstrap.
- No editing existing watched dir — must remove + re-add.
- No orphan-watched visual indicator (v2).
- No modal dialogs — inline disclosure throughout.

## 12. Build verification

```bash
cd frontend && npm run build
```

Vite builds into `docgraph/web/static/`. Backend serves directly. Manual smoke test via `poetry run docgraph serve` at `http://127.0.0.1:8088`.

No npm dependency additions — uses only `react@^19`, `react-dom@^19`, existing dev deps (`typescript`, `vite`, `@vitejs/plugin-react`).
