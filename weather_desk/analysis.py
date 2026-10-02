"""Serializable analysis and exports; no UI or network dependencies."""

import hashlib
import json
import math
from datetime import datetime, timezone
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

EARTH_RADIUS = 6_378_137
MERCATOR_LIMIT = math.pi * EARTH_RADIUS


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_mercator(lon: float, lat: float) -> tuple[float, float]:
    lat = max(-85.05112878, min(85.05112878, lat))
    return (
        EARTH_RADIUS * math.radians(lon),
        EARTH_RADIUS * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)),
    )


def to_lonlat(x: float, y: float) -> list[float]:
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("Coordonnées non finies.")
    if abs(x) > MERCATOR_LIMIT or abs(y) > MERCATOR_LIMIT + 0.01:
        raise ValueError("Tracé hors des limites de la carte Mercator.")
    return [
        round(math.degrees(x / EARTH_RADIUS), 6),
        round(math.degrees(2 * math.atan(math.exp(y / EARTH_RADIUS)) - math.pi / 2), 6),
    ]


def drawings_geojson(layers: dict, valid_time: str) -> dict:
    features = []
    for kind, layer in layers.items():
        data = layer["data"]
        for xs, ys in zip(data["xs"], data["ys"], strict=True):
            points = [to_lonlat(x, y) for x, y in zip(xs, ys, strict=True)]
            polygon = layer["geometry"] == "Polygon"
            # Drawing tools send incomplete gestures: export only usable geometries.
            if len(set(map(tuple, points))) < (3 if polygon else 2):
                continue
            if polygon and points[0] != points[-1]:
                points.append(points[0])
            features.append(
                {
                    "type": "Feature",
                    "properties": {"kind": kind, "valid_time": valid_time},
                    "geometry": {
                        "type": layer["geometry"],
                        "coordinates": [points] if polygon else points,
                    },
                }
            )
    return {"type": "FeatureCollection", "features": features}


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def render_bulletin(manifest: dict, annotations: dict) -> str:
    fields = manifest["analysis"]
    frontmatter = "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}"
        for key, value in {
            "type": "bulletin-meteo",
            "analysis_id": manifest["analysis_id"],
            "created_at_utc": manifest["created_at_utc"],
            "exported_at_utc": manifest["exported_at_utc"],
            "zone": fields["zone"],
            "valid_time": fields["valid_time"],
            "confidence": fields["confidence"],
            "review": "pending",
        }.items()
    )
    sections = "\n\n".join(
        f"## {title}\n\n{fields[key] or 'À compléter.'}"
        for key, title in [
            ("headline", "Situation"),
            ("analysis", "Analyse"),
            ("impacts", "Impacts / décisions"),
            ("limitations", "Limites et incertitudes"),
        ]
    )
    provenance = {"sources": manifest["sources"], "annotations": annotations}
    return (
        f"---\n{frontmatter}\n---\n\n# Bulletin météo — {fields['zone']}\n\n"
        f"**Échéance :** {fields['valid_time']}  \n**Confiance :** {fields['confidence']}\n\n"
        f"{sections}\n\n## Provenance et annotations\n\n```json\n"
        f"{json_bytes(provenance).decode().rstrip()}\n```\n\n"
        "_Brouillon — validation humaine requise avant diffusion._\n"
    )


def export_bundle(
    manifest: dict,
    annotations: dict,
    images: dict[str, bytes],
    live_artifacts: dict[str, bytes] | None = None,
) -> BytesIO:
    """Portable archive. Image paths are generated internally, never from upload names."""
    files = {
        "bulletin.md": render_bulletin(manifest, annotations).encode(),
        "annotations.geojson": json_bytes(annotations),
    }
    for key, content in images.items():
        if key not in {"satellite", "model"}:
            raise ValueError("Source d'image inconnue.")
        files[f"images/{key}.png"] = content
    allowed_live = {
        "live/satellite.png",
        "live/ifs-msl.grib2",
        "live/ifs-gh.grib2",
        "live/ifs-2t.grib2",
        "live/ifs-tp.grib2",
        "live/ifs-10u.grib2",
        "live/ifs-10v.grib2",
        "live/ifs-contours.json",
    }
    for name, content in (live_artifacts or {}).items():
        if name not in allowed_live:
            raise ValueError("Artefact météo inconnu.")
        files[name] = content
    archived_manifest = {
        **manifest,
        "files": {name: {"sha256": checksum(data)} for name, data in files.items()},
    }
    files["manifest.json"] = json_bytes(archived_manifest)
    result = BytesIO()
    with ZipFile(result, "w", ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    result.seek(0)
    return result
