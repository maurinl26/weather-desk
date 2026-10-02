"""Georeferenced public data, bounded disk cache and deterministic IFS contours.

All remote endpoints are fixed here. UI events never do network or GRIB work.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from xml.etree import ElementTree as ET

import contourpy
import numpy as np
import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from weather_desk.analysis import EARTH_RADIUS, checksum, json_bytes, to_mercator

LOG = logging.getLogger(__name__)
WMS = "https://view.eumetsat.int/geoserver/wms"
SATELLITE_LAYER = "msg_fes:wv062"
# Fixed working domain, Atlantic / Europe. Raster and contours use this exact footprint.
DOMAIN = (-35.0, 25.0, 45.0, 70.0)  # west, south, east, north (degrees)
BBOX = (*to_mercator(*DOMAIN[:2]), *to_mercator(*DOMAIN[2:]))
IMAGE_SIZE = (900, 840)


@dataclass(frozen=True)
class FieldSpec:
    field_id: str
    title: str
    parameters: tuple[str, ...]
    level_type: str
    level: int | None
    units: str
    interval: float
    scale: float = 1.0
    offset: float = 0.0
    color: str = "white"
    dash: str = "solid"


MODEL_FIELDS = {
    "msl": FieldSpec("msl", "Pression au niveau de la mer", ("msl",), "sfc", None, "hPa", 4),
    "gh500": FieldSpec(
        "gh500",
        "Géopotentiel 500 hPa",
        ("gh",),
        "pl",
        500,
        "dam",
        6,
        scale=0.1,
        color="#ffd166",
        dash="dashed",
    ),
    "t2m": FieldSpec(
        "t2m", "Température à 2 m", ("2t",), "sfc", None, "°C", 2, offset=-273.15, color="#ff6b6b"
    ),
    "tp": FieldSpec(
        "tp",
        "Précipitations cumulées",
        ("tp",),
        "sfc",
        None,
        "mm",
        2,
        scale=1000,
        color="#4ecdc4",
        dash="dotted",
    ),
    "wind10m": FieldSpec(
        "wind10m", "Vent à 10 m", ("10u", "10v"), "sfc", None, "m/s", 2, color="#a29bfe"
    ),
}


def iso(stamp: datetime) -> str:
    return stamp.replace(tzinfo=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class BoundedSession(requests.Session):
    def __init__(self, timeout=(8, 25)):
        super().__init__()
        self.network_timeout = timeout
        self.mount(
            "https://",
            HTTPAdapter(
                max_retries=Retry(
                    total=1,
                    backoff_factor=0.3,
                    status_forcelist=[429, 500, 502, 503, 504],
                    allowed_methods=["GET", "HEAD"],
                    respect_retry_after_header=False,
                )
            ),
        )

    def request(self, method, url, **kwargs):
        # multiurl passes its own timeout; cap it as well as calls made by latest().
        kwargs["timeout"] = self.network_timeout
        return super().request(method, url, **kwargs)


@lru_cache(maxsize=256)
def _key_lock(key: str):
    return threading.Lock()


class DiskCache:
    def __init__(self, root: Path, max_bytes=512 * 1024 * 1024):
        self.root = Path(root)
        self.max_bytes = max_bytes

    def fetch(self, key: str, loader, ttl: float | None = None) -> bytes:
        digest = checksum(key.encode())
        with _key_lock(str(self.root / digest)):
            self.root.mkdir(parents=True, exist_ok=True)
            data_path, meta_path = self.root / f"{digest}.bin", self.root / f"{digest}.json"
            try:
                meta = json.loads(meta_path.read_bytes())
                data = data_path.read_bytes()
                if (ttl is None or time.time() - meta["created"] < ttl) and checksum(data) == meta[
                    "sha256"
                ]:
                    LOG.info("cache_hit key=%s bytes=%d", key, len(data))
                    return data
            except (OSError, ValueError, KeyError):
                pass
            started = time.monotonic()
            data = loader()
            if not data or len(data) > 32 * 1024 * 1024:
                raise ValueError("Artefact vide ou trop volumineux.")
            suffix = uuid4().hex
            temp = self.root / f"{digest}.{suffix}.tmp"
            try:
                temp.write_bytes(data)
                os.replace(temp, data_path)
                temp.write_bytes(json_bytes({"created": time.time(), "sha256": checksum(data)}))
                os.replace(temp, meta_path)
            finally:
                temp.unlink(missing_ok=True)
            self.prune()
            LOG.info(
                "cache_fill key=%s bytes=%d duration=%.2fs",
                key,
                len(data),
                time.monotonic() - started,
            )
            return data

    def prune(self):
        # Ignore concurrent removals: another session may be pruning the same shared cache.
        with _key_lock(str(self.root / "prune")):
            files = []
            for path in self.root.glob("*.bin"):
                try:
                    stat = path.stat()
                    files.append((stat.st_mtime, stat.st_size, path))
                except FileNotFoundError:
                    pass
            total = sum(size for _, size, _ in files)
            for modified, size, path in sorted(files):
                if total <= self.max_bytes and time.time() - modified < 72 * 3600:
                    break
                path.unlink(missing_ok=True)
                path.with_suffix(".json").unlink(missing_ok=True)
                total -= size


def http_bytes(url: str, params: dict) -> bytes:
    with (
        BoundedSession(timeout=(5, 10)) as session,
        session.get(url, params=params, stream=True) as response,
    ):
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_content(64 * 1024):
            size += len(chunk)
            if size > 8 * 1024 * 1024:
                raise ValueError("Réponse fournisseur trop volumineuse.")
            chunks.append(chunk)
        return b"".join(chunks)


def _tag(element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child_text(element, name: str) -> str | None:
    return next(
        ((child.text or "").strip() for child in element if _tag(child) == name),
        None,
    )


def _duration(value: str) -> timedelta:
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value)
    if not match or not any(match.groups()):
        raise ValueError(f"Durée WMS non prise en charge : {value}")
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _time_dimension(layer):
    return next(
        (
            child
            for child in layer
            if _tag(child) in {"Dimension", "Extent"} and child.get("name", "").lower() == "time"
        ),
        None,
    )


def _times_from_dimension(dimension, count=6) -> list[str]:
    if dimension is None or not (dimension.text or "").strip():
        return []
    available = dimension.text.strip()
    if "/" not in available:
        values = sorted({iso(parse_time(value.strip())) for value in available.split(",")})
        return values[-count:]
    if available.count("/") != 2:
        raise ValueError("Dimension temporelle WMS invalide.")
    start, end, period = available.split("/")
    begin, last, cadence = parse_time(start), parse_time(end), _duration(period)
    if cadence.total_seconds() <= 0:
        raise ValueError("La cadence temporelle WMS doit être positive.")
    n = min(count - 1, int((last - begin) // cadence))
    return [iso(last - cadence * index) for index in reversed(range(n + 1))]


def _times_for_layer(layer, count=6) -> list[str]:
    return _times_from_dimension(_time_dimension(layer), count)


@dataclass(frozen=True)
class SatelliteProduct:
    product_id: str
    title: str
    abstract: str
    crs: tuple[str, ...]
    styles: tuple[str, ...]
    times: tuple[str, ...]


def parse_satellite_catalogue(xml: bytes, count=6) -> list[SatelliteProduct]:
    """Read image layers and inherited CRS from WMS GetCapabilities."""
    root = ET.fromstring(xml)
    capability = next((node for node in root.iter() if _tag(node) == "Capability"), None)
    if capability is None:
        raise ValueError("Document GetCapabilities WMS invalide.")
    products = []

    def visit(layer, inherited_crs, inherited_time):
        own_crs = {
            value
            for child in layer
            if _tag(child) in {"SRS", "CRS"}
            for value in (child.text or "").split()
        }
        crs = inherited_crs | own_crs
        time_dimension = _time_dimension(layer)
        if time_dimension is None:
            time_dimension = inherited_time
        product_id = _child_text(layer, "Name")
        if product_id:
            try:
                times = _times_from_dimension(time_dimension, count)
            except (ValueError, OverflowError):
                LOG.warning("eumetsat_layer_time_unsupported layer=%s", product_id)
                times = []
            if times and any(value.upper() in {"EPSG:3857", "EPSG:900913"} for value in crs):
                styles = tuple(
                    _child_text(child, "Name")
                    for child in layer
                    if _tag(child) == "Style" and _child_text(child, "Name")
                )
                products.append(
                    SatelliteProduct(
                        product_id,
                        _child_text(layer, "Title") or product_id,
                        _child_text(layer, "Abstract") or "",
                        tuple(sorted(crs)),
                        styles,
                        tuple(times),
                    )
                )
        for child in layer:
            if _tag(child) == "Layer":
                visit(child, crs, time_dimension)

    for child in capability:
        if _tag(child) == "Layer":
            visit(child, set(), None)
    return products


def satellite_times(xml: bytes, count=6, product_id=SATELLITE_LAYER) -> list[str]:
    root = ET.fromstring(xml)
    for layer in (node for node in root.iter() if _tag(node) == "Layer"):
        if _child_text(layer, "Name") == product_id:
            times = _times_for_layer(layer, count)
            if times:
                return times
            raise ValueError(f"Aucune échéance disponible pour {product_id}.")
    raise ValueError(f"Produit satellite {product_id} absent du catalogue EUMETSAT.")


def png_rgba(content: bytes, size: tuple[int, int] = IMAGE_SIZE) -> np.ndarray:
    with Image.open(BytesIO(content)) as image:
        if image.format != "PNG" or image.size != size:
            raise ValueError("Image WMS invalide ou dimensions inattendues.")
        # PNG is north-first, image_rgba is bottom-left. Explicit byte order packs RGBA.
        rgba = np.asarray(image.convert("RGBA"), dtype=np.uint8)
        return (
            np.ascontiguousarray(np.flipud(rgba)).view(np.uint32).reshape(image.height, image.width)
        )


@dataclass(frozen=True)
class SatelliteFrame:
    valid_time: str
    png: bytes
    rgba: np.ndarray
    product_id: str = SATELLITE_LAYER
    product_title: str = "SEVIRI WV6.2 µm"

    def metadata(self):
        return {
            "frame_id": f"{self.product_id}/{self.valid_time}",
            "source": f"EUMETSAT EUMETView — {self.product_title}",
            "product": self.product_id,
            "run": None,
            "valid_time": self.valid_time,
            "metadata_status": "provider_catalogue",
            "kind": "wms",
            "crs": "EPSG:3857",
            "bbox": BBOX,
            "bbox_order": "west,south,east,north",
            "georeferenced": True,
            "sha256": checksum(self.png),
            "uri": "live/satellite.png",
            "provider": WMS,
            "attribution": "EUMETSAT",
            "units": "rendered imagery (not calibrated measurements)",
        }


def ecmwf_client():
    from ecmwf.opendata import Client

    client = Client(
        source="ecmwf",
        model="ifs",
        resol="0p25",
        maximum_retries=1,
        retry_after=1,
        use_server_retry_after=False,
    )
    client.session = BoundedSession()
    return client


def decode_field(content: bytes, parameter: str, run: str, step: int):
    import eccodes as ec

    handle = ec.codes_new_from_message(content)
    try:
        if ec.codes_get(handle, "shortName") != parameter:
            raise ValueError("Paramètre GRIB inattendu.")
        if parameter == "gh" and ec.codes_get(handle, "level") != 500:
            raise ValueError("Niveau GRIB inattendu.")
        ref = datetime.strptime(
            f"{ec.codes_get(handle, 'dataDate')}{ec.codes_get(handle, 'dataTime'):04d}",
            "%Y%m%d%H%M",
        ).replace(tzinfo=UTC)
        valid = datetime.strptime(
            f"{ec.codes_get(handle, 'validityDate')}{ec.codes_get(handle, 'validityTime'):04d}",
            "%Y%m%d%H%M",
        ).replace(tzinfo=UTC)
        if iso(ref) != run or valid != ref + timedelta(hours=step):
            raise ValueError("Run ou échéance GRIB incohérents avec la demande.")
        units = ec.codes_get(handle, "units")
        expected_units = {
            "msl": {"Pa"},
            "gh": {"gpm"},
            "2t": {"K"},
            "tp": {"m"},
            "10u": {"m s**-1", "m s-1"},
            "10v": {"m s**-1", "m s-1"},
        }
        if parameter not in expected_units or units not in expected_units[parameter]:
            raise ValueError(f"Unité GRIB inattendue : {units}")
        ni, nj = ec.codes_get(handle, "Ni"), ec.codes_get(handle, "Nj")
        values = ec.codes_get_values(handle).reshape(nj, ni)
        lat = ec.codes_get_array(handle, "latitudes").reshape(nj, ni)[:, 0]
        lon = ec.codes_get_array(handle, "longitudes").reshape(nj, ni)[0, :]
        lon = (lon + 180) % 360 - 180
        ix, iy = np.argsort(lon), np.argsort(lat)
        lon, lat, values = lon[ix], lat[iy], values[np.ix_(iy, ix)]
        west, south, east, north = DOMAIN
        ix, iy = (
            np.where((lon >= west) & (lon <= east))[0],
            np.where((lat >= south) & (lat <= north))[0],
        )
        values = values[np.ix_(iy, ix)]
        if parameter == "msl":
            values = values / 100
        elif parameter == "gh":
            values = values / 10
        elif parameter == "2t":
            values = values - 273.15
        elif parameter == "tp":
            values = values * 1000
        if not np.isfinite(values).all() or not values.size:
            raise ValueError("Champ GRIB incomplet.")
        return lon[ix], lat[iy], values
    finally:
        ec.codes_release(handle)


def contours(lon, lat, values, interval: float) -> dict:
    x = EARTH_RADIUS * np.deg2rad(lon)
    y = EARTH_RADIUS * np.log(np.tan(np.pi / 4 + np.deg2rad(lat) / 2))
    generator = contourpy.contour_generator(x=x, y=y, z=values, name="serial")
    data = {"xs": [], "ys": [], "level": []}
    first = math.ceil(float(np.min(values)) / interval) * interval
    for level in np.arange(first, float(np.max(values)) + 0.001, interval):
        for line in generator.lines(level):
            # Coordinates remain projected on the map; no screen-sized figure to recompute on zoom.
            data["xs"].append(line[:, 0].tolist())
            data["ys"].append(line[:, 1].tolist())
            data["level"].append(float(level))
    return data


@dataclass(frozen=True)
class ModelFrame:
    run: str
    step: int
    msl: dict
    gh: dict
    gribs: dict[str, bytes]
    fields: dict[str, dict] | None = None

    def metadata(self):
        field_metadata = {
            key: {"title": MODEL_FIELDS[key].title, "units": MODEL_FIELDS[key].units}
            for key in (self.fields or {})
        }
        return {
            "frame_id": f"ifs/{self.run}/{self.step}",
            "source": "ECMWF IFS Open Data 0.25°",
            "run": self.run,
            "step_hours": self.step,
            "valid_time": iso(parse_time(self.run) + timedelta(hours=self.step)),
            "metadata_status": "grib_verified",
            "georeferenced": True,
            "crs": "EPSG:3857",
            "bbox": BBOX,
            "kind": "grib_contours",
            "parameters": field_metadata,
            "artifacts": {
                f"live/ifs-{p}.grib2": {"sha256": checksum(b)} for p, b in self.gribs.items()
            },
            "attribution": "ECMWF Open Data — CC BY 4.0",
            "provider": "https://data.ecmwf.int/forecasts/",
        }


class WeatherData:
    def __init__(self, cache: DiskCache):
        self.cache = cache

    def satellite_catalogue(self) -> list[SatelliteProduct]:
        xml = self.cache.fetch(
            "eumetview-capabilities-v1",
            lambda: http_bytes(
                WMS,
                {
                    "service": "WMS",
                    "version": "1.3.0",
                    "request": "GetCapabilities",
                },
            ),
            ttl=120,
        )
        return parse_satellite_catalogue(xml)

    def satellite(
        self,
        valid_time: str,
        product_id: str = SATELLITE_LAYER,
        product_title: str | None = None,
        size: tuple[int, int] = IMAGE_SIZE,
    ) -> SatelliteFrame:
        def acquire():
            data = http_bytes(
                WMS,
                {
                    "service": "WMS",
                    "version": "1.1.1",
                    "request": "GetMap",
                    "layers": product_id,
                    "styles": "",
                    "format": "image/png",
                    "transparent": "true",
                    "srs": "EPSG:3857",
                    "bbox": ",".join(map(str, BBOX)),
                    "width": size[0],
                    "height": size[1],
                    "time": valid_time,
                },
            )
            with Image.open(BytesIO(data)) as image:
                if image.format != "PNG" or image.size != size:
                    raise ValueError("Image WMS invalide ou dimensions inattendues.")
            return data

        key = f"eumetsat-wms-v2/{product_id}/{valid_time}/{BBOX}/{size}"
        png = self.cache.fetch(key, acquire)
        title = product_title or product_id
        if product_id == SATELLITE_LAYER and title == product_id:
            title = "SEVIRI WV6.2 µm"
        return SatelliteFrame(valid_time, png, png_rgba(png, size), product_id, title)

    def latest_run(self) -> str:
        def acquire():
            client = ecmwf_client()
            try:
                return iso(client.latest(type="fc", stream="oper", step=0)).encode()
            finally:
                client.session.close()

        return self.cache.fetch("ifs-latest-v1", acquire, ttl=900).decode()

    @lru_cache(maxsize=16)
    def model(
        self, run: str, step: int, selected_fields: tuple[str, ...] = ("msl", "gh500")
    ) -> ModelFrame:
        if step not in range(0, 73, 3):
            raise ValueError("Échéance IFS limitée à 0–72 h par pas de 3 h.")
        if not selected_fields or any(field not in MODEL_FIELDS for field in selected_fields):
            raise ValueError("Champ IFS inconnu ou aucune sélection.")
        selected_fields = tuple(dict.fromkeys(selected_fields))
        stamp = parse_time(run)
        parameters = tuple(
            dict.fromkeys(
                parameter
                for field_id in selected_fields
                for parameter in MODEL_FIELDS[field_id].parameters
            )
        )
        gribs, decoded = {}, {}
        for parameter in parameters:

            def acquire(parameter=parameter):
                client = ecmwf_client()
                try:
                    with TemporaryDirectory(prefix="weather-desk-ifs-") as folder:
                        target = Path(folder) / "field.grib2"
                        request = dict(
                            date=stamp.strftime("%Y%m%d"),
                            time=stamp.hour,
                            type="fc",
                            stream="oper",
                            step=step,
                            param=parameter,
                            levtype="pl" if parameter == "gh" else "sfc",
                        )
                        if parameter == "gh":
                            request["levelist"] = 500
                        client.retrieve(**request, target=str(target))
                        data = target.read_bytes()
                        decode_field(data, parameter, run, step)
                        return data
                finally:
                    client.session.close()

            content = self.cache.fetch(f"ifs-v2/{run}/{step}/{parameter}", acquire)
            gribs[parameter] = content
            lon, lat, values = decode_field(content, parameter, run, step)
            decoded[parameter] = (lon, lat, values)

        fields = {}
        for field_id in selected_fields:
            spec = MODEL_FIELDS[field_id]
            if field_id == "wind10m":
                east, north = (decoded[parameter][2] for parameter in spec.parameters)
                values = np.hypot(east, north)
                lon, lat = decoded[spec.parameters[0]][:2]
            else:
                lon, lat, values = decoded[spec.parameters[0]]
            fields[field_id] = contours(lon, lat, values, spec.interval)
        return ModelFrame(
            run,
            step,
            fields.get("msl", {"xs": [], "ys": [], "level": []}),
            fields.get("gh500", {"xs": [], "ys": [], "level": []}),
            gribs,
            fields,
        )


DATA = WeatherData(DiskCache(Path(__file__).resolve().parents[1] / "data" / "cache"))
