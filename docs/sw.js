// Minimal service worker: caches only the static app shell (HTML/CSS/JS/icons)
// for offline/fast loading. Deliberately does NOT touch requests to
// api.github.com or raw.githubusercontent.com — house data must always be
// fetched fresh, never served from a cache, or the app would show stale
// listings/prices with no way for the user to know.
const CACHE_NAME = "house-tracker-shell-v1";
const SHELL_FILES = [
  "index.html",
  "alert.html",
  "add.html",
  "style.css",
  "manifest.json",
  "icon-192.png",
  "icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return; // never touch GitHub API calls
  if (event.request.method !== "GET") return;

  // Network-first so shell updates (new features, bug fixes) show up
  // immediately on next load instead of being stuck on a stale cache;
  // falls back to cache only when actually offline.
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        const clone = res.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        return res;
      })
      .catch(() => caches.match(event.request))
  );
});
