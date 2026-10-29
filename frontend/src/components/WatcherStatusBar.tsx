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
    async (
      kind: "enable" | "disable" | "reconcile",
      fn: () => Promise<unknown>,
      isRetry = false,
    ) => {
      setActing(kind);
      setError(null);
      let scheduledRetry = false;
      try {
        await fn();
        const s = await fetchWatcherStatus();
        setStatus(s);
        onAction();
      } catch (e) {
        const msg = e instanceof Error ? e.message : "Action failed";
        // Spec §8: 409 transition-in-progress → auto-retry once after 2s.
        if (
          !isRetry &&
          (kind === "enable" || kind === "disable") &&
          msg.toLowerCase().includes("transition in progress")
        ) {
          scheduledRetry = true;
          setError(`${msg} — retrying in 2s…`);
          window.setTimeout(() => void runAction(kind, fn, true), 2000);
        } else {
          setError(msg);
        }
      } finally {
        // Keep `acting` set while a retry is pending so the button stays
        // disabled across the 2s gap. The retry call will clear it.
        if (!scheduledRetry) setActing(null);
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
