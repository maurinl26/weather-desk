# PWA Weather Desk

L'application est installable comme PWA (écran d'accueil mobile / bureau Chrome).

- `manifest.webmanifest` — manifeste (nom, `display: standalone`, couleurs, icônes
  192/512 + maskable). Sert aussi de source de vérité pour le `theme_color`.
- `sw.js` — service worker minimal : cache-first sur les ressources statiques de la
  même origine (JS/CSS Panel, icônes), aucune interception des navigations, des
  websocket ni des données météo distantes.
- `sw-register.js` — enregistre `/pwa/sw.js` au chargement de la page.
- `icon-*.png` — icônes générées (style Galerne : fond océan sombre, flèches de vent
  turquoise).

Le dossier est servi par Panel via `--static-dirs pwa=weather_desk/pwa`, donc à
l'URL racine `/pwa/...`. Le manifeste est branché dans `ui.py` (paramètres
`manifest` et `favicon` du template) et l'enregistrement du SW via
`config.js_files`.
