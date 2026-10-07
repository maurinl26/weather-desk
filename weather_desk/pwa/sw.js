/* Service worker Weather Desk : cache lecture-seule de l'app shell.
   Stratégies :
   - navigations (documents Panel) : jamais interceptées, elles restent fraîches ;
   - requêtes GET même origine (JS/CSS Bokeh, icônes) : cache-first puis réseau,
     la réponse est mise en cache pour un rechargement hors-ligne ultérieur ;
   - websocket, POST et données météo distantes : hors du scope, jamais touchées. */
const CACHE = "weather-desk-shell-v1";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return; // données météo : réseau direct
  if (request.mode === "navigate") return; // pages Panel : toujours fraîches

  event.respondWith(
    (async () => {
      const cached = await caches.match(request);
      if (cached) return cached;
      const response = await fetch(request);
      if (response.ok) {
        const cache = await caches.open(CACHE);
        cache.put(request, response.clone());
      }
      return response;
    })()
  );
});
