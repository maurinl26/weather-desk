# Weather Desk — spécification produit 2026

État : spécification de référence pour la refonte du poste synoptique.

## But et premier poste cible

Weather Desk devient un poste d’analyse synoptique multi-panneaux : comparer en parallèle des images satellite et des champs de modèles, annoter la situation, puis exporter une image prête à partager.

La première cible est l’écran physiquement relié au Mac mini. L’application reste une application web Panel servie localement, utilisable dans un navigateur plein écran; le même service reste accessible à distance via le tunnel Cloudflare déjà installé. La qualité visuelle prime sur une réduction prématurée de résolution. La fluidité se valide sur l’écran et le navigateur réels du Mac mini.

## Périmètre fonctionnel

### Workspace et panneaux

- Un workspace contient de 1 à 6 panneaux persistants.
- Préréglages de disposition : 1, 2 horizontaux, 2 verticaux, 4 (2×2) et 6 (3×2). Le navigateur peut réduire la grille si sa fenêtre est plus petite que l’écran cible.
- Chaque panneau a un identifiant stable, une source principale, une sélection de produit/champ et ses couches visibles.
- Chaque panneau a son chargement, sa provenance et son erreur; l’échec d’un panneau ne bloque pas les autres.
- La caméra est synchronisée par défaut. L’utilisateur peut la délier pour explorer une vue; un contrôle permet de resynchroniser.
- L’échéance de référence du workspace est commune. Chaque panneau choisit la donnée disponible la plus proche et affiche toujours son heure réelle de validité, son run et son éventuel décalage. Aucune date demandée n’est présentée comme date de la donnée si elle ne correspond pas.
- Les annotations sont partagées par le workspace et exprimées en longitude/latitude WGS84; leur affichage suit la caméra commune.

### Sources météo

- **Satellite** : catalogue EUMETSAT EUMETView WMS. WV6.2 reste le produit initial; sélectionner d’autres couches compatibles et utiliser les heures/cadences de leurs métadonnées.
- **Modèles** : conserver l’ingestion GRIB directe déjà utilisée par IFS/ECMWF Open Data. Aucun Open-Meteo.
- **AROME** : adaptateur prévu sur l’API ciblée officielle de Météo-France. Son service WCS permet de demander des couvertures GRIB avec paramètre, niveau, échéance et emprise choisis; l’accès nécessite un compte et un jeton OAuth2 configuré côté serveur. Le catalogue réel doit déterminer les champs et échéances disponibles, et l’absence de jeton ne doit pas empêcher l’usage d’IFS ou du satellite. Sources : [fiche de l’API AROME](https://www.data.gouv.fr/dataservices/api-modele-arome) et [documentation WCS Météo-France](https://confluence-meteofrance.atlassian.net/wiki/spaces/OpenDataMeteoFrance/pages/854032416/API%2BCibl%2Be%2BMod%2Bles).
- **Champs** : sélectionner dans un catalogue typé selon disponibilité réelle : nom lisible, paramètre/niveau, unité, source, run, validité et emprise. Les champs initiaux restent ceux déjà traités (MSL et GH500); température 2 m, précipitations et vent sont ajoutés si les GRIB et l’adaptateur les prennent en charge.
- Téléchargement, décodage, reprojection et calcul des isolignes restent côté serveur. Le navigateur reçoit des images géoréférencées, des vecteurs et des métadonnées.

### Contrôle par prompts et MCP

- L’interface et MCP agissent sur le même service d’application et le même workspace persistant.
- MCP expose des outils typés pour lire l’état et proposer des changements de disposition, panneau, source, champ, échéance, caméra et export.
- Les commandes qui modifient le workspace sont validées atomiquement : toutes les valeurs doivent être autorisées avant qu’un changement soit appliqué.
- Dans l’interface, le prompt affiche une proposition claire avant application. Un fournisseur de modèle de langage est configuré côté serveur; aucune clé n’est embarquée dans le navigateur.
- Endpoint MCP distant authentifié, accessible uniquement derrière l’authentification/protection existante. Pas de publication Instagram ni de recalcul de route déclenché par prompt.

### Export

- Exporter un panneau seul ou les panneaux visibles dans une composition PNG.
- Formats initiaux : carré 1080×1080 et portrait 1080×1350, avec aperçu du cadrage.
- Inclure couche(s), annotations, titre, unités, validités et crédits; masquer les contrôles interactifs.
- Prototyper une capture navigateur fidèle (rendu WebGL et tuiles de fond). Si les restrictions du navigateur empêchent une capture exacte, utiliser une capture Playwright côté serveur.
- L’utilisateur télécharge l’image. La publication directe sur Instagram est hors périmètre.

### Galerne Routing — dernière étape

Après les fonctionnalités 1 à 6 ci-dessus, intégrer via un adaptateur serveur la position du navire et sa route calculée. Vérifier le contrat réel, l’authentification, les coordonnées, horodatages et fraîcheur des réponses avant de coder l’adaptateur. Afficher cette couche comme overlay; une erreur de l’API ne bloque pas la météo.

## Architecture retenue

```text
Panel + Bokeh (workspace UI, plein écran sur le Mac mini)
                │
                ├── état Workspace versionné et persistant
                ├── service d’application commun UI / MCP
                ├── adaptateur EUMETSAT WMS ── catalogue + images rendues
                ├── adaptateur GRIB IFS / AROME ── champs géoréférencés
                ├── rendu PNG
                └── [dernière étape] adaptateur Galerne Routing
```

- Garder Panel+Bokeh pour la première version multi-panneaux : le socle actuel fonctionne et permet de conserver les outils de dessin et le pipeline Python.
- Utiliser une figure Bokeh par panneau; partager les sources de données et les résultats identiques au lieu de télécharger/décoder plusieurs fois la même trame.
- Tester le backend WebGL sur les rasters et contours réellement utilisés, tout en conservant le repli Canvas. WebGL ne décharge ni le réseau ni le décodage GRIB.
- N’adopter MapLibre ou une autre couche d’affichage que si une mesure sur le Mac mini révèle une limite concrète de Bokeh.
- Un seul service d’application porte validations, catalogue et mutations. L’état métier ne vit pas dans des callbacks UI; les adapters n’importent pas Panel.
- Conserver la provenance de chaque rendu : fournisseur, identifiant produit/paramètre, run, échéance réelle, CRS/emprise, unités et checksum de l’artefact source.

## État partagé et réactivité

- Le workspace principal est commun aux fenêtres locales et distantes et aux outils MCP; les changements incrémentent une révision monotone.
- Persister de façon atomique disposition, panneaux, synchronisation, échéance de référence et annotations. Ne pas persister les objets Bokeh ni les buffers pixels.
- Les sessions navigateur réconcilient les révisions et ne réappliquent jamais une réponse de chargement devenue obsolète.
- Cache disque existant pour les octets sources; ajouter déduplication des téléchargements et cache partagé des champs/images décodés par clé immuable (source, produit, run, validité, emprise, taille).
- Charger d’abord la trame visible. Précharger les images d’animation en tâche de fond seulement pour les panneaux qui utilisent une source animable.
- Chaque panneau garde sa dernière donnée valide avec un indicateur de chargement/erreur. Le changement de date/couche ne doit pas vider toutes les vues.
- La résolution du rendu est calculée à partir des pixels CSS et du ratio d’écran, avec une limite supérieure explicite; ne pas réduire arbitrairement toutes les vues à une petite vignette.

## Sécurité et limites

- Aucun secret fournisseur ou LLM côté navigateur. Les appels externes restent serveur-à-serveur.
- Les paramètres reçus par MCP sont validés contre les catalogues disponibles et l’identité du workspace.
- Une seule personne authentifiée est la cible initiale; les opérations et le stockage gardent des identifiants de workspace pour ne pas bloquer une évolution multi-utilisateur.
- Les états météo incomplets sont explicités : pas de zéro synthétique, pas d’échéance supposée, pas de route périmée présentée comme actuelle.

## Validation de la première version

La validation manuelle se fait dans le navigateur retenu sur l’écran du Mac mini, à sa résolution native, avec six panneaux : plusieurs produits/couches, requêtes concurrentes, changements rapides d’échéance, annotation, délai fournisseur et erreur isolée. Vérifier netteté, lisibilité, interaction pan/zoom, animation, absence de blocage, et stabilité mémoire après plusieurs changements. Mesurer d’abord la référence sur le poste réel; ne fixer un budget chiffré que d’après cette référence.

Le test automatisé couvre les invariants du workspace, l’application atomique de commandes, la synchronisation des révisions et la protection contre les réponses obsolètes. Le test fournisseur réel reste explicitement séparé.

## Ordre de réalisation

1. État de workspace persistant et service d’application partagé.
2. Grille Bokeh 1/2/4/6, synchronisation du temps et de la caméra, premier test sur écran Mac mini.
3. Catalogue EUMETSAT dynamique et sélection par panneau.
4. Catalogue de champs GRIB et extension IFS; décider/valider la source AROME.
5. Outils MCP et prompt adossés au service partagé.
6. Export PNG fidèle.
7. En dernier : position navire et route Galerne Routing.

Référence de suivi : issues GitHub #1 à #8 du dépôt weather-desk. `PLAN.md` et `OPEN_SOURCE_REVIEW.md` restent des notes de contexte/recherche; cette spécification gouverne les décisions produit actuelles.
