import { useEffect, useRef, useState } from "react";
import { addProject, browseDirectory, listProjects, removeProject } from "./chat/api";
import type { BrowseResult, Project } from "./chat/api";

function FolderPicker({
  onSelect,
  onCancel,
}: {
  onSelect: (path: string) => void;
  onCancel: () => void;
}) {
  const [browse, setBrowse] = useState<BrowseResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [manualPath, setManualPath] = useState("");

  const navigate = (path: string) => {
    setLoading(true);
    browseDirectory(path)
      .then((r) => {
        setBrowse(r);
        setManualPath(r.path);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    navigate("");
  }, []);

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal-content folder-picker" onClick={(e) => e.stopPropagation()}>
        <h3 className="modal-title">Select Folder</h3>

        <div className="folder-picker-path-row">
          <input
            className="folder-picker-path-input"
            value={manualPath}
            onChange={(e) => setManualPath(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") navigate(manualPath);
            }}
            placeholder="Type a path and press Enter"
          />
          <button className="folder-picker-go" onClick={() => navigate(manualPath)}>
            Go
          </button>
        </div>

        {browse && browse.parent !== null && (
          <button className="folder-picker-up" onClick={() => navigate(browse.parent!)}>
            .. (up)
          </button>
        )}

        <ul className="folder-picker-list">
          {loading && <li className="folder-picker-loading">Loading...</li>}
          {!loading && browse && browse.dirs.length === 0 && (
            <li className="folder-picker-empty">No subdirectories</li>
          )}
          {!loading &&
            browse?.dirs.map((d) => (
              <li key={d.path}>
                <button
                  className="folder-picker-dir"
                  onClick={() => navigate(d.path)}
                >
                  {d.name}
                </button>
              </li>
            ))}
        </ul>

        <div className="folder-picker-actions">
          <button
            className="new-chat-btn"
            onClick={() => onSelect(browse?.path || manualPath)}
            disabled={!browse?.path && !manualPath.trim()}
          >
            Select This Folder
          </button>
          <button className="project-cancel-btn" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

export default function ProjectsView() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [showPicker, setShowPicker] = useState(false);
  const [loading, setLoading] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    listProjects().then(setProjects).catch(console.error);
  }, []);

  useEffect(() => {
    if (showForm) nameRef.current?.focus();
  }, [showForm]);

  const handleAdd = async () => {
    const trimName = name.trim();
    const trimPath = path.trim();
    if (!trimName || !trimPath) return;
    setLoading(true);
    try {
      await addProject(trimName, trimPath);
      setProjects((prev) =>
        [...prev, { name: trimName, path: trimPath }].sort((a, b) =>
          a.name.localeCompare(b.name),
        ),
      );
      setName("");
      setPath("");
      setShowForm(false);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  };

  const handleRemove = async (projectName: string) => {
    if (!confirm(`Remove project "${projectName}"?`)) return;
    try {
      await removeProject(projectName);
      setProjects((prev) => prev.filter((p) => p.name !== projectName));
    } catch (e) {
      console.error(e);
    }
  };

  const autoNameFromPath = (dirPath: string) => {
    setPath(dirPath);
    if (!name.trim()) {
      const parts = dirPath.replace(/[\\/]+$/, "").split(/[\\/]/);
      const last = parts[parts.length - 1];
      if (last) setName(last);
    }
  };

  return (
    <div className="projects-view">
      <div className="projects-header">
        <h2>Projects</h2>
        <button className="new-chat-btn" onClick={() => setShowForm(true)}>
          + Add Project
        </button>
      </div>

      {showForm && (
        <div className="project-add-form">
          <input
            ref={nameRef}
            className="project-form-input"
            placeholder="Project name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleAdd();
              if (e.key === "Escape") {
                setShowForm(false);
                setName("");
                setPath("");
              }
            }}
            disabled={loading}
          />
          <div className="project-path-row">
            <input
              className="project-form-input project-form-path"
              placeholder="Local directory path"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleAdd();
                if (e.key === "Escape") {
                  setShowForm(false);
                  setName("");
                  setPath("");
                }
              }}
              disabled={loading}
            />
            <button
              className="browse-btn"
              onClick={() => setShowPicker(true)}
              type="button"
              title="Browse folders"
            >
              Browse
            </button>
          </div>
          <div className="project-form-actions">
            <button
              className="new-chat-btn"
              onClick={handleAdd}
              disabled={loading || !name.trim() || !path.trim()}
            >
              Add
            </button>
            <button
              className="project-cancel-btn"
              onClick={() => {
                setShowForm(false);
                setName("");
                setPath("");
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {showPicker && (
        <FolderPicker
          onSelect={(p) => {
            autoNameFromPath(p);
            setShowPicker(false);
          }}
          onCancel={() => setShowPicker(false)}
        />
      )}

      {projects.length === 0 && !showForm && (
        <p className="projects-empty">
          No projects yet. Add one to get started.
        </p>
      )}

      <ul className="projects-list">
        {projects.map((p) => (
          <li key={p.name} className="project-item">
            <div className="project-item-info">
              <span className="project-item-name">{p.name}</span>
              <span className="project-item-path">{p.path}</span>
            </div>
            <button
              className="project-remove-btn"
              onClick={() => handleRemove(p.name)}
              title="Remove project"
              aria-label="Remove project"
            >
              &times;
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
