# Weather Desk — plan d'intégration

> **Note de statut :** ce document est une cartographie exploratoire des briques Galerne/Karpos, pas la spécification produit courante. Pour le périmètre, les contrats et l'ordre actuels, voir [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md). La première cible est l'écran du Mac mini; Open-Meteo est écarté et l'intégration Galerne Routing vient en dernier.

## But

Faire du Weather Desk une station personnelle de prévision : confronter images satellite, observations et modèles, annoter une situation, rédiger un bulletin et interroger le contexte par prompts.

Le projet doit consommer les briques existantes par adaptateurs. Il ne doit pas copier les règles métier de Karpos ou Galerne.

## Inventaire des briques réutilisables

### Briques présentes ou prévues dans `wxr-shared`

| Package | Réutilisation Weather Desk |
|---|---|
| `weather-ingest` | contrat de manifeste, `run_id`, source, modèle, bbox, variables, artefact et promotion ; indispensable pour les images et champs IFS |
| `publish-runner` | verrou, logs persistants et exécution idempotente des acquisitions/publications sur le Mac mini |
| `validation` | cible à implémenter, pas un module prêt à brancher ; métriques communes : RMSE, biais, POD/FAR/CSI, CRPS et spread/skill |
| `observability` | cible future pour logs JSON, Sentry/Loki/Grafana ; le package est encore à compléter |
| `agent-llm` | contrat cible runtime/outils/prompt, package encore à implémenter ; ne pas le considérer comme un client disponible |

`wxr-shared` fournit aujourd'hui surtout des contrats et des wrappers. Les clients d'acquisition satellite, les cartes et les règles de bulletin restent à extraire ou à adapter.

### Galerne réutilisable par adaptateur

| Module | Usage |
|---|---|
| `wxrouting.data.hres_opendata.HresOpenDataClient` | récupérer et normaliser les champs HRES/IFS ouverts |
| `wxrouting.forecast.geoarches_io` | conventions de grille, crop, orientation et validation des champs |
| `wxrouting.serve.windfield_store` | stockage, cache, registre de versions et métadonnées de champs |
| `wxrouting.serve.tiles` | tuiles raster à la demande ; reprendre la garde anti-SSRF et la généraliser aux datasets autorisés |
| `wxrouting.serve.route_provenance` | modèle de provenance ; le généraliser de route à `WeatherArtifactProvenance` |
| `wxrouting.serve.bulletin_llm` | pattern LLM hors chemin critique, timeout, repli déterministe et protection contre l'injection |
| `karpOS/karpos/tools/hermes_http/server.py` | boucle tool-calling bornée, endpoint HTTP, Bearer et séparation lecture/calcul vs mutations |

À ne pas importer directement : logique de route maritime, polaires, courants, ports et règles spécifiques au skipper.

### Karpos réutilisable par adaptateur

| Module | Usage |
|---|---|
| `backtest/api/agent_facade/service.py` | facts météo, ensembles et indicateurs déterministes |
| `backtest/api/bulletin_meteo/service.py` | pipeline facts → risques → grounding → narration, avec vérification des nombres |
| `backtest/api/bulletin_meteo/producer.py` | publication idempotente et reprise sur erreur |
| `backtest/api/agent/tools.py` | schémas d'outils et permissions ; filtrer pour un profil analyste personnel |
| `backtest/api/mcp_server.py` | façade MCP à réutiliser pour les prompts, après retrait des outils assurance non pertinents |
| `backtest/src/lineage.py` | provenance des sources et des transformations |
| `backtest/src/viz/maps.py` | cartes et figures météo existantes à extraire en fonctions pures |
| `backtest/src/datasources/*` | patterns de sources et normalisation ; réutiliser uniquement les connecteurs météo nécessaires |
| `backtest/src/indices/*` | indices gel, phénologie et risque ; futurs modules stress hydrique/maturité |

À ne pas importer directement : API FastAPI complète, dépendances Supabase, facturation, RLS, logique produit assurance et prompts agriculteur finaux.

## Architecture cible

```text
Weather Desk Panel + Bokeh
  ├─ Analysis state (zone, valid_time, layers, annotations)
  ├─ Satellite adapter (EUMETView / EUMDAC / local)
  ├─ Model adapter (IFS / AROME / ensemble)
  ├─ Observation adapter (stations / radar / foudre)
  ├─ Annotation export (GeoJSON + JSON ; stockage à ajouter)
  ├─ Bulletin renderer (Markdown + manifest)
  └─ Prompt client
          ↓
  Weather tools read-only / calcul
          ↓
  wxr-shared contracts + Karpos/Galerne adapters
          ↓
  Hermes / LiteLLM
```

## Interaction par prompts

Le prompt doit agir sur l'analyse courante, jamais sur un contexte implicite. Le modèle reçoit : zone, échéance, couches visibles, IDs de runs, observations sélectionnées, annotations et provenance.

### Outils de lecture/calcul

- `list_frames(zone, time_window, product)` — liste les images et champs disponibles ;
- `get_frame(frame_id)` — métadonnées et URI autorisée ;
- `compare_models(zone, valid_time, variables, models)` — différences et accord entre modèles ;
- `get_observations(zone, window, variables)` — stations et contrôles qualité ;
- `get_satellite_context(zone, time_window, channels)` — séquence vapeur d'eau/IR ;
- `compute_indicator(indicator, zone, window, parameters)` — indice déterministe versionné ;
- `summarize_situation(analysis_id)` — synthèse factuelle des couches et annotations ;
- `draft_bulletin(analysis_id, audience, language)` — brouillon ancré sur les faits ;
- `check_bulletin(bulletin_id)` — contrôle des chiffres, unités, provenance et limites.

### Mutations soumises à validation

- `save_annotation(geometry, label, confidence)` ;
- `save_hypothesis(text, test_by)` ;
- `save_bulletin_draft(markdown)` ;
- `publish_bulletin(target)`.

Les trois premiers peuvent être proposés par Hermes dans une inbox. La publication externe exige une validation explicite et laisse une trace.

### Exemples de prompts

```text
Compare l'analyse IFS 00Z et AROME 03Z sur la façade Atlantique à 18Z.
Concentre-toi sur la position du front, la pluie et le vent. Cite les runs.
```

```text
À partir des couches visibles et du front tracé, rédige un brouillon de bulletin
pour un opérateur maritime. Sépare les faits, l'interprétation et l'incertitude.
N'ajoute aucun chiffre absent des sources.
```

```text
Enregistre cette ligne comme hypothèse testable à 18Z : le front accélère
sur l'ouest de la Bretagne. Ne publie rien.
```

## Phasage

### P0 — application visible

- [x] application Panel + Bokeh, une analyse isolée par session ;
- [x] deux images affichables ;
- [x] dessin sur carte ;
- [x] export Markdown ;
- [x] exporter les annotations GeoJSON et un manifeste JSON indépendant ;
- [x] archive ZIP avec bulletin, tracés, images locales normalisées et checksums ;
- [x] distinguer `source`, `run`, `valid_time` et `checksum` (métadonnées manuelles) ;
- [ ] sauvegarde automatique et restauration de session ;
- [x] géoréférencement des couches EUMETSAT/IFS et superposition des tracés ;
- [ ] géoréférencement des images importées manuellement ;
- [ ] symboles conventionnels des fronts.

Le moteur d’export est dans `weather_desk/analysis.py`, sans dépendance Panel. Les connecteurs EUMETView et IFS sont implémentés dans `weather_desk/data.py`. Les prompts et la persistance des analyses ne le sont pas encore. Les URL distantes sont affichées par le navigateur et ne sont ni archivées ni vérifiées par checksum.

### P1 — sources réelles

- [x] EUMETView/WMS WV6.2, catalogue temporel, EPSG:3857 et animation préchargée ;
- [ ] adapter EUMDAC/Data Store pour les artefacts numériques ;
- [x] adaptateur IFS léger avec le client officiel utilisé par Galerne : MSL/Z500, runs figés, contrôle des GRIB ;
- [ ] extraire le contrat commun dans wxr-shared après stabilisation des besoins Galerne/Weather Desk ;
- [ ] brancher AROME/ensemble via la façade Karpos ;
- [x] boucle satellite, cache borné avec checksums, heures séparées et alerte satellite de plus de 2 h ;
- [ ] animation des échéances IFS (changement manuel avec cache disponible) ;
- [ ] surveillance continue des retards fournisseurs.

### P2 — analyse et bulletin

- [ ] extraire cartes/figures pures de Karpos ;
- [ ] afficher comparaison modèle-observation ;
- [ ] intégrer le renderer de bulletin Karpos en mode générique ;
- [ ] ajouter validation des chiffres et unités ;
- [ ] publier un bulletin uniquement après validation humaine.

### P3 — prompts et Hermes

- [ ] créer un skill `weather_desk` dans karpOS ;
- [ ] exposer les outils read-only ci-dessus ;
- [ ] ajouter le contexte d'analyse courant au prompt ;
- [ ] compléter `agent-llm` puis réutiliser le pattern Hermes HTTP existant ;
- [ ] journaliser modèle, tokens, tools, latence et artefacts ;
- [ ] ajouter une inbox de mutations proposées.

### P4 — supervision

- [ ] brancher `publish-runner` pour les acquisitions ;
- [ ] exporter les métriques vers Grafana ;
- [ ] ajouter alerte de données périmées ;
- [ ] tester restauration des annotations et bulletins ;
- [ ] documenter la procédure de reprise Mac mini.

## Règles de conception

- Les faits météo sont déterministes et sourcés ; le LLM ne les invente pas.
- Le LLM interprète et rédige ; il ne choisit pas silencieusement une autre source.
- Toute couche visible possède un `frame_id` et une provenance.
- Toute annotation est liée à une échéance ; le versionnement persistant reste à développer.
- Le bulletin doit rester produisible sans LLM.
- Les outils exposés par prompt sont bornés, authentifiés et en lecture seule par défaut.
