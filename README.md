# Weather Desk

Poste personnel d'analyse météo **Panel + Bokeh** : vapeur d'eau EUMETSAT, champs IFS géoréférencés, tracés de fronts et bulletin Markdown traçable.

## Démarrage

```bash
cd ~/weather-desk
uv sync --locked
uv run panel serve app.py --address 127.0.0.1 --port 5006 \
  --allow-websocket-origin 127.0.0.1:5006 --show
```

Ouvrir <http://127.0.0.1:5006/app>. Python 3.12 ; versions Python fixées dans `uv.lock`. **ecCodes natif doit être installé** pour le décodage GRIB (`brew install eccodes` sur macOS si nécessaire). Le poste de développement dispose d'ecCodes 2.47.0. Sans uv, installer `requirements.txt` dans un environnement Python 3.12.

Serveur local sans authentification. Pour consulter le serveur du Mac mini à distance, utiliser un tunnel SSH privé ; ne pas exposer directement le port.

## Carte synoptique

Les couches chargent automatiquement en arrière-plan à l'ouverture :

- **Vapeur d'eau** : SEVIRI WV6.2 µm, EUMETSAT EUMETView WMS. Le catalogue fournit les heures disponibles, puis le serveur demande les images en EPSG:3857, projection de la carte. Dernière image affichée d'abord, puis préchargement de six images au maximum, nominalement espacées de 15 minutes. Si une image manque, la séquence disponible reste utilisable et le manque est signalé.
- **IFS Open Data 0,25°** : isobares au niveau de la mer (blanc, hPa, pas de 4) et hauteur géopotentielle à 500 hPa (jaune pointillé, dam, pas de 6). La valeur s'affiche au survol d'un contour. Choix du run sur les dernières 24 h et de l'échéance de 0 à 72 h par pas de 3 h ; les runs proposés sont des candidats, validés lors du téléchargement.
- **Domaine** : Atlantique / Europe, 35°W–45°E et 25–70°N. Hors de cette emprise, seuls le fond de carte et les tracés sont disponibles. Zoomer ne demande pas de nouvelles données ni n'augmente leur résolution.

Les heures satellite, run IFS et validité IFS sont affichées séparément, avec leur décalage. L'échéance IFS initiale est choisie au plus près de l'image satellite disponible (ou de l'heure courante). **+0 h correspond au champ initial du produit de prévision IFS**, pas à une réanalyse. WV est un aperçu radiométrique fourni par EUMETView ; il ne sert pas à mesurer directement une température de brillance.

Utiliser les boutons d'actualisation pour récupérer de nouvelles données. Les couches ne changent pas automatiquement de run pendant une analyse. Les contrôles d'opacité, visibilité et animation agissent directement dans le navigateur. Le changement d'échéance IFS ne déclenche un téléchargement qu'au relâchement du curseur.

## Analyse et bulletin

1. Examiner les couches, leur validité et leur fraîcheur ; ajuster run, échéance et opacité.
2. Arrêter l'animation sur l'image à analyser et renseigner l'échéance de l'analyse dans le contexte.
3. Choisir un outil dans la barre de carte (survol pour le libellé) : front froid, chaud, occlusion ou zone. Faire un **appui prolongé** pour commencer, cliquer pour poser des sommets, puis un appui prolongé pour terminer. Avec l'outil d'édition, un appui prolongé sur le tracé affiche ses sommets. Pour supprimer un tracé, le sélectionner puis Retour arrière, pointeur sur la carte.
4. Rédiger situation, analyse, impacts et incertitudes.
5. Exporter le ZIP complet ou le Markdown, GeoJSON et manifeste séparément.

Les zooms, les changements de couches et la rédaction conservent les annotations. Les fronts sont actuellement des lignes colorées, sans symboles météorologiques conventionnels. L'échéance globale s'applique à tous les tracés.

Le volet « Images de référence » conserve les imports PNG/JPEG/WebP et URL du prototype. Ces imports sont **non géoréférencés** ; les deux sources opérationnelles sur la carte le sont. Limites d'import : 15 Mo et 20 millions de pixels. Les URL de référence sont chargées par le navigateur ; importer le fichier pour en archiver le contenu.

**Exporter avant de fermer ou recharger la session : pas encore de sauvegarde automatique ni de réimport d'analyse.** Les fichiers météo en cache ne constituent pas une sauvegarde du bulletin ou des tracés.

## Fluidité et cache

- Acquisition et décodage dans quatre workers au maximum ; la boucle UI reste disponible.
- Images d'animation transférées une fois par séquence ; seules les sélections changent ensuite.
- Isolignes vectorielles calculées une fois par champ ; pas de recalcul au zoom ou lors de la rédaction.
- Résultats d'une ancienne demande ignorés si l'utilisateur a changé de run/échéance.
- Dernières données valides conservées lors d'une erreur ; état d'erreur visible.
- Cache disque `data/cache/`, vérifié par SHA-256, écrit atomiquement, purgé à 72 h / 512 Mio lors des acquisitions. Catalogue satellite valable 2 min, recherche du dernier run IFS 15 min ; champs horodatés immuables. Huit échéances IFS au maximum en mémoire.
- Timeouts et reprises réseau bornés ; téléchargement des seuls messages GRIB MSL et GH500, sans modèle complet ni GPU.

Le premier chargement dépend des fournisseurs. `scripts/check_sources.py` mesure le chargement réel et la relecture en cache sur le poste ; il ne constitue pas une garantie de latence réseau.

## Exports et provenance

Le ZIP contient `bulletin.md`, `annotations.geojson`, `manifest.json`, les images de référence importées, ainsi que les données réellement affichées :

- `live/satellite.png` : image satellite sélectionnée, avec projection et emprise dans le manifeste ;
- `live/ifs-msl.grib2` et `live/ifs-gh.grib2` : champs sources du run et de l'échéance affichés ;
- `live/ifs-contours.json` : contours projetés en EPSG:3857, unités hPa et dam.

Le manifeste conserve les checksums des fichiers (hors manifeste lui-même), la provenance, les échéances, l'opacité, les couches visibles et l'emprise de la vue. L'archive capture l'image satellite sélectionnée, pas toute l'animation. Si un téléchargement est en cours, l'export décrit les données encore affichées. Les annotations GeoJSON utilisent longitude/latitude WGS84. Run, validité, paramètre, niveau et unités IFS sont contrôlés dans le GRIB. Les métadonnées des références importées restent déclaratives.

## Organisation et vérification

- `app.py` : entrée Panel, analyse isolée par session et démarrage des chargements.
- `weather_desk/data.py` : sources publiques, cache, décodage GRIB, contours et projection.
- `weather_desk/live.py` : orchestration des workers et couches Bokeh.
- `weather_desk/ui.py` : annotations, rédaction et sources de référence.
- `weather_desk/analysis.py` : exports et bulletin indépendants de Panel.

```bash
uv run pytest -q                         # sans réseau
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_sources.py   # test volontaire des fournisseurs réels
# Avec Playwright installé et le serveur local lancé :
WEATHER_DESK_URL=http://127.0.0.1:5006/app node scripts/check_browser.cjs
```

Le test navigateur accepte `WEATHER_DESK_PLAYWRIGHT` comme chemin vers un module Playwright déjà installé. Le client IFS de Galerne existant importe aussi sa pile d'apprentissage ; cette application utilise donc directement le même client officiel `ecmwf-opendata`, derrière un adaptateur léger. Aucun code de routage ou d'assurance n'est importé. Hermes reste à connecter.

Sources : [EUMETView WMS](https://user.eumetsat.int/data-access/eumetview/resources), [ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data), [client ECMWF](https://github.com/ecmwf/ecmwf-opendata). Attribution : EUMETSAT ; ECMWF (CC BY 4.0) ; fond © OpenStreetMap contributors.

Voir [PLAN.md](PLAN.md) et [OPEN_SOURCE_REVIEW.md](OPEN_SOURCE_REVIEW.md) pour la suite et les choix de composants.
