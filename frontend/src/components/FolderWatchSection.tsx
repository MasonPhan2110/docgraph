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
