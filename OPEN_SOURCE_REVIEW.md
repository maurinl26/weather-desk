# Solutions open source — revue pour Weather Desk

> Cette revue conserve le comparatif des composants, mais ses séquences de phases ne sont plus le plan de réalisation. La spécification produit actuelle est [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md); elle retient Panel+Bokeh, l'ingestion GRIB directe et l'écran du Mac mini comme première cible.

## Conclusion

Parmi les solutions étudiées, aucune n’a été retenue comme application complète pour notre flux satellite, modèles, annotations et bulletins. Les outils existants se répartissent en trois familles :

- **stations de météorologie** : Metview, AWIPS II ;
- **briques scientifiques** : Satpy, MetPy, xarray, Cartopy, cfgrib ;
- **interfaces de données** : QGIS, Panel/HoloViz, Streamlit.

Le bon plan est de composer ces briques autour de contrats `WeatherFrame`, `Analysis` et `Bulletin`, sans adopter une suite monolithique.

## Comparatif

| Solution | Ce qu'elle apporte | Limite pour notre cas | Décision |
|---|---|---|---|
| **Metview / ECMWF** | Poste de travail météo complet, accès/traitement/visualisation de GRIB et BUFR, interface graphique, scripts Python et notebooks. | Installation plus lourde et interface à intégrer avec nos annotations, provenance et prompts. | À utiliser comme outil expert de référence et pour valider les traitements GRIB ; pas comme front principal du MVP. |
| **AWIPS II / Unidata AWIPS** | Référence open source la plus proche d'une station opérationnelle de prévision : couches, radar, satellite, modèles et outils forecaster. | Stack lourde, orientée écosystème NOAA/Linux ; trop coûteuse à adapter à Mac mini, EUMETSAT, Karpos et bulletins Markdown. | Étudier les concepts d'interface ; ne pas déployer dans la première version. |
| **Satpy / Pytroll** | Lecture de nombreux produits satellites, composites RGB, améliorations, reprojection et sorties PNG/GeoTIFF/NetCDF ; support Data Store EUMETSAT. | Ne gère ni modèles NWP ni bulletin ni workflow de prévision à lui seul. | **Brique satellite prioritaire.** |
| **MetPy / Unidata** | Calculs météo, unités, diagnostics, profils, tracés et intégration xarray ; bon socle pour champs et conventions météorologiques. | Ne fournit pas une application de poste de travail ni une orchestration de données. | **Brique diagnostic prioritaire.** |
| **xarray + cfgrib + Cartopy** | Base Python naturelle pour NetCDF/Zarr/GRIB, champs multidimensionnels, projections et figures reproductibles. | Demande de construire l'interface et les contrats. | **Socle données/figures.** |
| **QGIS** | Analyse manuelle robuste, WMS/WCS, raster/vector, fonds cartographiques et annotations riches. | Sépare le travail de l'application et du bulletin ; automatisation limitée pour notre flux. | Outil d'analyse experte et de secours, pas le produit principal. |
| **Panel/HoloViz** | Dashboards Python réactifs, vues liées, xarray, GeoViews, hvPlot, Datashader et widgets ; adapté aux applications scientifiques servies depuis notebooks/scripts. | Plus de concepts et de paramétrage que Streamlit pour un MVP simple. | **Interface retenue dès maintenant**, avec Bokeh pour le dessin cartographique. |
| **Streamlit** | Très rapide pour assembler formulaire, images, carte, prompt et téléchargement Markdown. | Gestion moins naturelle des vues liées, time slider riche et gros rasters. | Premier prototype remplacé par Panel. |
| **Grafana** | Séries, alertes, santé des acquisitions et métriques de jobs. | Raisonnement synoptique et annotation de fronts hors sujet. | Monitoring uniquement. |

Sources : [Metview ECMWF](https://confluence.ecmwf.int/spaces/METV/overview), [Metview Python](https://github.com/ecmwf/metview-python), [Satpy](https://satpy.readthedocs.io/en/latest/), [MetPy](https://unidata.github.io/MetPy/latest/), [AWIPS](https://www.weather.gov/cp/DownloadAWIPS), [Panel/HoloViz](https://holoviz.org/index.html), [QGIS OGC/WMS](https://docs.qgis.org/3.44/en/docs/user_manual/working_with_ogc/ogc_client_support.html).

## Plan retenu

### Phase 0 — MVP visible

Utiliser directement Panel + Bokeh : deux images, carte, annotations et bulletin Markdown. La migration inclut le manifeste des sources, le GeoJSON et une archive des images locales avec checksums. Le géoréférencement et la restauration de session restent à développer.

### Phase 1 — moteur scientifique

Ajouter les dépendances et adaptateurs suivants :

```text
satpy              → images Meteosat, vapeur d'eau, IR, RGB, reprojection
eumdac             → accès Data Store/Data Tailor EUMETSAT
xarray + cfgrib    → champs NetCDF/GRIB et séries multidimensionnelles
metpy              → unités, diagnostics, fronts, profils et calculs météo
cartopy             → projections et figures reproductibles
```

Les fonctions doivent recevoir/retourner des objets internes, pas des éléments Panel. Cela permet de les appeler depuis l’interface, un notebook ou les futurs outils Hermes sans réécrire le moteur.

### Phase 2 — adaptateurs Karpos/Galerne

- IFS/HRES : reprendre `HresOpenDataClient`, les conventions de grille et les métadonnées de Galerne derrière `ModelFrameAdapter`.
- AROME/ensemble : appeler la façade déterministe de Karpos derrière le même contrat.
- Bulletins : reprendre la chaîne facts/risks/grounding/narrative de Karpos, en supprimant les règles assurance.
- Provenance : unifier `weather-ingest`, `lineage.py` et `route_provenance` dans un manifest Weather Desk.
- Tuiles : reprendre la garde anti-SSRF de Galerne et n'autoriser que les URI locales ou les buckets configurés.

### Phase 3 — poste de prévision

Étendre le socle Panel existant : géoréférencement des couches, vues synchronisées, animation temporelle, séries et coupes. Ajouter GeoViews/hvPlot/Datashader lorsque les données et le volume le justifient ; ces dépendances ne sont pas nécessaires au dessin actuel avec Bokeh.

### Phase 4 — prompts Hermes

Créer un skill `weather_desk` dans karpOS. Les outils sont d'abord en lecture/calcul :

- `list_frames`, `get_frame` ;
- `compare_models` ;
- `get_satellite_context` ;
- `get_observations` ;
- `compute_indicator` ;
- `summarize_situation` ;
- `draft_bulletin` ;
- `check_bulletin`.

Les mutations (`save_annotation`, `save_hypothesis`, `save_bulletin_draft`, `publish_bulletin`) passent par une inbox et une validation humaine. On reprend la boucle tool-calling bornée et le repli déterministe déjà présents dans Hermes/Galerne.

## Modules internes à extraire en premier

1. `wxr-shared/packages/weather-ingest` — manifeste et promotion d'artefacts.
2. `wxr-shared/packages/publish-runner` — verrou et logs des acquisitions.
3. `galerne ... hres_opendata.py` — accès IFS/HRES.
4. `karpos-engine/backtest/src/lineage.py` — provenance.
5. `karpos-engine/backtest/api/bulletin_meteo/service.py` — facts et narration vérifiée.
6. `karpOS/karpos/tools/hermes_http/server.py` — boucle prompts/outils, avec outils météo dédiés.
7. Satpy/MetPy dans `weather-desk/analysis/` — nouvelles briques scientifiques, sans les enfouir dans l'UI.

## Décision finale

- **Ne pas installer AWIPS.** Trop lourd et mal aligné avec le besoin personnel.
- **Ne pas transformer Grafana en poste de prévision.** Il reste l'observabilité.
- **Panel adopté le 30 septembre 2026.** Interface réactive et Bokeh pour la carte ; moteur indépendant de l’interface.
- **Investir d'abord dans les contrats et la provenance.** C'est ce qui rendra l'outil flexible et réutilisable pour Karpos et Galerne.
- **Ajouter Satpy et MetPy avant d'ajouter davantage de modèles.** L'imagerie et le raisonnement doivent être solides avant l'automatisation agentique.
