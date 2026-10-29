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
