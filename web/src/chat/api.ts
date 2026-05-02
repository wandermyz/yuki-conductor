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
  | { type: "message"; message: ChatMessage }
  | { type: "processing"; on: boolean; message_id: string }
  | { type: "title"; title: string };

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

export function openConversationSocket(
  convId: string,
  onEvent: (e: WSEvent) => void,
  opts: { onOpen?: () => void; onClose?: () => void } = {},
): WebSocket {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(
    `${proto}://${window.location.host}/ws/conversations/${encodeURIComponent(convId)}`,
  );
  ws.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data) as WSEvent;
      onEvent(data);
    } catch {
      /* ignore */
    }
  };
  if (opts.onOpen) ws.onopen = opts.onOpen;
  if (opts.onClose) ws.onclose = opts.onClose;
  return ws;
}
