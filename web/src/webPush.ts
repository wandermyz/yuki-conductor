/**
 * Browser push notifications. The service worker (public/sw.js) shows them;
 * this module registers it and manages this browser's subscription with the
 * daemon. Push needs a secure context: HTTPS (e.g. tailscale serve) or localhost.
 */

export type PushState = "unsupported" | "denied" | "off" | "on";

export function pushSupported(): boolean {
  return (
    window.isSecureContext &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

/** Register the service worker and route notification clicks to the app. */
export function initServiceWorker(): void {
  if (!pushSupported()) return;
  navigator.serviceWorker.register("/sw.js").catch(console.error);
  navigator.serviceWorker.addEventListener("message", (e) => {
    if (e.data?.type === "open-conversation") {
      window.location.hash = `chat/${e.data.conversation_id}`;
    }
  });
  // Re-send an existing subscription so the daemon still has it even if its
  // database was reset since this browser subscribed.
  currentSubscription()
    .then((sub) => sub && saveSubscription(sub))
    .catch(console.error);
}

async function currentSubscription(): Promise<PushSubscription | null> {
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

async function saveSubscription(sub: PushSubscription): Promise<void> {
  const r = await fetch("/api/webpush/subscriptions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sub.toJSON()),
  });
  if (!r.ok) throw new Error("Failed to save push subscription");
}

export async function getPushState(): Promise<PushState> {
  if (!pushSupported()) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  return (await currentSubscription()) ? "on" : "off";
}

function base64UrlToBytes(b64: string): Uint8Array<ArrayBuffer> {
  const padded = (b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(padded);
  const out = new Uint8Array(new ArrayBuffer(bin.length));
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Ask for permission and subscribe. Must run from a user gesture. */
export async function enablePush(): Promise<PushState> {
  if ((await Notification.requestPermission()) !== "granted") return getPushState();
  const r = await fetch("/api/webpush/key");
  if (!r.ok) throw new Error("Failed to fetch push key");
  const { public_key } = await r.json();
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: base64UrlToBytes(public_key),
  });
  await saveSubscription(sub);
  return "on";
}

export async function disablePush(): Promise<PushState> {
  const sub = await currentSubscription();
  if (sub) {
    await fetch("/api/webpush/unsubscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint: sub.endpoint }),
    });
    await sub.unsubscribe();
  }
  return "off";
}
