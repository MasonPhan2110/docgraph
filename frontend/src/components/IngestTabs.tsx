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
