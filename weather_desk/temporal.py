"""Explicit UTC resolution policies for source-valid weather data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable


@dataclass(frozen=True)
class ResolvedTime:
    requested: str
    valid_time: str
    offset_minutes: int


@dataclass(frozen=True)
class IFSTime:
    run: str
    step_hours: int
    valid_time: str
    requested_time: str
    offset_minutes: int


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("L'heure de référence doit être une date ISO-8601 avec fuseau.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("L'heure de référence doit être une date ISO-8601 avec fuseau.") from exc
    if parsed.tzinfo is None:
        raise ValueError("L'heure de référence doit inclure son fuseau horaire.")
    return parsed.astimezone(UTC)


def format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def ifs_run_candidates(latest_run: str, count: int = 4) -> list[str]:
    """Return the latest provider run and three preceding six-hour cycles."""
    latest = parse_utc(latest_run)
    if latest.hour % 6 or latest.minute or latest.second or latest.microsecond:
        raise ValueError("Le dernier run IFS doit être aligné sur un cycle de six heures.")
    return [format_utc(latest - timedelta(hours=6 * index)) for index in range(count)]


def resolve_ifs_time(
    value: str,
    max_offset: timedelta = timedelta(minutes=90),
    available_runs: Iterable[str] | None = None,
) -> IFSTime:
    """Choose the nearest 3-hour IFS validity reachable from an advertised run."""
    requested = parse_utc(value)
    if available_runs is None:
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        elapsed = (requested - epoch).total_seconds()
        period = timedelta(hours=3)
        slot = int(elapsed / period.total_seconds() + 0.5)
        nearest = epoch + slot * period
        first_run = nearest.replace(hour=(nearest.hour // 6) * 6, minute=0, second=0, microsecond=0)
        available_runs = [format_utc(first_run - timedelta(hours=6 * index)) for index in range(13)]
    candidates = []
    for run_value in available_runs:
        run = parse_utc(run_value)
        if run.hour % 6 or run.minute or run.second or run.microsecond:
            raise ValueError("Les runs IFS disponibles doivent être alignés sur six heures.")
        for step in range(0, 73, 3):
            valid = run + timedelta(hours=step)
            candidates.append(
                (abs(valid - requested), valid < requested, -run.timestamp(), run, step, valid)
            )
    if not candidates:
        raise ValueError("Aucun run IFS disponible pour l'heure demandée.")
    _, _, _, run, step, valid = min(candidates)
    offset = valid - requested
    if abs(offset) > max_offset:
        raise ValueError("Aucune échéance IFS dans l'écart maximal autorisé de 90 minutes.")
    return IFSTime(
        run=format_utc(run),
        step_hours=step,
        valid_time=format_utc(valid),
        requested_time=format_utc(requested),
        offset_minutes=int(offset.total_seconds() / 60),
    )


def resolve_satellite_time(
    requested: str,
    available: Iterable[str],
    max_offset: timedelta = timedelta(minutes=30),
) -> ResolvedTime:
    """Choose the nearest advertised image; on ties, prefer the earlier frame."""
    target = parse_utc(requested)
    candidates = [parse_utc(value) for value in available]
    if not candidates:
        raise ValueError("Aucune échéance satellite disponible dans le catalogue.")
    selected = min(candidates, key=lambda value: (abs(value - target), value > target))
    offset = selected - target
    if abs(offset) > max_offset:
        raise ValueError("Aucune image satellite dans l'écart maximal autorisé de 30 minutes.")
    return ResolvedTime(
        requested=format_utc(target),
        valid_time=format_utc(selected),
        offset_minutes=int(offset.total_seconds() / 60),
    )
