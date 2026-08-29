#!/usr/bin/env python3
"""Build BY-04/JAMX01 TLEs from the latest GNSS mean orbital elements."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_URL = (
    "http://119.45.229.166/get_data?type=system&sat=BY-04"
    "&callbackfunc=data_callback&code=GNS-S1"
)
# The telemetry counter starts at 2017-01-01 00:00:00 China Standard Time.
MISSION_EPOCH = datetime(2016, 12, 31, 16, 0, 0, tzinfo=timezone.utc)
SERVER_TIMEZONE = timezone(timedelta(hours=8))
EARTH_MU = 3.986004418e14
SATELLITES = (("BY04-MEAN", 98247), ("JAMX01-MEAN", 98248))


@dataclass(frozen=True)
class Orbit:
    epoch: datetime
    semi_major_axis_m: float
    eccentricity: float
    inclination_rad: float
    raan_rad: float
    argument_of_perigee_rad: float
    mean_anomaly_rad: float
    received_at: datetime


def parse_jsonp(text: str) -> dict:
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    match = re.fullmatch(r"[A-Za-z_$][\w$]*\s*\((.*)\)\s*;?", text, re.DOTALL)
    if not match:
        raise ValueError("Telemetry response is neither JSON nor valid JSONP")
    return json.loads(match.group(1))


def fetch_payload(url: str, attempts: int = 3, timeout: float = 20.0) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "BY04-ephemeris-builder/1.0"})
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return parse_jsonp(response.read().decode("utf-8"))
        except (OSError, UnicodeError, ValueError, urllib.error.URLError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise RuntimeError(f"Unable to fetch telemetry after {attempts} attempts: {last_error}")


def _entry(table: dict, name: str) -> dict:
    value = table.get(name)
    if not isinstance(value, dict) or "f_double" not in value:
        raise ValueError(f"Missing telemetry field: {name}")
    if value.get("is_suspicious") is True:
        raise ValueError(f"Telemetry field is marked suspicious: {name}")
    return value


def extract_orbit(
    payload: dict,
    now: datetime | None = None,
    max_age_hours: float = 48.0,
    allow_stale: bool = False,
) -> Orbit:
    try:
        table = payload["data"]["BY-04"]
    except (KeyError, TypeError) as error:
        raise ValueError("Response does not contain BY-04 telemetry") from error

    quality = {
        "轨道数据有效标识": 1,
        "滤波器收敛标识": 1,
        "时间连续性标识": 1,
    }
    for name, expected in quality.items():
        actual = int(_entry(table, name)["f_double"])
        if actual != expected:
            raise ValueError(f"Telemetry quality check failed: {name}={actual}, expected {expected}")

    fields = {
        "epoch_seconds": "UTC累计秒整数",
        "semi_major_axis_m": "轨道半长轴",
        "eccentricity": "轨道偏心率",
        "inclination_rad": "轨道倾角",
        "raan_rad": "升交点赤经",
        "argument_of_perigee_rad": "近地点角距",
        "mean_anomaly_rad": "平近点角",
    }
    entries = {key: _entry(table, name) for key, name in fields.items()}
    receive_times = {
        datetime.strptime(item["server_receive_time"], "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=SERVER_TIMEZONE)
        .astimezone(timezone.utc)
        for item in entries.values()
    }
    if len(receive_times) != 1:
        raise ValueError("Required orbital fields do not come from one telemetry packet")
    received_at = receive_times.pop()
    now = now or datetime.now(timezone.utc)
    age = now - received_at
    if age < timedelta(minutes=-10):
        raise ValueError(f"Telemetry receive time is in the future: {received_at.isoformat()}")
    if not allow_stale and age > timedelta(hours=max_age_hours):
        raise ValueError(f"Telemetry is stale ({age.total_seconds() / 3600:.1f} hours old)")

    values = {key: float(item["f_double"]) for key, item in entries.items()}
    orbit = Orbit(
        epoch=MISSION_EPOCH + timedelta(seconds=values.pop("epoch_seconds")),
        received_at=received_at,
        **values,
    )
    if abs((orbit.epoch - received_at).total_seconds()) > 300:
        raise ValueError("Onboard UTC and server receive time differ by more than five minutes")
    if not 6.5e6 <= orbit.semi_major_axis_m <= 8.0e6:
        raise ValueError("Semi-major axis is outside the supported low-Earth-orbit range")
    if not 0.0 <= orbit.eccentricity < 0.1:
        raise ValueError("Eccentricity is outside the supported range")
    return orbit


def tle_checksum(line_without_checksum: str) -> str:
    total = sum(int(char) if char.isdigit() else 1 if char == "-" else 0 for char in line_without_checksum)
    return line_without_checksum + str(total % 10)


def tle_epoch(epoch: datetime) -> str:
    epoch = epoch.astimezone(timezone.utc)
    start = datetime(epoch.year, 1, 1, tzinfo=timezone.utc)
    day = 1.0 + (epoch - start).total_seconds() / 86400.0
    return f"{epoch.year % 100:02d}{day:012.8f}"


def parse_tle_epoch(field: str) -> datetime:
    year = int(field[:2])
    year += 1900 if year >= 57 else 2000
    day = float(field[2:])
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=day - 1.0)


def previous_state(text: str, catalog_number: int) -> tuple[datetime, float, int, int] | None:
    lines = text.splitlines()
    prefix = f"1 {catalog_number:05d}"
    for index, line1 in enumerate(lines):
        if line1.startswith(prefix) and index + 1 < len(lines):
            line2 = lines[index + 1]
            if len(line1) >= 68 and len(line2) >= 68 and line2.startswith(f"2 {catalog_number:05d}"):
                return (
                    parse_tle_epoch(line1[18:32]),
                    float(line2[52:63]),
                    int(line1[64:68]),
                    int(line2[63:68]),
                )
    return None


def build_tle(orbit: Orbit, catalog_number: int, previous: tuple | None) -> tuple[str, str]:
    mean_motion = math.sqrt(EARTH_MU / orbit.semi_major_axis_m**3) * 86400.0 / (2.0 * math.pi)
    element_set = 1
    revolution = 0
    if previous:
        old_epoch, old_mean_motion, old_element_set, old_revolution = previous
        delta_days = (orbit.epoch - old_epoch).total_seconds() / 86400.0
        if abs(delta_days) < 0.5e-8:
            element_set = old_element_set
            revolution = old_revolution
        elif delta_days > 0:
            element_set = old_element_set % 9999 + 1
            revolutions_elapsed = max(0, math.floor(delta_days * (old_mean_motion + mean_motion) / 2.0))
            revolution = (old_revolution + revolutions_elapsed) % 100000

    eccentricity = f"{orbit.eccentricity:.7f}".split(".")[1]
    degrees = lambda value: math.degrees(value) % 360.0
    line1 = (
        f"1 {catalog_number:05d}U {'':8s} {tle_epoch(orbit.epoch)}"
        f"  .00000000  00000-0  00000-0 0 {element_set:4d}"
    )
    line2 = (
        f"2 {catalog_number:05d} {degrees(orbit.inclination_rad):8.4f}"
        f" {degrees(orbit.raan_rad):8.4f} {eccentricity}"
        f" {degrees(orbit.argument_of_perigee_rad):8.4f}"
        f" {degrees(orbit.mean_anomaly_rad):8.4f} {mean_motion:11.8f}{revolution:5d}"
    )
    if len(line1) != 68 or len(line2) != 68:
        raise AssertionError(f"Invalid TLE line width: {len(line1)}, {len(line2)}")
    return tle_checksum(line1), tle_checksum(line2)


def render_files(orbit: Orbit, latest_text: str, history_text: str) -> tuple[str, str]:
    latest_blocks: list[str] = []
    history_additions: list[str] = []
    date_tag = orbit.epoch.strftime("%Y%m%d")
    for name, catalog_number in SATELLITES:
        line1, line2 = build_tle(orbit, catalog_number, previous_state(latest_text, catalog_number))
        latest_blocks.append(f"{name}\n{line1}\n{line2}")
        signature = f"{line1}\n{line2}"
        if signature not in history_text:
            history_name = f"{name.removesuffix('-MEAN')}-{date_tag}-MEAN"
            history_additions.append(f"{history_name}\n{signature}")

    new_latest = "\n\n".join(latest_blocks) + "\n"
    new_history = history_text.rstrip()
    if history_additions:
        new_history += "\n\n" + "\n\n".join(history_additions)
    return new_latest, new_history.rstrip() + "\n"


def atomic_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Read a saved JSON/JSONP response instead of the server")
    parser.add_argument("--telemetry-url", default=os.getenv("TELEMETRY_URL", DEFAULT_URL))
    parser.add_argument("--latest", type=Path, default=Path(__file__).resolve().parents[1] / "latest.tle")
    parser.add_argument("--history", type=Path, default=Path(__file__).resolve().parents[1] / "history.tle")
    parser.add_argument("--max-age-hours", type=float, default=48.0)
    parser.add_argument("--allow-stale", action="store_true", help="Only for replaying saved test data")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    payload = parse_jsonp(args.input.read_text(encoding="utf-8")) if args.input else fetch_payload(args.telemetry_url)
    orbit = extract_orbit(payload, max_age_hours=args.max_age_hours, allow_stale=args.allow_stale)
    latest_text = args.latest.read_text(encoding="utf-8") if args.latest.exists() else ""
    history_text = args.history.read_text(encoding="utf-8") if args.history.exists() else ""
    new_latest, new_history = render_files(orbit, latest_text, history_text)
    if args.dry_run:
        print(new_latest, end="")
    else:
        atomic_write(args.latest, new_latest)
        atomic_write(args.history, new_history)
        print(f"Built ephemeris for {orbit.epoch.isoformat()} from telemetry received at {orbit.received_at.isoformat()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, KeyError, OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
