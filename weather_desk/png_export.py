"""Browser-backed PNG export for Bokeh maps, including WebGL and remote tiles."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import BoundedSemaphore

from bokeh.document import Document
from bokeh.embed import file_html
from bokeh.models import Plot
from bokeh.resources import INLINE
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

_RENDERERS = ThreadPoolExecutor(max_workers=1, thread_name_prefix="weather-desk-png")
_RENDER_LIMIT = BoundedSemaphore(1)
FORMATS = {"square": (1080, 1080), "portrait": (1080, 1350)}
LOG = logging.getLogger(__name__)


def format_png_validity(manifest: dict) -> str:
    """Keep requested, effective source, and emission times visible on the image."""
    lines = [f"Référence demandée : {manifest['analysis']['valid_time']}"]
    for source in manifest["sources"]:
        if source.get("kind") == "wms":
            lines.append(f"Satellite : {source['valid_time']}")
        elif source.get("kind") == "grib_contours":
            lines.append(
                f"IFS : {source['valid_time']} · run {source['run']} +{source['step_hours']} h"
            )
    lines.append(f"Émis : {manifest['exported_at_utc']}")
    return "\n".join(lines)


def _font(size: int):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()


def compose_png(
    map_image: Image.Image,
    *,
    title: str,
    valid_time: str,
    legend: str,
    credits: str,
    format: str,
) -> BytesIO:
    if format not in FORMATS:
        raise ValueError("Format PNG inconnu.")
    width, height = FORMATS[format]
    header_height, footer_height = 184, 86
    available_height = height - header_height - footer_height
    map_image = map_image.convert("RGB")
    scale = min(width / map_image.width, available_height / map_image.height)
    map_image = map_image.resize(
        (round(map_image.width * scale), round(map_image.height * scale)), Image.Resampling.LANCZOS
    )
    canvas = Image.new("RGB", (width, height), "#102b3b")
    draw = ImageDraw.Draw(canvas)
    draw.text((44, 22), title[:100], fill="white", font=_font(30))
    draw.multiline_text(
        (44, 64),
        valid_time[:320],
        fill="#c7dbe5",
        font=_font(14),
        spacing=3,
    )
    draw.text((44, 151), f"Couches : {legend[:120]}", fill="#c7dbe5", font=_font(13))
    x = (width - map_image.width) // 2
    y = header_height + (available_height - map_image.height) // 2
    canvas.paste(map_image, (x, y))
    draw.text((44, height - 56), credits[:160], fill="#c7dbe5", font=_font(15))
    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output


def render_png(
    html_document: str, *, title: str, valid_time: str, legend: str, credits: str, format: str
) -> BytesIO:
    """Render a prepared Bokeh document in a bounded browser worker."""
    if format not in FORMATS:
        raise ValueError("Format PNG inconnu.")
    if not _RENDER_LIMIT.acquire(blocking=False):
        raise RuntimeError("Un export PNG est déjà en cours; réessayer dans un instant.")
    try:
        return _RENDERERS.submit(
            _render_png,
            html_document,
            title=title,
            valid_time=valid_time,
            legend=legend,
            credits=credits,
            format=format,
        ).result()
    finally:
        _RENDER_LIMIT.release()


def prepare_png_document(plot, *, format: str) -> str:
    """Snapshot Bokeh models on the Panel event loop before handing work to a thread."""
    if format not in FORMATS:
        raise ValueError("Format PNG inconnu.")
    width, height = FORMATS[format]
    available_height = height - 144 - 86
    document = plot.document
    if document is None:
        document = Document()
        document.add_root(plot)
    cloned_document = Document.from_json(document.to_json(deferred=False))
    render_root = cloned_document.get_model_by_id(plot.id)
    if render_root is None:
        raise ValueError("La carte ne figure plus dans le document Bokeh.")
    plots = [model for model in render_root.references() if isinstance(model, Plot)]
    if not plots:
        raise ValueError("Aucune carte Bokeh à exporter.")
    columns = getattr(render_root, "ncols", 1) or 1
    rows = (len(plots) + columns - 1) // columns
    for item in plots:
        item.width = width // columns
        item.height = available_height // rows
        item.sizing_mode = "fixed"
    return file_html(render_root, INLINE, title="Weather Desk export")


def _render_png(
    html_document: str, *, title: str, valid_time: str, legend: str, credits: str, format: str
) -> BytesIO:
    width, height = FORMATS[format]
    header_height, footer_height = 144, 86
    available_height = height - header_height - footer_height
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--use-gl=angle",
                "--use-angle=swiftshader",
            ],
        )
        try:
            page = browser.new_page(
                viewport={"width": width, "height": available_height}, device_scale_factor=1
            )
            page.on(
                "console",
                lambda message: (
                    LOG.warning("png_browser_console %s", message.text)
                    if message.type == "error"
                    else None
                ),
            )
            page.on("pageerror", lambda error: LOG.warning("png_browser_error %s", error))
            page.set_content(html_document, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1800)
            if page.locator("canvas").count() == 0:
                raise RuntimeError("Bokeh n'a pas rendu ses canvas dans Chromium.")
            screenshot = page.screenshot(full_page=True, timeout=30000)
        finally:
            browser.close()
    with Image.open(BytesIO(screenshot)) as source:
        map_image = source.convert("RGB")
    return compose_png(
        map_image, title=title, valid_time=valid_time, legend=legend, credits=credits, format=format
    )
