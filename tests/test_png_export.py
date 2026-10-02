import pytest
from PIL import Image

from weather_desk.png_export import compose_png


@pytest.mark.parametrize("format,size", [("square", (1080, 1080)), ("portrait", (1080, 1350))])
def test_social_png_has_target_dimensions_and_map_content(format, size):
    map_image = Image.new("RGB", (400, 300), "#d54c42")

    output = compose_png(
        map_image,
        title="Analyse Atlantique",
        valid_time="2026-10-02T12:00:00Z",
        legend="MSL (hPa) · GH500 (dam)",
        credits="EUMETSAT · ECMWF · © OpenStreetMap",
        format=format,
    )

    with Image.open(output) as exported:
        assert exported.size == size
        assert exported.getpixel((540, 540)) == (213, 76, 66)


def test_png_export_rejects_unknown_format():
    with pytest.raises(ValueError, match="Format PNG"):
        compose_png(
            Image.new("RGB", (20, 20)),
            title="",
            valid_time="",
            legend="",
            credits="",
            format="wide",
        )
