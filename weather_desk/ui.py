"""Panel session and Bokeh annotations. Acquisition adapters will stay outside this module."""

import html
import warnings
from io import BytesIO
from pathlib import PurePath
from urllib.parse import urlsplit
from uuid import uuid4

import panel as pn
from bokeh.models import ColumnDataSource, PolyDrawTool, PolyEditTool, Range1d, WMTSTileSource
from bokeh.plotting import figure
from PIL import Image, UnidentifiedImageError

from weather_desk.analysis import (
    MERCATOR_LIMIT,
    checksum,
    drawings_geojson,
    export_bundle,
    json_bytes,
    render_bulletin,
    to_mercator,
    utc_now,
)
from weather_desk.live import LiveLayers
from weather_desk.workspace import WorkspaceConflict, WorkspaceService


def normalize_image(content: bytes) -> bytes:
    if len(content) > 15 * 1024 * 1024:
        raise ValueError("Image trop volumineuse (15 Mo maximum).")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(content)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("Formats acceptés : PNG, JPEG, WebP.")
            if image.width * image.height > 20_000_000:
                raise ValueError("Image trop grande (20 millions de pixels maximum).")
            output = BytesIO()
            image.convert("RGBA").save(output, format="PNG")
            return output.getvalue()


def image_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Utiliser une URL directe d'image HTTP(S).")
    if parsed.username or parsed.password:
        raise ValueError("Ne pas inclure d'identifiants dans l'URL.")
    return value


class SourceCard:
    def __init__(self, key: str, title: str, changed):
        self.key = key
        self.changed = changed
        self.normalized = None
        self.original_checksum = None
        self.error = None
        self.upload = pn.widgets.FileInput(label="Image locale", accept=".png,.jpg,.jpeg,.webp")
        self.url = pn.widgets.TextInput(label="Ou URL directe d'image", placeholder="https://…")
        self.origin = pn.widgets.TextInput(
            label="Source / produit", placeholder="Ex. EUMETSAT WV 6.2"
        )
        self.run = pn.widgets.TextInput(
            label="Run (UTC, si modèle)", placeholder="2026-09-30T00:00:00Z"
        )
        self.valid = pn.widgets.TextInput(
            label="Validité de l'image (UTC)", placeholder="À renseigner"
        )
        self.preview = pn.Column(
            pn.pane.Markdown("Charger une image pour commencer."), min_height=160
        )
        self.status = pn.pane.Markdown("Aucune image sélectionnée.")
        self.clear = pn.widgets.Button(label="Retirer l'image", color="light")
        self.clear.on_click(self._clear)
        self.upload.param.watch(self._image_changed, "value")
        self.url.param.watch(self._image_changed, "value")
        for widget in (self.origin, self.run, self.valid):
            widget.param.watch(lambda event: self.changed(), "value")
        self.view = pn.Card(
            self.preview,
            self.upload,
            self.url,
            self.clear,
            pn.Accordion(("Provenance", pn.Column(self.origin, self.run, self.valid, self.status))),
            title=title,
            sizing_mode="stretch_width",
        )

    def _clear(self, event):
        self.url.value = ""
        self.upload.value = None

    def _image_changed(self, event):
        self.normalized = None
        self.original_checksum = None
        self.error = None
        try:
            if self.upload.value:
                self.normalized = normalize_image(self.upload.value)
                self.original_checksum = checksum(self.upload.value)
                self.preview.objects = [pn.pane.PNG(self.normalized, sizing_mode="scale_width")]
                self.status.object = (
                    "Fichier local prioritaire sur l'URL. SHA-256 du fichier original : "
                    f"`{self.original_checksum}`"
                )
            elif self.url.value.strip():
                url = image_url(self.url.value.strip())
                # Browser-only request: never fetch arbitrary user URLs on the server.
                self.preview.objects = [
                    pn.pane.HTML(
                        f'<img src="{html.escape(url, quote=True)}" '
                        'referrerpolicy="no-referrer" alt="Image distante (URL directe requise)" '
                        'style="width:100%;max-height:420px;object-fit:contain">'
                    )
                ]
                self.status.object = (
                    "Image distante chargée par le navigateur ; checksum non vérifié. "
                    "Importer le fichier pour l'inclure dans l'archive."
                )
            else:
                self.preview.objects = [pn.pane.Markdown("Charger une image pour commencer.")]
                self.status.object = "Aucune image sélectionnée."
        except (
            ValueError,
            OSError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as exc:
            self.error = str(exc)
            self.preview.objects = [pn.pane.Alert(self.error, alert_type="danger")]
            self.status.object = "Image invalide : retirer ou remplacer le fichier pour exporter."
        self.changed()

    def metadata(self) -> dict:
        local = self.normalized is not None
        return {
            "frame_id": self.key,
            "source": self.origin.value.strip() or None,
            "run": self.run.value.strip() or None,
            "valid_time": self.valid.value.strip() or None,
            "metadata_status": "manually_entered",
            "kind": "upload" if local else "remote_url" if self.url.value.strip() else "missing",
            "original_filename": PurePath(self.upload.filename or "image").name if local else None,
            "uri": f"images/{self.key}.png" if local else self.url.value.strip() or None,
            "original_sha256": self.original_checksum,
            "sha256": checksum(self.normalized) if local else None,
            "georeferenced": False,
        }


class WeatherDesk:
    def __init__(self, workspace_service: WorkspaceService | None = None):
        self.workspace_service = workspace_service
        self.workspace_state = workspace_service.read() if workspace_service else None
        self._syncing_workspace = False
        self.workspace_poller = None
        self.analysis_id = str(uuid4())
        self.created = utc_now()
        self.fields = {
            "zone": pn.widgets.TextInput(label="Zone", value="France / façade Atlantique"),
            "valid_time": pn.widgets.TextInput(
                label="Échéance de l'analyse (UTC)", value=self.created
            ),
            "confidence": pn.widgets.Select(
                label="Confiance", options=["faible", "moyenne", "forte"], value="moyenne"
            ),
            "headline": pn.widgets.TextInput(label="Message principal"),
            "analysis": pn.widgets.TextAreaInput(label="Analyse", height=160),
            "impacts": pn.widgets.TextAreaInput(label="Impacts et décisions", height=120),
            "limitations": pn.widgets.TextAreaInput(label="Limites et incertitudes", height=100),
        }
        self.sources = [
            SourceCard("satellite", "Satellite — vapeur d'eau / IR", self.refresh),
            SourceCard("model", "Modèle — IFS / AROME", self.refresh),
        ]
        self.layers = {}
        west, south = to_mercator(-25, 32)
        east, north = to_mercator(35, 65)
        self.map_x_range = Range1d(west, east, bounds=(-MERCATOR_LIMIT, MERCATOR_LIMIT))
        self.map_y_range = Range1d(south, north, bounds=(-MERCATOR_LIMIT, MERCATOR_LIMIT))
        self.maps = [self._make_map(index + 1) for index in range(6)]
        self.map = self.maps[0]
        self.live = LiveLayers(self.maps, self.refresh)
        self.layout = pn.widgets.Select(
            label="Disposition des panneaux",
            options={
                "1 panneau": "1",
                "2 panneaux côte à côte": "2-horizontal",
                "2 panneaux empilés": "2-vertical",
                "4 panneaux (2 × 2)": "4",
                "6 panneaux (3 × 2)": "6",
                "Disposition automatique": "auto",
            },
            value=(self.workspace_state.layout if self.workspace_state else "1"),
        )
        self.map_panes = [pn.pane.Bokeh(plot, sizing_mode="stretch_width") for plot in self.maps]
        self.map_grid = pn.GridBox(
            self.map_panes[0], ncols=1, sizing_mode="stretch_width", name="weather_map_grid"
        )
        initial_count = len(self.workspace_state.panels) if self.workspace_state else 1
        self._apply_layout(self.layout.value, initial_count)
        self.layout.param.watch(self._layout_changed, "value")
        self.status = pn.pane.Alert("", alert_type="info")
        self.preview = pn.pane.Str("", styles={"white-space": "pre-wrap"})
        self.downloads = [
            pn.widgets.FileDownload(
                label=label,
                filename=filename,
                callback=lambda kind=kind: self.download(kind),
                color="primary" if kind == "zip" else "default",
            )
            for kind, label, filename in [
                ("zip", "Exporter l'analyse complète (.zip)", "weather-desk.zip"),
                ("md", "Bulletin Markdown", "bulletin.md"),
                ("geojson", "Annotations GeoJSON", "annotations.geojson"),
                ("json", "Manifeste JSON", "manifest.json"),
            ]
        ]
        for field in self.fields.values():
            field.sizing_mode = "stretch_width"
            field.param.watch(lambda event: self.refresh(), "value")
        self.view = pn.template.FastListTemplate(
            title="Weather Desk",
            accent_base_color="#176b87",
            header_background="#123044",
            sidebar=[
                pn.pane.Markdown("## Poste multi-panneaux"),
                self.layout,
                self.live.controls,
                pn.pane.Markdown("## Contexte de l'analyse"),
                *[self.fields[k] for k in ("zone", "valid_time", "confidence")],
                pn.pane.Markdown("## Export"),
                *self.downloads,
                pn.pane.Markdown(
                    "L'analyse reste en mémoire pendant cette session. "
                    "**Exporter avant de fermer ou recharger la page.**"
                ),
            ],
            main=[
                self.live.time_status,
                pn.Card(
                    self.map_grid,
                    self.status,
                    pn.Accordion(
                        (
                            "Aide au tracé",
                            pn.pane.Markdown(
                                "Choisir un outil : **front froid (bleu), chaud (rouge), "
                                "occlusion (violet) ou zone (orange)**. "
                                "Faire un appui prolongé pour commencer, "
                                "cliquer pour placer les sommets, "
                                "puis un appui prolongé pour terminer. "
                                "Les outils d'édition permettent de déplacer les sommets. "
                                "Sélectionner un tracé avec son outil puis Retour arrière "
                                "pour le retirer, en gardant le pointeur sur la carte. "
                                "Les fronts sont des lignes colorées, sans symboles "
                                "météorologiques pour l'instant."
                            ),
                        )
                    ),
                    title="Atlantique / Europe — vapeur d'eau + IFS",
                    collapsible=False,
                ),
                pn.Card(
                    pn.pane.Markdown(
                        "Ces imports manuels sont des références complémentaires "
                        "non géoréférencées. "
                        "Les couches EUMETSAT et IFS de la carte "
                        "sont géoréférencées automatiquement."
                    ),
                    pn.Row(*[source.view for source in self.sources], sizing_mode="stretch_width"),
                    title="Images de référence locales ou par URL",
                    collapsed=True,
                ),
                pn.Card(
                    *[self.fields[k] for k in ("headline", "analysis", "impacts", "limitations")],
                    pn.Accordion(("Aperçu du Markdown exporté", self.preview)),
                    title="Bulletin",
                    collapsible=False,
                    sizing_mode="stretch_width",
                ),
            ],
            main_max_width="1600px",
        )
        self.refresh()

    def _make_map(self, index: int):
        plot = figure(
            title=f"Panneau {index}",
            x_range=self.map_x_range,
            y_range=self.map_y_range,
            x_axis_type="mercator",
            y_axis_type="mercator",
            height=640,
            sizing_mode="stretch_width",
            tools="pan,wheel_zoom,reset,save",
            active_scroll="wheel_zoom",
            toolbar_location="above",
        )
        plot.add_tile(
            WMTSTileSource(
                url="https://tile.openstreetmap.org/{Z}/{X}/{Y}.png",
                attribution='© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
            )
        )
        for kind, label, color, geometry in [
            ("cold_front", "Front froid", "#1671d9", "LineString"),
            ("warm_front", "Front chaud", "#d73027", "LineString"),
            ("occlusion", "Occlusion", "#8e44ad", "LineString"),
            ("area", "Zone d'intérêt", "#c56a00", "Polygon"),
        ]:
            if kind in self.layers:
                source = self.layers[kind]["source"]
            else:
                source = ColumnDataSource(data={"xs": [], "ys": []}, name=kind)
                self.layers[kind] = {"source": source, "geometry": geometry}
                source.on_change("data", lambda attr, old, new: self.refresh())
            kwargs = dict(xs="xs", ys="ys", source=source, line_color=color, line_width=3)
            if geometry == "Polygon":
                renderer = plot.patches(**kwargs, fill_color=color, fill_alpha=0.15)
            else:
                renderer = plot.multi_line(**kwargs)
            vertices = plot.scatter(x=[], y=[], size=9, color=color)
            plot.add_tools(
                PolyDrawTool(renderers=[renderer], description=f"Tracer : {label}"),
                PolyEditTool(
                    renderers=[renderer],
                    vertex_renderer=vertices,
                    description=f"Modifier : {label}",
                ),
            )
        return plot

    def _apply_layout(self, layout: str, panel_count: int | None = None):
        if layout == "auto":
            count = panel_count or (len(self.workspace_state.panels) if self.workspace_state else 1)
            columns = 1 if count == 1 else 2 if count <= 4 else 3
        else:
            counts = {
                "1": (1, 1),
                "2-horizontal": (2, 2),
                "2-vertical": (2, 1),
                "4": (4, 2),
                "6": (6, 3),
            }
            count, columns = counts[layout]
        self.map_grid.objects = self.map_panes[:count]
        self.map_grid.ncols = columns

    def _layout_changed(self, event):
        layout = event.new
        if self._syncing_workspace:
            return
        if self.workspace_service is None:
            self._apply_layout(layout)
            return
        try:
            self.workspace_state = self.workspace_service.apply(
                [{"op": "set_layout", "layout": layout}],
                expected_revision=self.workspace_state.revision,
                workspace_id=self.workspace_state.workspace_id,
            )
            self._apply_layout(layout, len(self.workspace_state.panels))
        except WorkspaceConflict:
            self.sync_workspace()

    def sync_workspace(self):
        if self.workspace_service is None:
            return
        incoming = self.workspace_service.read(self.workspace_state.workspace_id)
        if incoming.revision == self.workspace_state.revision:
            return
        self.workspace_state = incoming
        self._syncing_workspace = True
        try:
            self.layout.value = incoming.layout
        finally:
            self._syncing_workspace = False
        self._apply_layout(incoming.layout, len(incoming.panels))

    def start(self):
        self.live.start()
        if self.workspace_service is not None and self.workspace_poller is None:
            self.workspace_poller = pn.state.add_periodic_callback(self.sync_workspace, period=500)
            if pn.state.curdoc and pn.state.curdoc.session_context:
                pn.state.on_session_destroyed(self._session_closed)

    def _session_closed(self, session_context):
        if self.workspace_poller:
            self.workspace_poller.stop()

    def snapshot(self):
        errors = [source.error for source in self.sources if source.error]
        if errors:
            raise ValueError("Corriger les images invalides avant l'export.")
        fields = {key: widget.value for key, widget in self.fields.items()}
        annotations = drawings_geojson(
            {
                key: {"data": layer["source"].data, "geometry": layer["geometry"]}
                for key, layer in self.layers.items()
            },
            fields["valid_time"],
        )
        manifest = {
            "schema_version": 1,
            "analysis_id": self.analysis_id,
            "created_at_utc": self.created,
            "exported_at_utc": utc_now(),
            "analysis": fields,
            "sources": [source.metadata() for source in self.sources] + self.live.metadata(),
            "map_view": {
                "crs": "EPSG:3857",
                "bbox": [
                    self.map.x_range.start,
                    self.map.y_range.start,
                    self.map.x_range.end,
                    self.map.y_range.end,
                ],
            },
            "annotations": {"uri": "annotations.geojson", "count": len(annotations["features"])},
            "review": "pending",
        }
        return manifest, annotations

    def refresh(self):
        try:
            manifest, annotations = self.snapshot()
            self.preview.object = render_bulletin(manifest, annotations)
            self.status.object = f"{len(annotations['features'])} annotation(s) exportable(s)."
            self.status.alert_type = "info"
            for download in self.downloads:
                download.disabled = False
        except ValueError as exc:
            self.status.object = str(exc)
            self.status.alert_type = "danger"
            self.preview.object = "Export suspendu : corriger les données signalées."
            for download in self.downloads:
                download.disabled = True

    def download(self, kind: str) -> BytesIO:
        manifest, annotations = self.snapshot()
        if kind == "zip":
            return export_bundle(
                manifest,
                annotations,
                {source.key: source.normalized for source in self.sources if source.normalized},
                self.live.artifacts(),
            )
        if kind == "md":
            return BytesIO(render_bulletin(manifest, annotations).encode())
        return BytesIO(json_bytes(annotations if kind == "geojson" else manifest))
