import { useCallback, useEffect, useState } from "react";
import {
  addPlugin,
  listPlugins,
  removePlugin,
  restartDaemon,
  setPluginEnabled,
  waitForDaemon,
} from "./chat/api";
import type { Plugin, PluginList } from "./chat/api";

type RestartState = "idle" | "restarting" | "done" | "failed";

/** Dot colour + tooltip for a plugin's health, which is not the same as enabled. */
function statusOf(p: Plugin): { cls: string; text: string } {
  if (p.status === "missing") return { cls: "missing", text: "Directory not found" };
  if (p.status === "error") return { cls: "error", text: "Failed to load" };
  if (!p.enabled) return { cls: "disabled", text: "Disabled" };
  return { cls: "ok", text: "Enabled" };
}

function Badges({ plugin }: { plugin: Plugin }) {
  const badges: string[] = [];
  if (plugin.channels.length) badges.push("channel");
  if (plugin.skill_dirs.length) badges.push("skill");
  if (plugin.cli_entry) badges.push("cli");
  if (!badges.length) return null;
  return (
    <span className="plugin-badges">
      {badges.map((b) => (
        <span key={b} className={`plugin-badge ${b}`}>
          {b}
        </span>
      ))}
    </span>
  );
}

function AddPluginForm({ onAdded }: { onAdded: () => void }) {
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = () => {
    const trimmed = path.trim();
    if (!trimmed) return;
    setBusy(true);
    setError(null);
    addPlugin(trimmed)
      .then(() => {
        setPath("");
        onAdded();
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };

  return (
    <div className="plugin-add">
      <input
        placeholder="Path to a plugin directory (containing yuki-plugin.yaml)"
        value={path}
        onChange={(e) => setPath(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
        disabled={busy}
      />
      <button onClick={submit} disabled={busy || !path.trim()}>
        Add
      </button>
      {error && <div className="plugin-error-line">{error}</div>}
    </div>
  );
}

function PluginDetail({
  plugin,
  onChanged,
  onBack,
}: {
  plugin: Plugin;
  onChanged: () => void;
  onBack: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    fn()
      .then(onChanged)
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };

  const status = statusOf(plugin);

  return (
    <div className="plugin-detail">
      <button className="back-btn" onClick={onBack}>
        &larr; Back
      </button>
      <div className="title-row">
        <h2>{plugin.name}</h2>
        <span className={`alive-dot ${status.cls}`} title={status.text} />
      </div>
      <p className="plugin-description">{plugin.description || "No description."}</p>

      <dl>
        <dt>Status</dt>
        <dd>{status.text}</dd>
        <dt>Source</dt>
        <dd>{plugin.source}</dd>
        {plugin.version && (
          <>
            <dt>Version</dt>
            <dd>{plugin.version}</dd>
          </>
        )}
        {plugin.path && (
          <>
            <dt>Path</dt>
            <dd>
              <code>{plugin.path}</code>
            </dd>
          </>
        )}
        <dt>Channels</dt>
        <dd>{plugin.channels.join(", ") || "—"}</dd>
        <dt>Skills</dt>
        <dd>
          {plugin.skill_dirs.length ? (
            plugin.skill_dirs.map((d) => <code key={d}>{d}</code>)
          ) : (
            "—"
          )}
        </dd>
        <dt>CLI</dt>
        <dd>{plugin.cli_entry ? <code>{plugin.cli_entry}</code> : "—"}</dd>
      </dl>

      {plugin.error && <pre className="plugin-error-box">{plugin.error}</pre>}

      <div className="plugin-actions">
        <button
          className="action-btn"
          disabled={busy || plugin.source === "entry_point"}
          title={
            plugin.source === "entry_point"
              ? "Installed packages are always on; register it by path to toggle it"
              : undefined
          }
          onClick={() => act(() => setPluginEnabled(plugin.name, !plugin.enabled))}
        >
          {plugin.enabled ? "Disable" : "Enable"}
        </button>
        {!plugin.builtin && plugin.source !== "entry_point" && (
          <button
            className="action-btn delete-btn"
            disabled={busy}
            onClick={() => act(() => removePlugin(plugin.name))}
          >
            Remove
          </button>
        )}
      </div>
      {error && <div className="plugin-error-line">{error}</div>}
    </div>
  );
}

export default function PluginsView() {
  const [data, setData] = useState<PluginList | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [restart, setRestart] = useState<RestartState>("idle");
  const [restartError, setRestartError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    listPlugins()
      .then(setData)
      .catch((e: Error) => setRestartError(e.message));
  }, []);

  useEffect(refresh, [refresh]);

  const doRestart = () => {
    setRestart("restarting");
    setRestartError(null);
    restartDaemon()
      .then(() => waitForDaemon())
      .then(() => {
        setRestart("done");
        refresh();
      })
      .catch((e: Error) => {
        setRestart("failed");
        setRestartError(e.message);
      });
  };

  const plugins = data?.plugins ?? [];
  const active = plugins.find((p) => p.name === selected) ?? null;

  return (
    <div className={`app plugins-view ${selected ? "detail-open" : ""}`}>
      <aside className="sidebar">
        <div className="sidebar-header">
          <h2>Plugins</h2>
        </div>

        <div className="plugin-toolbar">
          <button
            className="action-btn restart-btn"
            onClick={doRestart}
            disabled={restart === "restarting"}
          >
            {restart === "restarting" ? "Restarting…" : "Restart daemon"}
          </button>
          {data?.restart_required && restart !== "restarting" && (
            <div className="plugin-banner">
              Changes take effect after a restart.
            </div>
          )}
          {restart === "done" && <div className="plugin-banner ok">Daemon is back up.</div>}
          {restart === "failed" && (
            <div className="plugin-banner error">
              Restart may have failed — check <code>daemon.log</code>.
              {restartError ? ` (${restartError})` : ""}
            </div>
          )}
          {data?.channels_override && (
            <div className="plugin-banner warn">
              Channels overridden by <code>CHANNELS</code>:{" "}
              {data.channels_override.join(", ") || "(none)"}
            </div>
          )}
        </div>

        <AddPluginForm onAdded={refresh} />

        <ul className="plugin-list">
          {plugins.map((p) => {
            const status = statusOf(p);
            const broken = p.status !== "ok";
            return (
              <li
                key={p.name}
                className={`${p.name === selected ? "active" : ""} ${
                  broken ? "broken" : ""
                } ${!p.enabled ? "off" : ""}`}
                onClick={() => setSelected(p.name)}
              >
                <span className="plugin-row">
                  <span className={`alive-dot ${status.cls}`} title={status.text} />
                  <span className="plugin-name">{p.name}</span>
                  <Badges plugin={p} />
                </span>
                <span className="plugin-sub">
                  {broken ? p.error : p.description || p.source}
                </span>
              </li>
            );
          })}
          {data && !plugins.length && (
            <li className="placeholder">No plugins registered.</li>
          )}
        </ul>
      </aside>

      <main className="content">
        {active ? (
          <PluginDetail
            plugin={active}
            onChanged={refresh}
            onBack={() => setSelected(null)}
          />
        ) : (
          <p className="placeholder">Select a plugin</p>
        )}
      </main>
    </div>
  );
}
