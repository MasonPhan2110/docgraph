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
