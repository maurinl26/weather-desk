"""Nonblocking Panel integration; raster animation and opacity run in the browser."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import panel as pn
from bokeh.models import CDSView, ColumnDataSource, HoverTool, IndexFilter

from weather_desk.analysis import json_bytes
from weather_desk.cartography import PROJECTIONS, reproject_lines, warp_rgba
from weather_desk.data import (
    DATA,
    MODEL_FIELDS,
    SATELLITE_LAYER,
    ModelFrame,
    SatelliteFrame,
    iso,
    parse_time,
)

LOG = logging.getLogger(__name__)
WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="weather-data")


class LiveLayers:
    def __init__(self, plot, changed, service=DATA, projection="mercator"):
        self.plots = list(plot) if isinstance(plot, (list, tuple)) else [plot]
        if not self.plots:
            raise ValueError("Au moins une carte est nécessaire.")
        self.plot, self.changed, self.service = self.plots[0], changed, service
        self.pending = {}
        self.satellites: list[SatelliteFrame] = []
        self.products = {}
        self.current_product = SATELLITE_LAYER
        self.current_product_title = "SEVIRI WV6.2 µm"
        self._arrival_order: list[str] = []
        self._missing_frames: list[str] = []
        self.model: ModelFrame | None = None
        self.projection = projection
        self._poller = None
        self._closed = False
        self._setting_controls = False
        self.sat_enabled = pn.widgets.Checkbox(label="Satellite", value=True)
        self.projection_control = pn.widgets.Select(
            label="Projection",
            options={title: key for key, (title, _) in PROJECTIONS.items()},
            value=projection,
        )
        self.model_fields = pn.widgets.CheckBoxGroup(
            label="Champs IFS",
            options={spec.title: field_id for field_id, spec in MODEL_FIELDS.items()},
            value=["msl", "gh500"],
            inline=False,
        )
        self.opacity = pn.widgets.FloatSlider(
            label="Opacité vapeur d'eau", start=0, end=1, step=0.05, value=0.8
        )
        self.player = pn.widgets.Player(
            label="Satellite • UTC",
            start=0,
            end=0,
            value=0,
            interval=700,
            width=290,
            visible_buttons=["first", "previous", "pause", "play", "next", "last"],
            show_loop_controls=False,
            loop_policy="loop",
            disabled=True,
        )
        self.sat_status = pn.pane.Markdown("Satellite : en attente de chargement.")
        self.model_status = pn.pane.Markdown("IFS : en attente de chargement.")
        self.time_status = pn.pane.Markdown("")
        self.refresh_sat = pn.widgets.Button(label="Actualiser le satellite", color="primary")
        self.sat_product = pn.widgets.Select(
            label="Produit EUMETSAT", options={}, value=None, disabled=True
        )
        self.refresh_model = pn.widgets.Button(label="Dernier run IFS")
        self.run = pn.widgets.Select(label="Run IFS (UTC)", options=[], disabled=True)
        self.step = pn.widgets.IntSlider(label="Échéance IFS (h)", start=0, end=72, step=3, value=0)
        self.sat_data = ColumnDataSource(
            dict(image=[], x=[], y=[], dw=[], dh=[]), name="satellite_frames"
        )
        self.sat_filter = IndexFilter(indices=[], name="satellite_frame_filter")
        self.frame_order = ColumnDataSource(dict(index=[]), name="satellite_frame_order")
        self.field_data = {
            field_id: ColumnDataSource(dict(xs=[], ys=[], level=[]), name=f"ifs_{field_id}")
            for field_id in MODEL_FIELDS
        }
        self.msl_data = self.field_data["msl"]
        self.gh_data = self.field_data["gh500"]
        self.sat_renderers = []
        self.field_renderers = {field_id: [] for field_id in MODEL_FIELDS}
        for plot in self.plots:
            sat_renderer = plot.image_rgba(
                image="image",
                x="x",
                y="y",
                dw="dw",
                dh="dh",
                source=self.sat_data,
                view=CDSView(filter=self.sat_filter),
                global_alpha=self.opacity.value,
            )
            # Keep raster above basemap and below contours and every annotation.
            plot.renderers.remove(sat_renderer)
            plot.renderers.insert(1, sat_renderer)
            field_renderers = []
            for field_id, spec in MODEL_FIELDS.items():
                renderer = plot.multi_line(
                    xs="xs",
                    ys="ys",
                    source=self.field_data[field_id],
                    line_color=spec.color,
                    line_width=1.6,
                    line_dash=spec.dash,
                )
                field_renderers.append((field_id, renderer))
            for index, (field_id, renderer) in enumerate(field_renderers, 2):
                spec = MODEL_FIELDS[field_id]
                plot.renderers.remove(renderer)
                plot.renderers.insert(index, renderer)
                plot.add_tools(
                    HoverTool(
                        renderers=[renderer],
                        tooltips=[(spec.title, f"@level{{0}} {spec.units}")],
                        line_policy="nearest",
                    )
                )
                self.field_renderers[field_id].append(renderer)
            self.sat_renderers.append(sat_renderer)
        # Keep the first renderer attributes for callers that inspect the original single map.
        self.sat_renderer = self.sat_renderers[0]
        self.msl_renderer = self.field_renderers["msl"][0]
        self.gh_renderer = self.field_renderers["gh500"][0]
        # Preloaded arrays stay in the browser: changing the frame never re-sends pixels.
        self.player.jscallback(
            args={"frame_filter": self.sat_filter, "frame_order": self.frame_order},
            value=(
                "const i = frame_order.data.index[source.value]; "
                "if (Number.isInteger(i)) frame_filter.indices = [i]"
            ),
        )
        for renderer in self.sat_renderers:
            self.opacity.jslink(renderer.glyph, value="global_alpha")
            self.sat_enabled.jslink(renderer, value="visible")
        self._sync_field_visibility()
        self.player.param.watch(lambda event: self._context_changed(), "value")
        for control in (self.sat_enabled, self.model_fields, self.opacity):
            control.param.watch(lambda event: self._context_changed(), "value")
        self.model_fields.param.watch(lambda event: self.load_model(), "value")
        self.refresh_sat.on_click(lambda event: self.load_satellite())
        self.sat_product.param.watch(self._product_selected, "value")
        self.refresh_model.on_click(lambda event: self.load_latest_model())
        self.run.param.watch(lambda event: self.load_model(), "value")
        self.step.param.watch(lambda event: self.load_model(), "value_throttled")
        for component in (self.sat_status, self.model_status, self.time_status):
            component.sizing_mode = "stretch_width"
        self.controls = pn.Column(
            pn.pane.Markdown("## Couches météo"),
            self.projection_control,
            self.sat_enabled,
            self.opacity,
            self.player,
            self.sat_product,
            self.refresh_sat,
            self.sat_status,
            self.model_fields,
            self.run,
            self.step,
            self.refresh_model,
            self.model_status,
            sizing_mode="stretch_width",
        )

    def set_projection(self, projection: str):
        if projection not in PROJECTIONS:
            raise ValueError("Projection cartographique inconnue.")
        self.projection = projection
        if self.satellites:
            warped = [warp_rgba(frame.rgba, projection) for frame in self.satellites]
            images = [item[0] for item in warped]
            x, y, width, height = warped[0][1]
            self.sat_data.data = dict(
                image=list(images),
                x=[x] * len(images),
                y=[y] * len(images),
                dw=[width] * len(images),
                dh=[height] * len(images),
            )
        self._project_model()

    def start(self):
        if self._poller is None:
            self._poller = pn.state.add_periodic_callback(self.poll, period=150, start=False)
        self.load_satellite()
        self.load_latest_model()
        if pn.state.curdoc and pn.state.curdoc.session_context:
            pn.state.on_session_destroyed(self.close)

    def close(self, session_context):
        self._closed = True
        for future, _ in self.pending.values():
            future.cancel()
        self.pending.clear()
        if self._poller:
            self._poller.stop()

    def submit(self, channel, work, apply):
        if self._closed:
            return
        old = self.pending.pop(channel, None)
        if old:
            old[0].cancel()
        self.pending[channel] = (WORKERS.submit(work), apply)
        if self._poller and not self._poller.running:
            self._poller.start()

    def poll(self):
        for channel, (future, apply) in list(self.pending.items()):
            if not future.done():
                continue
            # Only the currently tracked request may update the map.
            if self.pending.get(channel, (None,))[0] is not future:
                continue
            del self.pending[channel]
            try:
                apply(future.result())
            except Exception:
                LOG.exception("weather_load_failed channel=%s", channel)
                message = (
                    "Chargement indisponible. Réessayer ; "
                    "les données déjà affichées sont conservées."
                )
                if channel.startswith("satellite:"):
                    self._missing_frames.append(channel.split(":", 1)[1])
                    self._history_status()
                elif channel.startswith("sat"):
                    self.sat_status.object = f"**Satellite : {message}**"
                    self.refresh_sat.disabled = False
                else:
                    self.model_status.object = f"**IFS : {message}**"
                    self.refresh_model.disabled = False
        if not self.pending and self._poller:
            self._poller.stop()

    def load_satellite(self):
        for channel in list(self.pending):
            if channel.startswith("satellite"):
                self.pending.pop(channel)[0].cancel()
        self._missing_frames = []
        self.refresh_sat.disabled = True
        self.player.direction = 0
        self.sat_status.object = "Recherche des dernières images EUMETSAT…"
        self.submit("satellite", self.service.satellite_catalogue, self._catalogue_loaded)

    def _catalogue_loaded(self, times):
        if not times:
            raise ValueError("Catalogue satellite vide.")
        self.products = {product.product_id: product for product in times}
        options = {product.title: product.product_id for product in times}
        preferred = self.current_product
        if preferred not in self.products:
            preferred = SATELLITE_LAYER if SATELLITE_LAYER in self.products else times[0].product_id
        self._setting_controls = True
        try:
            self.sat_product.options = options
            self.sat_product.value = preferred
            self.sat_product.disabled = False
        finally:
            self._setting_controls = False
        self._request_product(preferred)

    def _product_selected(self, event):
        if event.new and not self._setting_controls:
            self._request_product(event.new)

    def _request_product(self, product_id):
        product = self.products.get(product_id)
        if product is None:
            return
        for channel in list(self.pending):
            if channel.startswith("satellite"):
                self.pending.pop(channel)[0].cancel()
        self.current_product = product.product_id
        self.current_product_title = product.title
        times = list(product.times)
        if not times:
            self.sat_status.object = f"**Satellite : aucune échéance pour {product.title}.**"
            return
        self._missing_frames = []
        self.refresh_sat.disabled = True
        self.player.disabled = True
        self.player.direction = 0
        self.sat_status.object = f"Chargement {product.title}…"
        self.submit(
            "satellite",
            lambda: self.service.satellite(product.times[-1], product.product_id, product.title),
            lambda frame: self._latest_satellite_loaded(frame, times, product),
        )

    def _latest_satellite_loaded(self, frame, times, product):
        self.apply_satellite([frame])
        for stamp in times[:-1]:
            self.submit(
                f"satellite:{stamp}",
                lambda stamp=stamp: self.service.satellite(
                    stamp, product.product_id, product.title
                ),
                self._history_frame_loaded,
            )
        self._history_status()

    def _history_status(self):
        remaining = sum(key.startswith("satellite:") for key in self.pending)
        count = len(self.satellites)
        self.sat_status.object = f"{count} image(s) préchargée(s) · © EUMETSAT"
        if remaining:
            self.sat_status.object += f" · {remaining} en cours…"
        if self._missing_frames:
            self.sat_status.object += (
                f" · ⚠ {len(self._missing_frames)} manquante(s), séquence incomplète"
            )
        self.refresh_sat.disabled = bool(remaining)

    def _history_frame_loaded(self, frame):
        selected = self.selected_satellite().valid_time
        self._arrival_order.append(frame.valid_time)
        self.satellites = sorted([*self.satellites, frame], key=lambda f: f.valid_time)
        image, bounds = warp_rgba(frame.rgba, self.projection)
        x0, y0, width, height = bounds
        # Stream only the new buffer, not every previously loaded image.
        self.sat_data.stream(dict(image=[image], x=[x0], y=[y0], dw=[width], dh=[height]))
        self._set_frame_order(selected)
        self._history_status()
        self._context_changed()

    def _set_frame_order(self, selected):
        order = [self._arrival_order.index(f.valid_time) for f in self.satellites]
        self.frame_order.data = dict(index=order)
        self.player.end = len(order) - 1
        self.player.value = next(
            i for i, f in enumerate(self.satellites) if f.valid_time == selected
        )
        self.sat_filter.indices = [order[self.player.value]]
        self.player.disabled = len(order) < 2

    def apply_satellite(self, frames):
        self.player.direction = 0
        self.satellites = sorted(frames, key=lambda f: f.valid_time)
        self._arrival_order = [f.valid_time for f in self.satellites]
        count = len(frames)
        warped = [warp_rgba(frame.rgba, self.projection) for frame in self.satellites]
        images = [item[0] for item in warped]
        x0, y0, width, height = warped[0][1]
        self.sat_data.data = dict(
            image=images,
            x=[x0] * count,
            y=[y0] * count,
            dw=[width] * count,
            dh=[height] * count,
        )
        self._set_frame_order(self.satellites[-1].valid_time)
        self._history_status()
        self._context_changed()

    def load_latest_model(self):
        self.refresh_model.disabled = True
        self.model_status.object = "Recherche du dernier run IFS…"
        self.submit("model", self.service.latest_run, self._run_loaded)

    def _run_loaded(self, run):
        self._setting_controls = True
        try:
            stamp = parse_time(run)
            self.run.options = [iso(stamp - timedelta(hours=6 * i)) for i in range(4)]
            self.run.value = run
            self.run.disabled = False
            target = (
                parse_time(self.satellites[-1].valid_time) if self.satellites else datetime.now(UTC)
            )
            self.step.value = max(0, min(72, round((target - stamp).total_seconds() / 10800) * 3))
        finally:
            self._setting_controls = False
        self.load_model()

    def load_model(self):
        if self._setting_controls or not self.run.value:
            return
        run, step = self.run.value, self.step.value
        self.model_status.object = (
            f"Chargement IFS +{step} h… Les contours précédents restent visibles."
        )
        selected = tuple(self.model_fields.value)
        self.submit("model", lambda: self.service.model(run, step, selected), self.apply_model)

    def apply_model(self, frame):
        self.model = frame
        self._project_model()
        self._sync_field_visibility()
        self.model_status.object = f"IFS +{frame.step} h · 0,25° · © ECMWF / CC BY 4.0"
        self.refresh_model.disabled = False
        self._context_changed()

    def _project_model(self):
        if self.model is None:
            fields = {}
        else:
            fields = self.model.fields or {"msl": self.model.msl, "gh500": self.model.gh}
        for field_id, source in self.field_data.items():
            data = fields.get(field_id, {"xs": [], "ys": [], "level": []})
            xs, ys = reproject_lines(data["xs"], data["ys"], self.projection)
            source.data = {**data, "xs": xs, "ys": ys}

    def selected_satellite(self):
        if not self.satellites:
            return None
        return self.satellites[min(self.player.value, len(self.satellites) - 1)]

    def _sync_field_visibility(self):
        selected = set(self.model_fields.value)
        for field_id, renderers in self.field_renderers.items():
            for renderer in renderers:
                renderer.visible = field_id in selected

    def _context_changed(self):
        satellite = self.selected_satellite()
        parts = []
        if satellite:
            age = (datetime.now(UTC) - parse_time(satellite.valid_time)).total_seconds() / 3600
            parts.append(
                f"**{satellite.product_title} : {satellite.valid_time}**"
                + (" · ⚠ image de plus de 2 h" if age > 2 else "")
            )
        if self.model:
            meta = self.model.metadata()
            parts.append(
                f"**IFS valide : {meta['valid_time']}** · "
                f"run {self.model.run} (+{self.model.step} h)"
            )
            if satellite:
                delta = (
                    parse_time(meta["valid_time"]) - parse_time(satellite.valid_time)
                ).total_seconds() / 3600
                parts.append(f"Décalage IFS − satellite : **{delta:+.2f} h**")
        self.time_status.object = "  \n".join(parts) or "Chargement des sources en arrière-plan…"
        self.changed()

    def metadata(self):
        result = []
        satellite = self.selected_satellite()
        if satellite:
            result.append(
                {
                    **satellite.metadata(),
                    "visible": self.sat_enabled.value,
                    "opacity": self.opacity.value,
                }
            )
        if self.model:
            result.append(
                {
                    **self.model.metadata(),
                    "visible_parameters": {
                        field_id: field_id in self.model_fields.value for field_id in MODEL_FIELDS
                    },
                }
            )
        return result

    def artifacts(self):
        result = {}
        satellite = self.selected_satellite()
        if satellite:
            result["live/satellite.png"] = satellite.png
        if self.model:
            result.update({f"live/ifs-{p}.grib2": b for p, b in self.model.gribs.items()})
            result["live/ifs-contours.json"] = json_bytes(
                {
                    "crs": "EPSG:3857",
                    "fields": self.model.fields or {"msl": self.model.msl, "gh500": self.model.gh},
                }
            )
        return result
