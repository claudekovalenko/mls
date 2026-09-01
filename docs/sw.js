// Caches only the static app shell. Database API calls are never cached --
// house data must always be live, or the app would quietly show stale prices
// and verdicts with no way for the user to tell.
const CACHE = "house-finder-v42";
const SHELL = ["index.html", "app.js", "style.css", "manifest.json", "icon-192.png", "icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;  // never touch the database
  if (e.request.method !== "GET") return;
  // Network-first so shell updates land immediately; cache is the offline fallback.
  e.respondWith(
    fetch(e.request).then(res => {
      const copy = res.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy));
      return res;
    }).catch(() => caches.match(e.request))
  );
});
