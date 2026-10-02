from datetime import timedelta

import pytest

from weather_desk.temporal import (
    ifs_run_candidates,
    parse_utc,
    resolve_ifs_time,
    resolve_satellite_time,
)


@pytest.mark.parametrize(
    ("requested", "run", "step", "valid", "offset"),
    [
        (
            "2026-10-02T17:40:00Z",
            "2026-10-02T18:00:00Z",
            0,
            "2026-10-02T18:00:00Z",
            20,
        ),
        (
            "2026-10-02T20:31:00+02:00",
            "2026-10-02T18:00:00Z",
            0,
            "2026-10-02T18:00:00Z",
            -31,
        ),
        (
            "2026-10-02T23:31:00Z",
            "2026-10-03T00:00:00Z",
            0,
            "2026-10-03T00:00:00Z",
            29,
        ),
        (
            "2026-10-02T21:30:00Z",
            "2026-10-02T18:00:00Z",
            3,
            "2026-10-02T21:00:00Z",
            -30,
        ),
    ],
)
def test_ifs_resolution_uses_nearest_three_hour_validity_and_six_hour_cycle(
    requested, run, step, valid, offset
):
    resolved = resolve_ifs_time(requested)

    assert (resolved.run, resolved.step_hours, resolved.valid_time) == (run, step, valid)
    assert resolved.offset_minutes == offset


def test_ifs_resolution_rejects_missing_or_timezone_naive_reference():
    with pytest.raises(ValueError, match="fuseau"):
        resolve_ifs_time("2026-10-02T12:00:00")
    with pytest.raises(ValueError, match="date ISO-8601"):
        resolve_ifs_time("yesterday")


def test_satellite_resolution_handles_different_cadences_and_ties():
    available = [
        "2026-10-02T12:00:00Z",
        "2026-10-02T12:20:00Z",
        "2026-10-02T12:40:00Z",
    ]

    resolved = resolve_satellite_time("2026-10-02T12:30:00Z", available)

    assert resolved.valid_time == "2026-10-02T12:20:00Z"
    assert resolved.offset_minutes == -10


@pytest.mark.parametrize(
    ("requested", "available"),
    [
        ("2026-10-02T12:00:00Z", []),
        ("2026-10-02T14:00:00Z", ["2026-10-02T12:00:00Z"]),
    ],
)
def test_satellite_resolution_refuses_missing_or_too_old_data(requested, available):
    with pytest.raises(ValueError, match="disponible|écart maximal"):
        resolve_satellite_time(requested, available)


def test_utc_parser_normalizes_offsets_and_rejects_naive_values():
    assert parse_utc("2026-10-02T14:00:00+02:00").isoformat() == "2026-10-02T12:00:00+00:00"
    with pytest.raises(ValueError, match="fuseau"):
        parse_utc("2026-10-02T12:00:00")


def test_ifs_policy_has_explicit_maximum_offset():
    with pytest.raises(ValueError, match="90 minutes"):
        resolve_ifs_time("1969-12-31T23:59:00Z", max_offset=timedelta(seconds=30))


def test_ifs_resolution_uses_only_advertised_runs_and_rejects_out_of_horizon():
    runs = ifs_run_candidates("2026-10-02T12:00:00Z")
    resolved = resolve_ifs_time("2026-10-02T17:40:00Z", available_runs=runs)

    assert (resolved.run, resolved.step_hours, resolved.valid_time) == (
        "2026-10-02T12:00:00Z",
        6,
        "2026-10-02T18:00:00Z",
    )
    assert runs == [
        "2026-10-02T12:00:00Z",
        "2026-10-02T06:00:00Z",
        "2026-10-02T00:00:00Z",
        "2026-10-01T18:00:00Z",
    ]
    with pytest.raises(ValueError, match="écart maximal"):
        resolve_ifs_time("2026-10-09T12:00:00Z", available_runs=runs)
