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
}

export type WSEvent =
  | { type: "message"; conversation_id: string; message: ChatMessage }
  | { type: "processing"; conversation_id: string; on: boolean; message_id: string }
  | { type: "title"; conversation_id: string; title: string }
  | { type: "reload" };

export async function listConversations(): Promise<Conversation[]> {
  const r = await fetch("/api/conversations?platform=web");
  if (!r.ok) throw new Error("Failed to list conversations");
  return r.json();
}

export async function createConversation(title?: string): Promise<Conversation> {
  const r = await fetch("/api/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: title ?? null }),
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

/**
 * Opens a chat WebSocket with automatic reconnection using exponential backoff.
 * Returns a handle with a `close()` method to permanently disconnect.
 */
export function openChatSocket(
  onEvent: (e: WSEvent) => void,
  opts: { onOpen?: () => void; onClose?: () => void } = {},
): { close: () => void } {
  const BASE_DELAY = 1000;
  const MAX_DELAY = 30000;
  let attempt = 0;
  let ws: WebSocket | null = null;
  let closed = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  function connect() {
    if (closed) return;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${window.location.host}/ws/chat`);

    ws.onopen = () => {
      attempt = 0;
      opts.onOpen?.();
    };

    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as WSEvent;
        onEvent(data);
      } catch {
        /* ignore */
      }
    };

    ws.onclose = () => {
      opts.onClose?.();
      if (closed) return;
      const delay = Math.min(BASE_DELAY * 2 ** attempt, MAX_DELAY);
      attempt++;
      timer = setTimeout(connect, delay);
    };
  }

  connect();

  return {
    close() {
      closed = true;
      if (timer != null) clearTimeout(timer);
      ws?.close();
    },
  };
}
