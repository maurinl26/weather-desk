"""Panel session and Bokeh annotations. Acquisition adapters will stay outside this module."""

import asyncio
import html
import json
import math
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
    checksum,
    drawings_geojson,
    export_bundle,
    json_bytes,
    render_bulletin,
    to_lonlat,
    to_mercator,
    utc_now,
)
from weather_desk.assistant import propose_commands
from weather_desk.cartography import (
    PROJECTIONS,
    domain_bounds,
    land_lines,
    land_polygons,
    project_xy,
    transformer,
)
from weather_desk.live import LiveLayers
from weather_desk.png_export import format_png_validity, prepare_png_document, render_png
from weather_desk.temporal import resolve_ifs_time
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
        self._has_local_conflict = False
        self.workspace_poller = None
        self.analysis_id = str(uuid4())
        self.created = utc_now()
        editorial = (self.workspace_state.editorial or {}) if self.workspace_state else {}
        self.fields = {
            "zone": pn.widgets.TextInput(
                label="Zone", value=editorial.get("zone", "France / façade Atlantique")
            ),
            "valid_time": pn.widgets.TextInput(
                label="Échéance de l'analyse (UTC)",
                value=(
                    self.workspace_state.reference_time if self.workspace_state else self.created
                ),
            ),
            "confidence": pn.widgets.Select(
                label="Confiance",
                options=["faible", "moyenne", "forte"],
                value=editorial.get("confidence", "moyenne"),
            ),
            "headline": pn.widgets.TextInput(
                label="Message principal", value=editorial.get("headline", "")
            ),
            "analysis": pn.widgets.TextAreaInput(
                label="Analyse", value=editorial.get("analysis", ""), height=160
            ),
            "impacts": pn.widgets.TextAreaInput(
                label="Impacts et décisions", value=editorial.get("impacts", ""), height=120
            ),
            "limitations": pn.widgets.TextAreaInput(
                label="Limites et incertitudes", value=editorial.get("limitations", ""), height=100
            ),
        }
        self.sources = [
            SourceCard("satellite", "Satellite — vapeur d'eau / IR", self.refresh),
            SourceCard("model", "Modèle — IFS / AROME", self.refresh),
        ]
        self.layers = {}
        self.projection = self.workspace_state.projection if self.workspace_state else "mercator"
        west, south, east, north = domain_bounds(self.projection)
        self.map_x_range = Range1d(west, east)
        self.map_y_range = Range1d(south, north)
        self.tiles = []
        self.land_sources = []
        self.land_fill_sources = []
        self.land_fill_renderers = []
        self.maps = [self._make_map(index + 1) for index in range(6)]
        self.map = self.maps[0]
        self.live = LiveLayers(self.maps, self.refresh, projection=self.projection)
        self.live.reference_time = self.fields["valid_time"].value
        self.live.model_fields.param.watch(self._panel_fields_changed, "value")
        self.live.sat_product.param.watch(self._panel_product_changed, "value")
        if self.workspace_state:
            self._restore_annotations(self.workspace_state.annotations)
        self.fields["valid_time"].param.watch(self._reference_time_changed, "value")
        for plot, renderer in zip(self.maps, self.land_fill_renderers, strict=True):
            plot.renderers.remove(renderer)
            plot.renderers.insert(1, renderer)
        self.live.projection_control.param.watch(self._projection_changed, "value")
        self._camera_dirty = False
        for prop in ("start", "end"):
            self.map_x_range.on_change(prop, self._camera_changed)
            self.map_y_range.on_change(prop, self._camera_changed)
        if self.workspace_state:
            self._apply_camera(self.workspace_state.camera)
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
        self.reload_conflict_button = pn.widgets.Button(
            label="Recharger la version enregistrée", color="warning", visible=False
        )
        self.reload_conflict_button.on_click(self._reload_workspace_conflict)
        self.analysis_name = pn.widgets.TextInput(
            label="Nom de l'analyse",
            value=self.workspace_state.name if self.workspace_state else "Analyse météo",
        )
        self.rename_analysis_button = pn.widgets.Button(label="Renommer", width=110)
        self.duplicate_analysis_button = pn.widgets.Button(label="Dupliquer", width=110)
        self.analysis_select = pn.widgets.Select(
            label="Ouvrir une analyse", options={}, value=None, disabled=True
        )
        self.freeze_edition_button = pn.widgets.Button(
            label="Figer une édition", color="primary", disabled=workspace_service is None
        )
        self.edition_select = pn.widgets.Select(
            label="Éditions immuables", options={}, value=None, disabled=True
        )
        self.edition_download = pn.widgets.FileDownload(
            label="Télécharger l'édition figée (.zip)",
            filename="weather-desk-edition.zip",
            callback=self._download_edition,
            disabled=True,
        )
        if self.workspace_service:
            self._refresh_analysis_options()
            self._refresh_editions()
            self.analysis_select.param.watch(self._open_analysis, "value")
            self.rename_analysis_button.on_click(self._rename_analysis)
            self.duplicate_analysis_button.on_click(self._duplicate_analysis)
            self.freeze_edition_button.on_click(self._freeze_edition)
            self.edition_select.param.watch(self._edition_selected, "value")
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
        self.png_target = pn.widgets.Select(
            label="Contenu PNG",
            options={"Panneau principal": "panel", "Composition visible": "workspace"},
            value="workspace",
        )
        self.png_format = pn.widgets.Select(
            label="Format",
            options={"Carré · 1080 × 1080": "square", "Portrait · 1080 × 1350": "portrait"},
            value="portrait",
        )
        self.png_download = pn.widgets.FileDownload(
            label="Télécharger le PNG",
            filename="weather-desk.png",
            color="primary",
            disabled=True,
        )
        self.png_preview_button = pn.widgets.Button(label="Prévisualiser le PNG", color="primary")
        self.png_preview_status = pn.pane.Markdown(
            "Choisir le contenu et le format, puis générer un aperçu."
        )
        self._preview_generation = 0
        self.png_preview_image = pn.pane.PNG(None, sizing_mode="scale_width", max_width=480)
        self.png_preview_button.on_click(self._preview_png)
        self.png_target.param.watch(self._invalidate_png_preview, "value")
        self.png_format.param.watch(self._invalidate_png_preview, "value")
        self.prompt = pn.widgets.TextInput(
            label="",
            placeholder="Ex. Compare le vent à 10 m, la pression et le satellite sur six panneaux…",
            sizing_mode="stretch_width",
        )
        self.prompt_button = pn.widgets.Button(label="Préparer", color="primary", width=110)
        self.prompt_apply = pn.widgets.Button(
            label="Confirmer et appliquer", color="success", disabled=True, width=190
        )
        self.prompt_result = pn.pane.Markdown("")
        self.prompt_details = pn.Accordion(
            ("Résultat de l’assistant", self.prompt_result),
            active=[],
            sizing_mode="stretch_width",
            visible=False,
        )
        self.prompt_bar = pn.Column(
            pn.Row(
                pn.pane.Markdown("**Piloter Weather Desk**", width=180, margin=(0, 8, 0, 0)),
                self.prompt,
                self.prompt_button,
                self.prompt_apply,
                sizing_mode="stretch_width",
                styles={"align-items": "center", "gap": "10px"},
            ),
            self.prompt_details,
            sizing_mode="stretch_width",
            styles={
                "position": "fixed",
                "bottom": "0",
                "left": "0",
                "width": "100vw",
                "z-index": "100",
                "background": "#ffffff",
                "border-top": "1px solid #d5e0e6",
                "padding": "12px 16px",
                "box-shadow": "0 -4px 16px rgba(18, 48, 68, 0.12)",
            },
        )
        self.pending_proposal = None
        self.prompt_button.on_click(self._prompt_proposal)
        self.prompt_apply.on_click(self._apply_prompt_proposal)
        for key, field in self.fields.items():
            field.sizing_mode = "stretch_width"
            if key != "valid_time":
                field.param.watch(lambda event: self._editor_changed(), "value")
        self.view = pn.template.FastListTemplate(
            title="Weather Desk",
            accent_base_color="#176b87",
            header_background="#123044",
            header=[self.prompt_bar],
            sidebar=[
                pn.pane.Markdown("## Poste multi-panneaux"),
                self.layout,
                pn.pane.Markdown("## Analyses"),
                self.analysis_name,
                pn.Row(self.rename_analysis_button, self.duplicate_analysis_button),
                self.analysis_select,
                self.reload_conflict_button,
                self.freeze_edition_button,
                self.edition_select,
                self.edition_download,
                self.live.controls,
                pn.pane.Markdown("## Contexte de l'analyse"),
                *[self.fields[k] for k in ("zone", "valid_time", "confidence")],
                pn.pane.Markdown("## Export"),
                *self.downloads,
                self.png_target,
                self.png_format,
                self.png_preview_button,
                self.png_download,
                pn.pane.Markdown("Les textes et annotations sont sauvegardés automatiquement."),
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
                pn.Card(
                    self.png_preview_status,
                    self.png_preview_image,
                    title="Aperçu PNG avant publication",
                    collapsed=True,
                    sizing_mode="stretch_width",
                ),
                pn.Spacer(height=100),
            ],
            main_max_width="1600px",
        )
        if self.workspace_state:
            first_panel = self.workspace_state.panels[0]
            self.live.current_product = first_panel.satellite_product or self.live.current_product
            self._syncing_workspace = True
            try:
                self.live.model_fields.value = [
                    field
                    for field in first_panel.fields
                    if field in self.live.model_fields.options.values()
                ]
            finally:
                self._syncing_workspace = False
        self.refresh()

    def _make_map(self, index: int):
        plot = figure(
            title=f"Panneau {index}",
            x_range=self.map_x_range,
            y_range=self.map_y_range,
            x_axis_type=None,
            y_axis_type=None,
            height=640,
            sizing_mode="stretch_width",
            tools="pan,wheel_zoom,reset,save",
            active_scroll="wheel_zoom",
            toolbar_location="above",
        )
        plot.background_fill_color = "#e7f0f3"
        plot.xgrid.visible = False
        plot.ygrid.visible = False
        self.tiles = getattr(self, "tiles", [])
        self.land_sources = getattr(self, "land_sources", [])
        tile = plot.add_tile(
            WMTSTileSource(
                url="https://tile.openstreetmap.org/{Z}/{X}/{Y}.png",
                attribution='© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
            )
        )
        tile.visible = self.projection == "mercator"
        self.tiles.append(tile)
        land_source = ColumnDataSource(dict(xs=[], ys=[]), name=f"land_outline_{index}")
        land_xs, land_ys = land_lines(self.projection)
        land_source.data = dict(xs=land_xs, ys=land_ys)
        land_fill_source = ColumnDataSource(dict(xs=[], ys=[]), name=f"land_fill_{index}")
        fill_xs, fill_ys = land_polygons(self.projection)
        land_fill_source.data = dict(xs=fill_xs, ys=fill_ys)
        land_fill = plot.patches(
            xs="xs",
            ys="ys",
            source=land_fill_source,
            fill_color="#dce6dc",
            fill_alpha=0.92,
            line_alpha=0,
        )
        self.land_fill_renderers.append(land_fill)
        self.land_fill_sources.append(land_fill_source)
        plot.multi_line(
            xs="xs",
            ys="ys",
            source=land_source,
            line_color="#546b74",
            line_width=1,
            line_alpha=0.9,
        )
        self.land_sources.append(land_source)
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
                source.on_change("data", lambda attr, old, new: self._editor_changed())
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

    def _projection_changed(self, event):
        if self._syncing_workspace:
            return
        self._switch_projection(event.new, persist=True)

    def _switch_projection(self, projection, persist):
        if projection == self.projection:
            return
        old = self.projection
        camera = self._camera_from_ranges()
        convert_old = transformer(old, inverse=True)
        convert_new = transformer(projection)
        for layer in self.layers.values():
            data = layer["source"].data
            xs, ys = [], []
            for line_x, line_y in zip(data["xs"], data["ys"], strict=True):
                mercator_x, mercator_y = convert_old(line_x, line_y)
                x, y = convert_new(mercator_x, mercator_y)
                xs.append(list(x))
                ys.append(list(y))
            layer["source"].data = {**data, "xs": xs, "ys": ys}
        self.projection = projection
        self.live.set_projection(projection)
        land_xs, land_ys = land_lines(projection)
        for source in self.land_sources:
            source.data = dict(xs=land_xs, ys=land_ys)
        fill_xs, fill_ys = land_polygons(projection)
        for source in self.land_fill_sources:
            source.data = dict(xs=fill_xs, ys=fill_ys)
        for tile in self.tiles:
            tile.visible = projection == "mercator"
        if persist and self.workspace_service is not None:
            self._camera_dirty = True
        self._apply_camera_values(camera)
        if hasattr(self, "png_preview_button"):
            self._invalidate_png_preview(None)

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
        except WorkspaceConflict as exc:
            self._mark_workspace_conflict(exc)

    def _refresh_analysis_options(self):
        states = self.workspace_service.list()
        options = {
            f"{state.name} · {state.workspace_id[:8]}": state.workspace_id for state in states
        }
        self._syncing_workspace = True
        try:
            self.analysis_select.options = options
            self.analysis_select.value = self.workspace_state.workspace_id
            self.analysis_select.disabled = False
        finally:
            self._syncing_workspace = False

    def _refresh_editions(self):
        records = self.workspace_service.list_editions(self.workspace_state.workspace_id)
        options = {
            f"{record['created_at']} · révision {record['workspace_revision']}": record[
                "edition_id"
            ]
            for record in records
        }
        self.edition_select.options = options
        self.edition_select.value = next(iter(options.values()), None)
        self.edition_select.disabled = not options
        self.edition_download.disabled = not options

    def _edition_selected(self, event):
        self.edition_download.disabled = not bool(event.new)

    def _freeze_edition(self, event):
        try:
            manifest, annotations = self.snapshot()
            edition_id = str(uuid4())
            created_at = utc_now()
            manifest["edition"] = {
                "edition_id": edition_id,
                "created_at_utc": created_at,
                "workspace_id": self.workspace_state.workspace_id,
                "workspace_revision": self.workspace_state.revision,
                "immutable": True,
            }
            artifacts = {
                f"images/{source.key}.png": source.normalized
                for source in self.sources
                if source.normalized
            }
            artifacts.update(self.live.artifacts())
            self.workspace_service.create_edition(
                edition_id=edition_id,
                workspace_id=self.workspace_state.workspace_id,
                expected_revision=self.workspace_state.revision,
                created_at=created_at,
                manifest=manifest,
                annotations=annotations,
                artifacts=artifacts,
            )
            self._refresh_editions()
            self.status.object = f"Édition figée et archivée : `{edition_id}`."
            self.status.alert_type = "success"
        except (ValueError, WorkspaceConflict) as exc:
            self.status.object = f"Édition non figée : {exc}"
            self.status.alert_type = "danger"

    def _download_edition(self):
        edition_id = self.edition_select.value
        if not edition_id:
            raise ValueError("Sélectionner une édition archivée.")
        edition = self.workspace_service.read_edition(edition_id)
        images = {
            name[len("images/") : -len(".png")]: content
            for name, content in edition.artifacts.items()
            if name.startswith("images/") and name.endswith(".png")
        }
        live = {
            name: content for name, content in edition.artifacts.items() if name.startswith("live/")
        }
        return export_bundle(edition.manifest, edition.annotations, images, live)

    def _open_analysis(self, event):
        if self._syncing_workspace or not event.new:
            return
        self.workspace_state = self.workspace_service.read(event.new)
        self._has_local_conflict = False
        self.reload_conflict_button.visible = False
        self.analysis_name.value = self.workspace_state.name
        self._apply_workspace_state(self.workspace_state)
        self._refresh_analysis_options()
        self._refresh_editions()

    def _rename_analysis(self, event):
        name = self.analysis_name.value.strip()
        try:
            self.workspace_state = self.workspace_service.apply(
                [{"op": "rename", "name": name}],
                expected_revision=self.workspace_state.revision,
                workspace_id=self.workspace_state.workspace_id,
            )
            self.status.object = f"Analyse « {name} » enregistrée."
            self.status.alert_type = "success"
            self._refresh_analysis_options()
        except WorkspaceConflict as exc:
            self._mark_workspace_conflict(exc)
        except ValueError as exc:
            self.status.object = f"Nom non enregistré : {exc}"
            self.status.alert_type = "danger"

    def _duplicate_analysis(self, event):
        name = self.analysis_name.value.strip()
        try:
            self.workspace_state = self.workspace_service.duplicate(
                self.workspace_state.workspace_id, f"{name} — copie"
            )
            self.analysis_name.value = self.workspace_state.name
            self._apply_workspace_state(self.workspace_state)
            self.status.object = f"Analyse dupliquée sous « {self.workspace_state.name} »."
            self.status.alert_type = "success"
            self._refresh_analysis_options()
            self._refresh_editions()
        except (WorkspaceConflict, ValueError) as exc:
            if isinstance(exc, WorkspaceConflict):
                self._mark_workspace_conflict(exc)
            else:
                self.status.object = f"Duplication impossible : {exc}"
                self.status.alert_type = "danger"

    def sync_workspace(self):
        if self.workspace_service is None:
            return
        incoming = self.workspace_service.read(self.workspace_state.workspace_id)
        if self._has_local_conflict:
            return
        if incoming.revision == self.workspace_state.revision:
            if self._camera_dirty:
                try:
                    camera = self._camera_from_ranges()
                    self.workspace_state = self.workspace_service.apply(
                        [
                            {"op": "set_camera", "camera": camera},
                            {"op": "set_projection", "projection": self.projection},
                        ],
                        expected_revision=self.workspace_state.revision,
                        workspace_id=self.workspace_state.workspace_id,
                    )
                except WorkspaceConflict as exc:
                    self._mark_workspace_conflict(exc)
                else:
                    self._camera_dirty = False
            return
        self.workspace_state = incoming
        self.analysis_name.value = incoming.name
        self._apply_workspace_state(incoming)
        self._refresh_analysis_options()
        self._refresh_editions()

    def _apply_workspace_state(self, incoming):
        projection_changed = incoming.projection != self.projection
        time_changed = incoming.reference_time != self.live.reference_time
        self._syncing_workspace = True
        try:
            self.analysis_name.value = incoming.name
            self.layout.value = incoming.layout
            self.fields["valid_time"].value = incoming.reference_time
            for key, value in (incoming.editorial or {}).items():
                if key in self.fields:
                    self.fields[key].value = value
            self.live.projection_control.value = incoming.projection
        finally:
            self._syncing_workspace = False
        if projection_changed:
            self._set_projection(incoming.projection)
        if time_changed:
            self.live.set_reference_time(incoming.reference_time)
        self._restore_annotations(incoming.annotations)
        self._apply_layout(incoming.layout, len(incoming.panels))
        if incoming.panels:
            panel = incoming.panels[0]
            self._syncing_workspace = True
            try:
                if panel.satellite_product:
                    self.live.current_product = panel.satellite_product
                if (
                    panel.satellite_product
                    and panel.satellite_product in self.live.sat_product.options.values()
                ):
                    self.live.sat_product.value = panel.satellite_product
                self.live.model_fields.value = [
                    field
                    for field in panel.fields
                    if field in self.live.model_fields.options.values()
                ]
            finally:
                self._syncing_workspace = False
        self._apply_camera(incoming.camera)

    def _editor_changed(self):
        if self._syncing_workspace:
            return
        self.refresh()
        if self.workspace_service is None or not hasattr(self, "layers"):
            return
        try:
            state = self.workspace_service.apply(
                [
                    {
                        "op": "set_editorial",
                        "value": {
                            key: self.fields[key].value
                            for key in (
                                "zone",
                                "confidence",
                                "headline",
                                "analysis",
                                "impacts",
                                "limitations",
                            )
                        },
                    },
                    {"op": "set_annotations", "value": self._annotations_geojson()},
                ],
                expected_revision=self.workspace_state.revision,
                workspace_id=self.workspace_state.workspace_id,
            )
            self.workspace_state = state
        except WorkspaceConflict as exc:
            self._mark_workspace_conflict(exc)

    def _panel_fields_changed(self, event):
        if self._syncing_workspace or self.workspace_service is None:
            return
        self._save_panel_changes({"fields": list(event.new)})

    def _panel_product_changed(self, event):
        if self._syncing_workspace or self.workspace_service is None or not event.new:
            return
        self._save_panel_changes({"satellite_product": event.new})

    def _save_panel_changes(self, changes):
        try:
            panel = self.workspace_state.panels[0]
            self.workspace_state = self.workspace_service.apply(
                [{"op": "configure_panel", "panel_id": panel.panel_id, "changes": changes}],
                expected_revision=self.workspace_state.revision,
                workspace_id=self.workspace_state.workspace_id,
            )
        except WorkspaceConflict as exc:
            self._mark_workspace_conflict(exc)

    def _annotations_geojson(self):
        convert = transformer(self.projection, inverse=True)
        drawing_layers = {}
        for key, layer in self.layers.items():
            data = layer["source"].data
            xs, ys = [], []
            for line_x, line_y in zip(data["xs"], data["ys"], strict=True):
                x, y = convert(line_x, line_y)
                xs.append(list(x))
                ys.append(list(y))
            drawing_layers[key] = {
                "data": {**data, "xs": xs, "ys": ys},
                "geometry": layer["geometry"],
            }
        return drawings_geojson(drawing_layers, self.fields["valid_time"].value)

    def _restore_annotations(self, collection):
        if not isinstance(collection, dict):
            return
        grouped = {key: {"xs": [], "ys": []} for key in self.layers}
        convert = transformer(self.projection)
        for feature in collection.get("features", []):
            kind = feature.get("properties", {}).get("kind")
            geometry = feature.get("geometry", {})
            if kind not in grouped or geometry.get("type") != self.layers[kind]["geometry"]:
                continue
            coordinates = geometry.get("coordinates", [])
            if geometry["type"] == "Polygon":
                coordinates = coordinates[0] if coordinates else []
            if len(coordinates) < (3 if geometry["type"] == "Polygon" else 2):
                continue
            mercator = [to_mercator(point[0], point[1]) for point in coordinates]
            x, y = convert([point[0] for point in mercator], [point[1] for point in mercator])
            grouped[kind]["xs"].append(list(x))
            grouped[kind]["ys"].append(list(y))
        self._syncing_workspace = True
        try:
            for kind, data in grouped.items():
                self.layers[kind]["source"].data = data
        finally:
            self._syncing_workspace = False

    def _mark_workspace_conflict(self, error):
        self._has_local_conflict = True
        self.reload_conflict_button.visible = True
        self.status.object = (
            f"Conflit de sauvegarde : {error} Vos modifications locales sont conservées "
            "jusqu'au rechargement explicite."
        )
        self.status.alert_type = "danger"

    def _reload_workspace_conflict(self, event):
        self.workspace_state = self.workspace_service.read(self.workspace_state.workspace_id)
        self._has_local_conflict = False
        self.reload_conflict_button.visible = False
        self._apply_workspace_state(self.workspace_state)
        self._refresh_analysis_options()
        self._refresh_editions()
        self.status.object = (
            f"Version enregistrée rechargée (révision {self.workspace_state.revision})."
        )
        self.status.alert_type = "info"

    def _reference_time_changed(self, event):
        if self._syncing_workspace:
            return
        try:
            resolved = resolve_ifs_time(event.new)
            self._syncing_workspace = True
            try:
                self.fields["valid_time"].value = resolved.requested_time
            finally:
                self._syncing_workspace = False
            if self.workspace_service is not None:
                self.workspace_state = self.workspace_service.apply(
                    [{"op": "set_reference_time", "value": resolved.requested_time}],
                    expected_revision=self.workspace_state.revision,
                    workspace_id=self.workspace_state.workspace_id,
                )
            self.live.set_reference_time(resolved.requested_time)
        except WorkspaceConflict as exc:
            self._mark_workspace_conflict(exc)
        except ValueError as exc:
            self.live.time_status.object = f"**Heure de référence invalide : {exc}**"
            self.status.object = str(exc)
            self.status.alert_type = "danger"

    def _camera_changed(self, attr, old, new):
        if not self._syncing_workspace:
            self._camera_dirty = self.workspace_service is not None
            if hasattr(self, "png_preview_button"):
                self._invalidate_png_preview(None)

    def _camera_from_ranges(self):
        west, south = self.map_x_range.start, self.map_y_range.start
        east, north = self.map_x_range.end, self.map_y_range.end
        mercator_x, mercator_y = transformer(self.projection, inverse=True)(
            (west + east) / 2, (south + north) / 2
        )
        longitude, latitude = to_lonlat(mercator_x, mercator_y)
        viewport_width = max(float(self.maps[0].width or 640), 1)
        zoom = math.log2(40_075_016.686 * viewport_width / (256 * max(east - west, 1)))
        return {
            "longitude": longitude,
            "latitude": latitude,
            "zoom": max(0, min(24, zoom)),
        }

    def _apply_camera(self, camera):
        center_x, center_y = project_xy(
            *to_mercator(camera.longitude, camera.latitude), self.projection
        )
        viewport_width = max(float(self.maps[0].width or 640), 1)
        span_x = 40_075_016.686 * viewport_width / (256 * (2**camera.zoom))
        span_y = span_x * (self.maps[0].height or 640) / viewport_width
        self._apply_camera_values(camera, center=(center_x, center_y), span=(span_x, span_y))

    def _apply_camera_values(self, camera, center=None, span=None):
        if center is None or span is None:
            center = project_xy(
                *to_mercator(camera["longitude"], camera["latitude"]), self.projection
            )
            zoom = camera["zoom"]
            viewport_width = max(float(self.maps[0].width or 640), 1)
            span_x = 40_075_016.686 * viewport_width / (256 * (2**zoom))
            span = (span_x, span_x * (self.maps[0].height or 640) / viewport_width)
        center_x, center_y = center
        span_x, span_y = span
        self._syncing_workspace = True
        try:
            self.map_x_range.start, self.map_x_range.end = (
                center_x - span_x / 2,
                center_x + span_x / 2,
            )
            self.map_y_range.start, self.map_y_range.end = (
                center_y - span_y / 2,
                center_y + span_y / 2,
            )
        finally:
            self._syncing_workspace = False

    def _set_projection(self, projection):
        self._switch_projection(projection, persist=False)

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
        if self.live.has_unresolved_selection:
            raise ValueError(
                "Export suspendu : attendre le chargement du modèle correspondant à la sélection."
            )
        fields = {key: widget.value for key, widget in self.fields.items()}
        convert = transformer(self.projection, inverse=True)
        drawing_layers = {}
        for key, layer in self.layers.items():
            data = layer["source"].data
            xs, ys = [], []
            for line_x, line_y in zip(data["xs"], data["ys"], strict=True):
                x, y = convert(line_x, line_y)
                xs.append(list(x))
                ys.append(list(y))
            drawing_layers[key] = {
                "data": {**data, "xs": xs, "ys": ys},
                "geometry": layer["geometry"],
            }
        annotations = drawings_geojson(drawing_layers, fields["valid_time"])
        manifest = {
            "schema_version": 1,
            "analysis_id": self.analysis_id,
            "created_at_utc": self.created,
            "exported_at_utc": utc_now(),
            "analysis": fields,
            "sources": [source.metadata() for source in self.sources] + self.live.metadata(),
            "map_view": {
                "crs": PROJECTIONS[self.projection][1],
                "projection": self.projection,
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
        if hasattr(self, "png_preview_button") and not self.png_download.disabled:
            self._invalidate_png_preview(None)
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

    def _invalidate_png_preview(self, event):
        self._preview_generation += 1
        self.png_download.file = None
        self.png_download.disabled = True
        self.png_preview_image.object = None
        self.png_preview_status.object = "Format ou contenu modifié; générer un nouvel aperçu."

    async def _preview_png(self, event):
        """Render and show the exact PNG before enabling its download."""
        generation = self._preview_generation
        self.png_preview_button.disabled = True
        self.png_download.disabled = True
        self.png_preview_status.object = "Rendu de l'aperçu…"
        if self.png_target.value == "panel":
            target = self.maps[0]
            filename = "weather-desk-panel.png"
        else:
            target = self.map_grid.get_root()
            filename = "weather-desk-composition.png"
        title = "Weather Desk — " + (self.fields["zone"].value or "Analyse météo")
        metadata = self.live.metadata()
        legend_parts = []
        attributions = (
            ["© OpenStreetMap contributors"]
            if self.projection == "mercator"
            else ["Natural Earth 1:110m (domaine public)"]
        )
        for source in metadata:
            if source.get("visible") is False:
                continue
            if source.get("kind") == "wms":
                legend_parts.append(f"{source['source']} · {source['units']}")
            for field_id, field in source.get("parameters", {}).items():
                if source.get("visible_parameters", {}).get(field_id, True):
                    legend_parts.append(f"{field['title']} ({field['units']})")
            if source.get("attribution"):
                attributions.append(source["attribution"])
        legend = "; ".join(legend_parts) or "Fond cartographique"
        credits = " · ".join(dict.fromkeys(attributions))
        try:
            manifest, _ = self.snapshot()
            html_document = prepare_png_document(target, format=self.png_format.value)
            output = await asyncio.to_thread(
                render_png,
                html_document,
                title=title,
                valid_time=format_png_validity(manifest),
                legend=legend,
                credits=credits,
                format=self.png_format.value,
            )
            if generation != self._preview_generation:
                self.png_preview_status.object = "La vue a changé pendant le rendu; réessayer."
                return
            suffix = "square" if self.png_format.value == "square" else "portrait"
            self.png_download.filename = filename.replace(".png", f"-{suffix}.png")
            self.png_download.file = output
            self.png_preview_image.object = output.getvalue()
            dimensions = "1080 × 1080" if self.png_format.value == "square" else "1080 × 1350"
            self.png_preview_status.object = (
                f"Aperçu **{dimensions} px**. "
                "Le cadrage conserve toute la carte; les bandes de fond restent visibles."
            )
            self.png_download.disabled = False
        except Exception as exc:
            self.status.object = f"Export PNG impossible : {exc}"
            self.status.alert_type = "danger"
            self.png_preview_status.object = f"**Aperçu impossible :** {exc}"
        finally:
            self.png_preview_button.disabled = False

    async def _prompt_proposal(self, event):
        self.prompt_button.disabled = True
        self.prompt_apply.disabled = True
        self.pending_proposal = None
        self.prompt_details.visible = True
        self.prompt_details.active = [0]
        self.prompt_result.object = "Préparation de la proposition…"
        try:
            if self.workspace_service is None:
                raise RuntimeError("Le workspace partagé n'est pas disponible.")
            proposal = await asyncio.to_thread(
                propose_commands,
                self.prompt.value,
                self.workspace_service,
                self.workspace_state.workspace_id,
            )
            self.pending_proposal = proposal
            self.prompt_result.object = (
                "**Proposition non appliquée — vérifiez les changements :**\n\n"
                "```json\n"
                + json.dumps(proposal["commands"], ensure_ascii=False, indent=2)
                + "\n```\n\n"
                f"Révision attendue : `{proposal['expected_revision']}`."
            )
            self.prompt_apply.disabled = False
        except Exception as exc:
            self.prompt_result.object = f"**Aucune modification effectuée :** {exc}"
        finally:
            self.prompt_button.disabled = False

    def _apply_prompt_proposal(self, event):
        proposal = self.pending_proposal
        if proposal is None:
            return
        try:
            self.workspace_state = self.workspace_service.apply(
                proposal["commands"],
                expected_revision=proposal["expected_revision"],
                workspace_id=proposal["workspace_id"],
            )
            self._apply_workspace_state(self.workspace_state)
            self.prompt_result.object = (
                f"Proposition appliquée à la révision `{self.workspace_state.revision}`."
            )
        except WorkspaceConflict as exc:
            self.prompt_result.object = f"**Proposition périmée :** {exc}. Recommencer le prompt."
        except Exception as exc:
            self.prompt_result.object = f"**Proposition refusée :** {exc}"
        finally:
            self.pending_proposal = None
            self.prompt_apply.disabled = True
