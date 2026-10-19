// Service worker for browser push notifications (see src/yuki_conductor/web_push.py).
// Served from the site root so its scope covers the whole app.
//
// A push names a target: {kind: "conversation" | "task", id}. That decides
// the URL a click opens and which notifications replace each other.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

function targetHash(target) {
  if (!target) return "";
  return target.kind === "task" ? `#tasks/${target.id}` : `#chat/${target.id}`;
}

function targetUrl(target) {
  return new URL(`/${targetHash(target)}`, self.location.origin).href;
}

self.addEventListener("push", (event) => {
  const data = event.data ? event.data.json() : {};
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      // The user is already looking at it; the page shows its own update.
      // Chrome doesn't penalize a silent push while a window is focused.
      const watching = windows.some(
        (c) => c.focused && c.visibilityState === "visible" && c.url === targetUrl(data.target),
      );
      if (watching) return;
      await self.registration.showNotification(data.title || "yuki-conductor", {
        body: data.body || "",
        icon: "/api/favicon",
        // One notification per conversation or task: a newer one replaces it.
        tag: data.target ? `${data.target.kind}:${data.target.id}` : undefined,
        renotify: true,
        data: { target: data.target },
      });
    })(),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = event.notification.data?.target;
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const existing = windows[0];
      if (existing) {
        await existing.focus();
        // A hash change switches view without reloading the app.
        if (target) existing.postMessage({ type: "open-hash", hash: targetHash(target) });
        return;
      }
      await self.clients.openWindow(target ? targetUrl(target) : "/");
    })(),
  );
});
