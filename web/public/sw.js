// Service worker for browser push notifications (see src/yuki_conductor/web_push.py).
// Served from the site root so its scope covers the whole app.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

function chatUrl(conversationId) {
  return new URL(`/#chat/${conversationId}`, self.location.origin).href;
}

self.addEventListener("push", (event) => {
  const data = event.data ? event.data.json() : {};
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      // The user is already reading this conversation; the page plays its own
      // sound. Chrome doesn't penalize a silent push while a window is focused.
      const watching = windows.some(
        (c) => c.focused && c.visibilityState === "visible" && c.url === chatUrl(data.conversation_id),
      );
      if (watching) return;
      await self.registration.showNotification(data.title || "yuki-conductor", {
        body: data.body || "",
        icon: "/api/favicon",
        // One notification per conversation: a newer message replaces the older one.
        tag: data.conversation_id,
        renotify: true,
        data: { conversation_id: data.conversation_id },
      });
    })(),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const conversationId = event.notification.data?.conversation_id;
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const existing = windows[0];
      if (existing) {
        await existing.focus();
        // A hash change switches conversation without reloading the app.
        if (conversationId) existing.postMessage({ type: "open-conversation", conversation_id: conversationId });
        return;
      }
      await self.clients.openWindow(conversationId ? chatUrl(conversationId) : "/");
    })(),
  );
});
