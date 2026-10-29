from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class WatcherState(str, Enum):
    DISABLED = "disabled"
    ENABLING = "enabling"
    ENABLED = "enabled"
    DISABLING = "disabling"


@dataclass
class WatchEvent:
    action: str            # "UPSERT" | "DELETE" | "RENAME"
    src_path: str
    dest_path: Optional[str] = None


@dataclass
class WatcherStats:
    events_received: int = 0
    events_debounced: int = 0
    events_processed: int = 0
    events_dropped_queue_full: int = 0
    reconcile_runs: int = 0
    last_reconcile_at: Optional[str] = None

    def reset(self) -> None:
        self.events_received = 0
        self.events_debounced = 0
        self.events_processed = 0
        self.events_dropped_queue_full = 0
        self.reconcile_runs = 0
        self.last_reconcile_at = None
