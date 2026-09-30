from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import folium
import streamlit as st
from folium.plugins import Draw
from streamlit_folium import st_folium


st.set_page_config(
    page_title="Weather Desk",
    page_icon="🌦️",
    layout="wide",
    initial_sidebar_state="expanded",
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def image_input(label: str, key: str) -> Any:
    upload = st.file_uploader(f"{label} — fichier", type=["png", "jpg", "jpeg", "webp"], key=f"{key}_upload")
    url = st.text_input(f"{label} — URL optionnelle", key=f"{key}_url", placeholder="https://…")
    if upload is not None:
        return upload
    return url or None


def make_map(lat: float, lon: float) -> folium.Map:
    fmap = folium.Map(location=[lat, lon], zoom_start=5, control_scale=True, tiles="CartoDB positron")
    Draw(
        export=False,
        position="topleft",
        draw_options={
            "polyline": True,
            "polygon": True,
            "rectangle": True,
            "circle": False,
            "marker": True,
            "circlemarker": False,
        },
        edit_options={"edit": True, "remove": True},
    ).add_to(fmap)
    return fmap


def render_bulletin(
    *,
    zone: str,
    valid_time: str,
    phenomenon: str,
    headline: str,
    analysis: str,
    impacts: str,
    confidence: str,
    limitations: str,
    drawings: list[dict[str, Any]],
) -> str:
    created = now_utc().isoformat().replace("+00:00", "Z")
    metadata = {
        "created_at_utc": created,
        "zone": zone,
        "valid_time": valid_time,
        "phenomenon": phenomenon,
        "confidence": confidence,
        "drawings": drawings,
    }
    return f"""---
type: bulletin-meteo
created_at_utc: {created}
zone: {json.dumps(zone, ensure_ascii=False)}
valid_time: {json.dumps(valid_time, ensure_ascii=False)}
phenomenon: {json.dumps(phenomenon, ensure_ascii=False)}
confidence: {confidence}
review: pending
---

# Bulletin météo — {zone}

**Échéance :** {valid_time}  
**Phénomène :** {phenomenon}  
**Confiance :** {confidence}

## Situation

{headline or "À compléter."}

## Analyse

{analysis or "À compléter."}

## Impacts / décisions

{impacts or "À compléter."}

## Limites et incertitudes

{limitations or "À compléter."}

## Provenance et annotations

```json
{json.dumps(metadata, ensure_ascii=False, indent=2)}
```

_Brouillon généré par Weather Desk — validation humaine requise avant diffusion._
"""


st.title("🌦️ Weather Desk")
st.caption("Analyse satellite + modèles + annotations + bulletin Markdown")

with st.sidebar:
    st.header("Contexte")
    zone = st.text_input("Zone", value="France / façade Atlantique")
    valid_time = st.text_input("Échéance / heure d'analyse", value=now_utc().strftime("%Y-%m-%d %H:%M UTC"))
    phenomenon = st.selectbox(
        "Phénomène principal",
        ["situation générale", "front froid", "front chaud", "occlusion", "ligne sèche", "convection", "brouillard", "vent", "pluie"],
    )
    confidence = st.select_slider("Confiance", options=["faible", "moyenne", "forte"], value="moyenne")
    lat = st.number_input("Latitude de la carte", value=46.5, min_value=-90.0, max_value=90.0)
    lon = st.number_input("Longitude de la carte", value=2.5, min_value=-180.0, max_value=180.0)

st.subheader("Sources")
left, right = st.columns(2)
with left:
    st.markdown("#### Satellite — vapeur d'eau / IR")
    satellite = image_input("Image satellite", "satellite")
    if satellite:
        st.image(satellite, use_container_width=True, caption="Source satellite — à documenter dans le bulletin")
    else:
        st.info("Charge une image locale ou colle une URL EUMETView/WMS.")

with right:
    st.markdown("#### Modèle — IFS / AROME")
    model = image_input("Analyse modèle", "model")
    if model:
        st.image(model, use_container_width=True, caption="Analyse modèle — à documenter dans le bulletin")
    else:
        st.info("Charge une analyse modèle ou colle une URL d'image.")

st.subheader("Analyse et tracé")
map_result = st_folium(make_map(lat, lon), height=520, width=None, key="weather-map")
drawings = map_result.get("all_drawings", []) if map_result else []
st.caption(f"Annotations actives : {len(drawings)}")

st.subheader("Bulletin")
headline = st.text_area("Titre / message principal", placeholder="Ex. Un front froid actif aborde la façade Atlantique…")
analysis = st.text_area("Analyse", height=150, placeholder="Décrire la structure, la dynamique et les éléments convergents.")
impacts = st.text_area("Impacts et décisions", height=120, placeholder="Qui est concerné ? Quelle action ou surveillance ?")
limitations = st.text_area("Limites / incertitudes", height=100, placeholder="Divergence modèles, timing, données manquantes…")

bulletin = render_bulletin(
    zone=zone,
    valid_time=valid_time,
    phenomenon=phenomenon,
    headline=headline,
    analysis=analysis,
    impacts=impacts,
    confidence=confidence,
    limitations=limitations,
    drawings=drawings,
)

with st.expander("Aperçu Markdown"):
    st.markdown(bulletin)

filename = f"bulletin-{now_utc().strftime('%Y%m%d-%H%M')}.md"
st.download_button(
    "Télécharger le bulletin Markdown",
    data=bulletin,
    file_name=filename,
    mime="text/markdown",
    type="primary",
)

