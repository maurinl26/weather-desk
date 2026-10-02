import json
from concurrent.futures import Future
from io import BytesIO
from zipfile import ZipFile

import numpy as np
import pytest
from PIL import Image

from weather_desk.analysis import checksum, to_mercator
from weather_desk.data import (
    BBOX,
    IMAGE_SIZE,
    DiskCache,
    ModelFrame,
    SatelliteFrame,
    contours,
    decode_field,
    parse_satellite_catalogue,
    png_rgba,
    satellite_times,
)
from weather_desk.ui import WeatherDesk


def satellite_fixture(stamp="2026-09-30T12:00:00Z"):
    out = BytesIO()
    image = Image.new("RGBA", IMAGE_SIZE, (80, 80, 80, 255))
    image.putpixel((0, 0), (255, 0, 0, 255))
    image.putpixel((0, IMAGE_SIZE[1] - 1), (0, 0, 255, 255))
    image.save(out, format="PNG")
    png = out.getvalue()
    return SatelliteFrame(stamp, png, png_rgba(png))


def model_fixture():
    data = {"xs": [[0, 10]], "ys": [[0, 20]], "level": [1000]}
    return ModelFrame("2026-09-30T06:00:00Z", 6, data, data, {"msl": b"grib-msl", "gh": b"grib-gh"})


def test_wms_catalogue_uses_real_end_and_cadence():
    xml = b"""<WMS_Capabilities xmlns="http://www.opengis.net/wms"><Capability><Layer>
    <Layer><Name>msg_fes:wv062</Name><Dimension name="time">
    2020-08-01T00:00:00.000Z/2026-09-30T12:45:00.000Z/PT15M
    </Dimension></Layer></Layer></Capability></WMS_Capabilities>"""
    times = satellite_times(xml)
    assert len(times) == 6
    assert times[0] == "2026-09-30T11:30:00Z"
    assert times[-1] == "2026-09-30T12:45:00Z"
    half_hour = satellite_times(xml.replace(b"PT15M", b"PT30M"))
    assert half_hour[-2:] == ["2026-09-30T12:15:00Z", "2026-09-30T12:45:00Z"]


def test_eumetsat_catalogue_discovers_multiple_timed_layers_and_inherits_crs():
    xml = b"""<WMS_Capabilities xmlns="http://www.opengis.net/wms"><Capability><Layer>
    <Title>root</Title><CRS>EPSG:3857 EPSG:4326</CRS><Dimension name="time">
    2026-10-02T12:00:00Z/2026-10-02T13:00:00Z/PT15M</Dimension>
    <Layer><Name>msg_fes:wv062</Name><Title>Water vapour 6.2</Title><Style>
    <Name>default</Name></Style></Layer>
    <Layer><Name>msg_fes:ir108</Name><Title>Infrared 10.8</Title><Dimension name="time">
    2026-10-02T10:00:00Z,2026-10-02T11:00:00Z</Dimension></Layer>
    </Layer></Capability></WMS_Capabilities>"""
    products = parse_satellite_catalogue(xml)
    assert [item.product_id for item in products] == ["msg_fes:wv062", "msg_fes:ir108"]
    assert products[0].title == "Water vapour 6.2"
    assert products[0].times == (
        "2026-10-02T12:00:00Z",
        "2026-10-02T12:15:00Z",
        "2026-10-02T12:30:00Z",
        "2026-10-02T12:45:00Z",
        "2026-10-02T13:00:00Z",
    )
    assert products[1].times == ("2026-10-02T10:00:00Z", "2026-10-02T11:00:00Z")
    assert "EPSG:3857" in products[0].crs


def test_raster_north_south_and_alpha_are_not_inverted():
    frame = satellite_fixture()
    assert int(frame.rgba[-1, 0]) == 0xFF0000FF  # red at north-west
    assert int(frame.rgba[0, 0]) == 0xFFFF0000  # blue at south-west
    assert frame.metadata()["bbox"] == BBOX
    assert BBOX[:2] == to_mercator(-35, 25)


def test_wms_exception_cannot_be_decoded_as_image():
    with pytest.raises(OSError):
        png_rgba(b"<ServiceExceptionReport>Unavailable</ServiceExceptionReport>")


def test_cache_reuses_valid_data_and_refetches_corruption(tmp_path):
    calls = []

    def loader():
        calls.append(True)
        return b"verified"

    cache = DiskCache(tmp_path)
    assert cache.fetch("test", loader) == b"verified"
    assert cache.fetch("test", loader) == b"verified"
    assert len(calls) == 1
    next(tmp_path.glob("*.bin")).write_bytes(b"broken")
    assert cache.fetch("test", loader) == b"verified"
    assert len(calls) == 2
    cache.fetch("test", loader, ttl=-1)
    assert len(calls) == 3


def test_cache_failed_loader_does_not_publish_empty_or_partial_file(tmp_path):
    def fail():
        raise TimeoutError()

    cache = DiskCache(tmp_path)
    with pytest.raises(TimeoutError):
        cache.fetch("test", fail)
    assert not list(tmp_path.glob("*.bin"))


def test_cache_has_a_size_limit(tmp_path):
    cache = DiskCache(tmp_path, max_bytes=6)
    cache.fetch("one", lambda: b"aaaa")
    cache.fetch("two", lambda: b"bbbb")
    assert sum(p.stat().st_size for p in tmp_path.glob("*.bin")) <= 6


def test_contours_use_mercator_and_correct_level_units():
    lon, lat = np.array([-5.0, 0.0, 5.0]), np.array([40.0, 45.0, 50.0])
    data = contours(lon, lat, np.array([[996.0, 1000.0, 1004.0]] * 3), interval=4)
    index = data["level"].index(1000)
    assert data["xs"][index] == pytest.approx([0, 0, 0])
    assert min(data["ys"][index]) == pytest.approx(to_mercator(0, 40)[1])
    assert max(data["ys"][index]) == pytest.approx(to_mercator(0, 50)[1])


def make_grib(parameter):
    import eccodes as ec

    handle = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        settings = {
            "Ni": 72,
            "Nj": 10,
            "latitudeOfFirstGridPointInDegrees": 70,
            "latitudeOfLastGridPointInDegrees": 25,
            "longitudeOfFirstGridPointInDegrees": 0,
            "longitudeOfLastGridPointInDegrees": 355,
            "iDirectionIncrementInDegrees": 5,
            "jDirectionIncrementInDegrees": 5,
            "dataDate": 20260930,
            "dataTime": 600,
            "forecastTime": 6,
        }
        for key, value in settings.items():
            ec.codes_set(handle, key, value)
        if parameter == "gh":
            ec.codes_set(handle, "typeOfLevel", "isobaricInhPa")
            ec.codes_set(handle, "level", 500)
        ec.codes_set(handle, "shortName", parameter)
        # Global coordinates wrap through Greenwich. Each row has a recognisable value.
        bases = {"msl": 100000, "gh": 5500, "2t": 280, "tp": 0.025, "10u": 4, "10v": 3}
        offset = np.arange(10) * 100 if parameter in {"msl", "gh"} else np.zeros(10)
        ec.codes_set_values(handle, np.repeat(offset + bases[parameter], 72))
        return ec.codes_get_message(handle)
    finally:
        ec.codes_release(handle)


@pytest.mark.parametrize("parameter,expected", [("msl", 1009), ("gh", 640)])
def test_grib_run_units_latitude_and_wrapped_longitude(parameter, expected):
    grib = make_grib(parameter)
    lon, lat, values = decode_field(grib, parameter, "2026-09-30T06:00:00Z", 6)
    assert lon[0] == -35 and lon[-1] == 45
    assert lat[0] == 25 and lat[-1] == 70
    assert values[0, 0] == expected
    with pytest.raises(ValueError, match="échéance"):
        decode_field(grib, parameter, "2026-09-30T06:00:00Z", 3)


@pytest.mark.parametrize(
    "parameter,expected",
    [("2t", 6.85), ("tp", 25), ("10u", 4), ("10v", 3)],
)
def test_additional_ifs_fields_are_converted_to_display_units(parameter, expected):
    lon, lat, values = decode_field(make_grib(parameter), parameter, "2026-09-30T06:00:00Z", 6)
    assert lon[0] == -35 and lat[0] == 25
    assert values[0, 0] == pytest.approx(expected, abs=0.02)


def test_live_frames_keep_drawings_and_view_and_export_actual_displayed_data():
    desk = WeatherDesk()
    original_range = desk.map.x_range.start, desk.map.x_range.end
    desk.layers["cold_front"]["source"].data = {"xs": [[0, 10]], "ys": [[0, 20]]}
    frame = satellite_fixture()
    desk.live.apply_satellite([frame, satellite_fixture("2026-09-30T12:15:00Z")])
    desk.live.apply_model(model_fixture())
    desk.live.player.value = 0
    assert desk.map.x_range.start == original_range[0]
    assert desk.map.x_range.end == original_range[1]
    assert len(desk.snapshot()[1]["features"]) == 1
    with ZipFile(desk.download("zip")) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        live = [s for s in manifest["sources"] if s["georeferenced"]]
        assert live[0]["valid_time"] == frame.valid_time
        assert live[1]["valid_time"] == "2026-09-30T12:00:00Z"
        assert archive.read("live/satellite.png") == frame.png
        assert archive.read("live/ifs-msl.grib2") == b"grib-msl"
        for name, record in manifest["files"].items():
            assert checksum(archive.read(name)) == record["sha256"]


def test_worker_failure_preserves_last_good_model_and_reports_error():
    desk = WeatherDesk()
    frame = model_fixture()
    desk.live.apply_model(frame)
    future = Future()
    future.set_exception(TimeoutError("provider timeout"))
    desk.live.pending["model"] = (future, desk.live.apply_model)
    desk.live.poll()
    assert desk.live.model is frame
    assert "indisponible" in desk.live.model_status.object
    assert not desk.live.pending


def test_new_request_discards_old_result_and_close_cancels(monkeypatch):
    import weather_desk.live as module

    futures = []

    class Executor:
        def submit(self, work):
            future = Future()
            futures.append(future)
            return future

    monkeypatch.setattr(module, "WORKERS", Executor())
    desk = WeatherDesk()
    applied = []
    desk.live.submit("model", lambda: 1, applied.append)
    futures[0].set_running_or_notify_cancel()
    desk.live.submit("model", lambda: 2, applied.append)
    futures[0].set_result("old")
    desk.live.poll()
    assert applied == []
    futures[1].set_result("new")
    desk.live.poll()
    assert applied == ["new"]
    desk.live.submit("model", lambda: 3, applied.append)
    desk.live.close(None)
    assert futures[2].cancelled()


def test_progressive_frames_keep_chronology_and_current_selection():
    desk = WeatherDesk()
    latest = satellite_fixture("2026-09-30T12:30:00Z")
    desk.live.apply_satellite([latest])
    first_buffer = desk.live.sat_data.data["image"][0]
    desk.live._history_frame_loaded(satellite_fixture("2026-09-30T12:00:00Z"))
    desk.live._history_frame_loaded(satellite_fixture("2026-09-30T12:15:00Z"))
    assert desk.live.frame_order.data["index"] == [1, 2, 0]
    assert desk.live.sat_filter.indices == [0]
    assert desk.live.selected_satellite().valid_time == latest.valid_time
    assert desk.live.sat_data.data["image"][0] is first_buffer
    desk.live.player.value = 0
    assert desk.live.selected_satellite().valid_time == "2026-09-30T12:00:00Z"


def test_missing_history_frame_keeps_usable_sequence():
    desk = WeatherDesk()
    desk.live.apply_satellite([satellite_fixture()])
    failed = Future()
    failed.set_exception(TimeoutError())
    desk.live.pending["satellite:2026-09-30T11:45:00Z"] = (failed, desk.live._history_frame_loaded)
    desk.live.poll()
    assert len(desk.live.satellites) == 1
    assert "séquence incomplète" in desk.live.sat_status.object
    assert not desk.live.refresh_sat.disabled
