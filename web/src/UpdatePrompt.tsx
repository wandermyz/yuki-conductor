import { useCallback, useEffect, useState } from "react";
import { checkForUpdate, runUpdate } from "./chat/api";
import type { UpdateStatus } from "./chat/api";

const POLL_MS = 5 * 60 * 1000;
const SNOOZE_MS = 60 * 60 * 1000;
const SNOOZE_KEY = "yuki-update-snoozed-until";

function snoozed(): boolean {
  return Number(window.localStorage.getItem(SNOOZE_KEY) ?? 0) > Date.now();
}

/**
 * Polls /api/update and asks whether to update when upstream has moved and
 * no agent run is in flight.
 */
export default function UpdatePrompt() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const poll = useCallback(() => {
    if (snoozed()) return;
    checkForUpdate()
      // Updating restarts the daemon, killing any chat, task or automation
      // run in flight, so wait for an idle poll before asking.
      .then((s) =>
        setStatus(s.behind > 0 && Object.keys(s.running).length === 0 ? s : null),
      )
      // A checkout that can't update (no upstream, offline) isn't worth a
      // popup every five minutes; the Plugins tab's Update button reports it.
      .catch((e: Error) => console.warn("Update check failed:", e.message));
  }, []);

  useEffect(() => {
    poll();
    const timer = window.setInterval(poll, POLL_MS);
    return () => window.clearInterval(timer);
  }, [poll]);

  if (!status) return null;

  // Cancel just closes the modal; the next poll brings it back.
  const cancel = () => {
    setStatus(null);
    setError(null);
  };

  const snooze = () => {
    window.localStorage.setItem(SNOOZE_KEY, String(Date.now() + SNOOZE_MS));
    cancel();
  };

  const update = () => {
    setBusy(true);
    setError(null);
    runUpdate()
      .then((restarted) => {
        if (!restarted) setStatus(null);
      })
      .catch((e: Error) => {
        setError(e.message);
        setBusy(false);
      });
  };

  const { behind, commits, dirty, upstream } = status;
  const blocked = dirty.length > 0;

  return (
    <div className="modal-overlay">
      <div className="modal-content update-prompt">
        <h3 className="modal-title">
          Update available — {behind} new commit{behind === 1 ? "" : "s"} on{" "}
          <code>{upstream}</code>
        </h3>
        <ul className="update-commits">
          {commits.map((c) => (
            <li key={c.sha}>
              <code>{c.short}</code> {c.subject}
            </li>
          ))}
          {behind > commits.length && (
            <li className="update-more">…and {behind - commits.length} more</li>
          )}
        </ul>
        <p className="update-note">
          Updating rebases this checkout onto <code>{upstream}</code> and restarts
          the daemon.
        </p>
        {blocked && (
          <div className="plugin-banner error">
            Local changes block the update; commit or discard them first:{" "}
            {dirty.join(", ")}
          </div>
        )}
        {error && <div className="plugin-banner error">{error}</div>}
        <div className="modal-actions update-actions">
          <button className="modal-close-btn" onClick={cancel} disabled={busy}>
            Cancel
          </button>
          <button className="modal-close-btn" onClick={snooze} disabled={busy}>
            Snooze 1 hour
          </button>
          <button
            className="action-btn restart-btn"
            onClick={update}
            disabled={busy || blocked}
          >
            {busy ? "Updating…" : "Update & restart"}
          </button>
        </div>
      </div>
    </div>
  );
}
