export interface Conversation {
  id: string;
  platform: string;
  title: string | null;
  project: string | null;
  claude_session_id: string | null;
  created_at: number;
  updated_at: number;
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
  /** Client-only delivery state for optimistic user messages. Absent on
   *  server-confirmed messages (treated as delivered). */
  status?: "sending" | "failed";
}

/** One intermediate step of a running Claude turn. Memory-only — never persisted. */
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
  | { type: "reload" };

export async function listConversations(): Promise<Conversation[]> {
  const r = await fetch("/api/conversations?platform=web");
  if (!r.ok) throw new Error("Failed to list conversations");
  return r.json();
}

export async function createConversation(title?: string, project?: string): Promise<Conversation> {
  const r = await fetch("/api/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: title ?? null, project: project ?? null }),
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
