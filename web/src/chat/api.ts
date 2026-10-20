export interface Conversation {
  id: string;
  platform: string;
  title: string | null;
  project: string | null;
  claude_session_id: string | null;
  /** Agent harness, fixed when the conversation is created. */
  harness: string;
  created_at: number;
  updated_at: number;
}

export interface HarnessInfo {
  name: string;
  label: string;
  /** Its CLI is on the daemon's PATH. */
  available: boolean;
}

export async function listHarnesses(): Promise<{ default: string; harnesses: HarnessInfo[] }> {
  const r = await fetch("/api/harnesses");
  if (!r.ok) throw new Error("Failed to list harnesses");
  return r.json();
}

export interface AttachmentRef {
  id: string;
  filename: string;
  url: string;
  mime_type: string | null;
}

export interface ChatMessage {
  id: string;
  conversation_id: string;
  role: "user" | "assistant";
  text: string;
  attachments: AttachmentRef[];
  created_at: number;
  /** How full the model's context was at the end of the turn that produced
   *  this message, and the window it was measured against. Only assistant
   *  messages carry them; absent on the optimistic ones the client draws
   *  before the server confirms. */
  context_tokens?: number | null;
  context_window?: number | null;
  /** Client-only delivery state for optimistic user messages. Absent on
   *  server-confirmed messages (treated as delivered). */
  status?: "sending" | "failed";
}

/** One intermediate step of a running agent turn. Memory-only — never persisted. */
export interface StreamStep {
  kind: "init" | "text" | "thinking" | "tool_use" | "tool_result";
  label: string;
  detail: Record<string, unknown>;
  seq: number;
}

export type ConvStatus = "unread" | "read" | "done";

export type WSEvent =
  | {
      type: "message";
      conversation_id: string;
      message: ChatMessage;
      /** Out-of-band push (`yuki-conductor send`) with no surrounding turn. */
      pushed?: boolean;
    }
  | { type: "processing"; conversation_id: string; on: boolean; message_id: string }
  | { type: "step"; conversation_id: string; step: StreamStep }
  | { type: "title"; conversation_id: string; title: string }
  | { type: "status"; conversation_id: string; status: ConvStatus }
  /** A taskboard task changed (status, fields or comments): refetch it. */
  | { type: "task"; id: number }
  | { type: "reload" };

export async function listConversations(): Promise<Conversation[]> {
  const r = await fetch("/api/conversations?platform=web");
  if (!r.ok) throw new Error("Failed to list conversations");
  return r.json();
}

/** One conversation matching a search: its best-matching message. */
export interface SearchHit {
  conversation_id: string;
  title: string | null;
  message_id: string;
  /** Excerpt of that message; each matched term is wrapped in \x02…\x03. */
  snippet: string;
  score: number;
}

export async function searchConversations(q: string): Promise<SearchHit[]> {
  const r = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
  if (!r.ok) throw new Error("Search failed");
  return r.json();
}

export async function createConversation(
  title?: string,
  project?: string,
  harness?: string,
): Promise<Conversation> {
  const r = await fetch("/api/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: title ?? null, project: project ?? null, harness: harness ?? null }),
  });
  if (!r.ok) throw new Error("Failed to create conversation");
  return r.json();
}

export async function deleteConversation(id: string): Promise<void> {
  const r = await fetch(`/api/conversations/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!r.ok) throw new Error("Failed to delete conversation");
}

export async function archiveConversation(id: string): Promise<void> {
  const r = await fetch(`/api/conversations/${encodeURIComponent(id)}/archive`, {
    method: "POST",
  });
  if (!r.ok) throw new Error("Failed to archive conversation");
}

export async function updateConversation(
  id: string,
  patch: { title?: string; project?: string },
): Promise<Conversation> {
  const r = await fetch(`/api/conversations/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!r.ok) throw new Error("Failed to update conversation");
  return r.json();
}

export async function listMessages(convId: string): Promise<ChatMessage[]> {
  const r = await fetch(
    `/api/conversations/${encodeURIComponent(convId)}/messages?limit=200`,
  );
  if (!r.ok) throw new Error("Failed to list messages");
  return r.json();
}

export async function sendMessage(
  convId: string,
  text: string,
  attachmentIds: string[] = [],
): Promise<ChatMessage> {
  const r = await fetch(
    `/api/conversations/${encodeURIComponent(convId)}/messages`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, attachment_ids: attachmentIds }),
    },
  );
  if (!r.ok) throw new Error("Failed to send message");
  return r.json();
}

export async function uploadFile(file: File): Promise<AttachmentRef> {
  const fd = new FormData();
  fd.append("file", file);
  const r = await fetch("/api/uploads", { method: "POST", body: fd });
  if (!r.ok) throw new Error("Failed to upload file");
  return r.json();
}

export async function fetchStatuses(): Promise<Record<string, string>> {
  const r = await fetch("/api/chat/conversations/statuses");
  if (!r.ok) throw new Error("Failed to fetch statuses");
  return r.json();
}

export async function setConvStatus(convId: string, status: string): Promise<void> {
  const r = await fetch(`/api/chat/conversations/${encodeURIComponent(convId)}/status`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!r.ok) throw new Error("Failed to set status");
}

export async function cancelProcessing(convId: string): Promise<void> {
  const r = await fetch(`/api/conversations/${encodeURIComponent(convId)}/cancel`, {
    method: "POST",
  });
  if (!r.ok) throw new Error("Failed to cancel");
}

/** Returns map of conversation_id -> message_id for conversations currently being processed. */
export async function fetchProcessing(): Promise<Record<string, string>> {
  const r = await fetch("/api/chat/processing");
  if (!r.ok) throw new Error("Failed to fetch processing state");
  return r.json();
}

/**
 * Buffered intermediate steps for every in-flight conversation. Called on
 * (re)connect so the step list resumes instead of starting blank mid-run.
 */
export async function fetchSteps(): Promise<Record<string, StreamStep[]>> {
  const r = await fetch("/api/chat/steps");
  if (!r.ok) throw new Error("Failed to fetch steps");
  return r.json();
}

export interface ConversationUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number | null;
}

export async function fetchAllUsage(): Promise<Record<string, ConversationUsage>> {
  const r = await fetch("/api/chat/conversations/usage");
  if (!r.ok) throw new Error("Failed to fetch usage");
  return r.json();
}

export interface Project {
  name: string;
  path: string;
}

export async function listProjects(): Promise<Project[]> {
  const r = await fetch("/api/projects");
  if (!r.ok) throw new Error("Failed to list projects");
  return r.json();
}

export async function addProject(name: string, path: string): Promise<void> {
  const r = await fetch("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, path }),
  });
  if (!r.ok) throw new Error("Failed to add project");
}

export async function removeProject(name: string): Promise<void> {
  const r = await fetch(`/api/projects/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!r.ok) throw new Error("Failed to remove project");
}

export async function reorderProjects(names: string[]): Promise<void> {
  const r = await fetch("/api/projects/order", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ names }),
  });
  if (!r.ok) throw new Error("Failed to reorder projects");
}

export interface BrowseResult {
  path: string;
  parent: string | null;
  dirs: { name: string; path: string }[];
}

export async function browseDirectory(path: string = ""): Promise<BrowseResult> {
  const r = await fetch(`/api/browse?path=${encodeURIComponent(path)}`);
  if (!r.ok) throw new Error("Failed to browse directory");
  return r.json();
}

export interface CronRun {
  id: number;
  task_name: string;
  trigger: "schedule" | "manual";
  status: "running" | "success" | "error";
  started_at: number;
  finished_at: number | null;
  response: string | null;
  error: string | null;
  notified: boolean;
  session_id: string | null;
  conversation_id: string | null;
}

export interface Automation {
  name: string;
  label: string;
  display_name: string | null;
  description: string;
  schedule: string;
  schedule_text: string;
  next_runs: number[];
  prompt: string;
  chat_app: string | null;
  origin_conversation: string | null;
  paused: boolean;
  /** False once the task is gone from cron.yaml and only history remains. */
  active: boolean;
  last_run: CronRun | null;
  running: boolean;
  /** Only present on the single-automation endpoint. */
  runs?: CronRun[];
}

export async function listAutomations(): Promise<Automation[]> {
  const r = await fetch("/api/automations");
  if (!r.ok) throw new Error("Failed to list automations");
  return r.json();
}

export async function getAutomation(name: string): Promise<Automation> {
  const r = await fetch(`/api/automations/${encodeURIComponent(name)}`);
  if (!r.ok) throw new Error("Failed to load automation");
  return r.json();
}

export async function renameAutomation(
  name: string,
  displayName: string,
): Promise<Automation> {
  return patchAutomation(name, { display_name: displayName });
}

/** Pause or resume an automation's schedule. Manual runs work either way. */
export async function setAutomationPaused(
  name: string,
  paused: boolean,
): Promise<Automation> {
  return patchAutomation(name, { paused });
}

async function patchAutomation(
  name: string,
  body: { display_name?: string; paused?: boolean },
): Promise<Automation> {
  const r = await fetch(`/api/automations/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error("Failed to update automation");
  return r.json();
}

export async function runAutomation(name: string): Promise<void> {
  const r = await fetch(`/api/automations/${encodeURIComponent(name)}/run`, {
    method: "POST",
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => null);
    throw new Error(detail?.detail || "Failed to run automation");
  }
}

/**
 * Module-level singleton WebSocket that stays alive across component
 * mount/unmount cycles (e.g. tab switches). Subscribers are notified of
 * events, open, and close. The connection is established on first subscribe
 * and kept open as long as at least one subscriber exists.
 */

export interface ChatSocketSubscriber {
  onEvent: (e: WSEvent) => void;
  onOpen?: () => void;
  onClose?: () => void;
}

const subscribers = new Set<ChatSocketSubscriber>();
let ws: WebSocket | null = null;
let attempt = 0;
let connected = false;

const BASE_DELAY = 1000;
const MAX_DELAY = 30000;

function wsConnect() {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${window.location.host}/ws/chat`);

  ws.onopen = () => {
    attempt = 0;
    connected = true;
    for (const sub of subscribers) sub.onOpen?.();
  };

  ws.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data) as WSEvent;
      for (const sub of subscribers) sub.onEvent(data);
    } catch {
      /* ignore */
    }
  };

  ws.onclose = () => {
    connected = false;
    for (const sub of subscribers) sub.onClose?.();
    const delay = Math.min(BASE_DELAY * 2 ** attempt, MAX_DELAY);
    attempt++;
    setTimeout(wsConnect, delay);
  };
}

/**
 * Subscribe to the shared chat WebSocket. The connection is created lazily on
 * first subscribe. Returns an unsubscribe function — call it on unmount.
 * New subscribers that join while the socket is already open get an immediate
 * onOpen callback so they can sync state.
 */
export function subscribeChatSocket(sub: ChatSocketSubscriber): () => void {
  subscribers.add(sub);

  // Start the singleton connection on first subscriber
  if (subscribers.size === 1 && !ws) {
    wsConnect();
  }

  // If already connected, notify the new subscriber immediately
  if (connected) {
    sub.onOpen?.();
  }

  return () => {
    subscribers.delete(sub);
  };
}

// --------------------------------------------------------------------------
// Plugins
// --------------------------------------------------------------------------

export interface Plugin {
  name: string;
  description: string;
  version: string | null;
  source: "builtin" | "path" | "entry_point";
  path: string | null;
  enabled: boolean;
  builtin: boolean;
  channels: string[];
  skill_dirs: string[];
  cli_entry: string | null;
  status: "ok" | "missing" | "error";
  error: string | null;
}

export interface PluginList {
  plugins: Plugin[];
  /** Channel names the running daemon actually has up. */
  active_channels: string[];
  /** Non-null when the CHANNELS env var is overriding the registry. */
  channels_override: string[] | null;
  restart_required: boolean;
}

export async function listPlugins(): Promise<PluginList> {
  const r = await fetch("/api/plugins");
  if (!r.ok) throw new Error("Failed to list plugins");
  return r.json();
}

async function pluginError(r: Response): Promise<never> {
  let detail = `Request failed (${r.status})`;
  try {
    const body = await r.json();
    if (body?.detail) detail = body.detail;
  } catch {
    /* keep the status-code message */
  }
  throw new Error(detail);
}

export async function addPlugin(path: string): Promise<{ name: string }> {
  const r = await fetch("/api/plugins", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!r.ok) return pluginError(r);
  return r.json();
}

export async function setPluginEnabled(
  name: string,
  enabled: boolean,
): Promise<void> {
  const r = await fetch(`/api/plugins/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
  });
  if (!r.ok) return pluginError(r);
}

export async function removePlugin(name: string): Promise<void> {
  const r = await fetch(`/api/plugins/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!r.ok) return pluginError(r);
}

/**
 * Ask the daemon to restart itself. Returns as soon as the detached restarter
 * is spawned — the daemon is still up at that point, so callers must pass the
 * returned boot id to `waitForDaemon` to find out when the new one is serving.
 */
export async function restartDaemon(): Promise<string> {
  const r = await fetch("/api/daemon/restart", { method: "POST" });
  if (!r.ok) return pluginError(r);
  return (await r.json()).boot_id;
}

export interface UpdateCommit {
  sha: string;
  short: string;
  author: string;
  date: string;
  subject: string;
}

export interface UpdateStatus {
  branch: string;
  upstream: string;
  current: string;
  latest: string;
  behind: number;
  ahead: number;
  /** Tracked files with uncommitted changes; updating refuses while any exist. */
  dirty: string[];
  commits: UpdateCommit[];
}

/** What GET /api/update adds: agent runs in flight, by kind ("chat", "task", "automation"). */
export interface UpdateCheck extends UpdateStatus {
  running: Record<string, number>;
}

/** Fetch origin and report what upstream has that this checkout lacks. */
export async function checkForUpdate(): Promise<UpdateCheck> {
  const r = await fetch("/api/update", { cache: "no-store" });
  if (!r.ok) return pluginError(r);
  return r.json();
}

/**
 * Ask before an action that restarts the daemon while agent runs are in
 * flight, since the restart kills them. Proceeds without asking when idle or
 * when the check itself fails (e.g. no upstream).
 */
export async function confirmRestartIfBusy(action: string): Promise<boolean> {
  let running: Record<string, number>;
  try {
    running = (await checkForUpdate()).running;
  } catch {
    return true;
  }
  const parts = Object.entries(running)
    .filter(([, n]) => n > 0)
    .map(([kind, n]) => `${n} ${kind} run${n === 1 ? "" : "s"}`);
  if (parts.length === 0) return true;
  return window.confirm(
    `Agent work in progress: ${parts.join(", ")}.\n\n` +
      `${action} restarts the daemon and will interrupt it. Continue?`,
  );
}

/** Rebase onto upstream and restart. `restarting` is false when already current. */
export async function applyUpdate(): Promise<
  UpdateStatus & { restarting: boolean; boot_id?: string }
> {
  const r = await fetch("/api/update", { method: "POST" });
  if (!r.ok) return pluginError(r);
  return r.json();
}

/**
 * Rebase onto upstream and restart, then reload once the daemon is back so
 * the page picks up the rebuilt frontend. Resolves `false` if there was
 * nothing to update; rejects with the server's message (local changes, a
 * failed rebase, …).
 */
export async function runUpdate(): Promise<boolean> {
  const result = await applyUpdate();
  if (!result.restarting || !result.boot_id) return false;
  await waitForDaemon(result.boot_id);
  window.location.reload();
  return true;
}

/**
 * Resolve once a daemon other than `bootId` answers, or reject after
 * `timeoutMs`. The old daemon keeps answering for a few seconds, and while
 * it's down a proxy (`tailscale serve`) replies 502 rather than failing the
 * request, so neither "a reply" nor "an error" alone says anything — only a
 * new boot id does.
 */
export async function waitForDaemon(bootId: string, timeoutMs = 120000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((res) => setTimeout(res, 2000));
    try {
      const r = await fetch("/api/daemon", { cache: "no-store" });
      if (r.ok && (await r.json()).boot_id !== bootId) return;
    } catch {
      // Down or mid-restart; keep polling.
    }
  }
  throw new Error("Daemon did not come back within the timeout");
}
