#!/usr/bin/env python3
"""Fit and publish BY-04/JAMX01 SGP4 TLEs from GNSS telemetry."""

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

from sgp4.api import SGP4_ERRORS, Satrec, WGS72, jday


DEFAULT_URL = (
    "http://119.45.229.166/get_data?type=system&sat=BY-04"
    "&callbackfunc=data_callback&code=GNS-S1"
)
MISSION_EPOCH = datetime(2017, 1, 1, tzinfo=timezone.utc)
SERVER_TIMEZONE = timezone.utc
EARTH_MU = 3.986004418e14
EARTH_ROTATION_RAD_S = 7.2921150e-5
SGP4_EPOCH_JD = 2433281.5
SATELLITES = (("BY04-MEAN", 98247), ("JAMX01-MEAN", 98248))
MAX_POSITION_ERROR_M = 1000.0
MAX_VELOCITY_ERROR_M_S = 1.0


@dataclass(frozen=True)
class Orbit:
    epoch: datetime
    semi_major_axis_m: float
    eccentricity: float
    inclination_rad: float
    raan_rad: float
    argument_of_perigee_rad: float
    mean_anomaly_rad: float
    position_ecef_m: tuple[float, float, float]
    velocity_ecef_m_s: tuple[float, float, float]
    received_at: datetime


@dataclass(frozen=True)
class FittedElements:
    mean_motion_rev_day: float
    eccentricity: float
    inclination_rad: float
    raan_rad: float
    argument_of_perigee_rad: float
    mean_anomaly_rad: float
    position_error_m: float
    velocity_error_m_s: float


def parse_jsonp(text: str) -> dict:
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    match = re.fullmatch(r"[A-Za-z_$][\w$]*\s*\((.*)\)\s*;?", text, re.DOTALL)
    if not match:
        raise ValueError("Telemetry response is neither JSON nor valid JSONP")
    return json.loads(match.group(1))


def fetch_payload(url: str, attempts: int = 3, timeout: float = 20.0) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "BY04-ephemeris-builder/2.0"})
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

    for name, expected in {
        "轨道数据有效标识": 1,
        "滤波器收敛标识": 1,
        "时间连续性标识": 1,
    }.items():
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
        "position_x": "卫星位置X方向",
        "position_y": "卫星位置Y方向",
        "position_z": "卫星位置Z方向",
        "velocity_x": "卫星速度X方向",
        "velocity_y": "卫星速度Y方向",
        "velocity_z": "卫星速度Z方向",
    }
    entries = {key: _entry(table, name) for key, name in fields.items()}
    receive_times = {
        datetime.strptime(item["server_receive_time"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=SERVER_TIMEZONE
        )
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
        epoch=MISSION_EPOCH + timedelta(seconds=values["epoch_seconds"]),
        received_at=received_at,
        semi_major_axis_m=values["semi_major_axis_m"],
        eccentricity=values["eccentricity"],
        inclination_rad=values["inclination_rad"],
        raan_rad=values["raan_rad"],
        argument_of_perigee_rad=values["argument_of_perigee_rad"],
        mean_anomaly_rad=values["mean_anomaly_rad"],
        position_ecef_m=tuple(values[f"position_{axis}"] for axis in "xyz"),
        velocity_ecef_m_s=tuple(values[f"velocity_{axis}"] for axis in "xyz"),
    )
    if abs((orbit.epoch - received_at).total_seconds()) > 300:
        raise ValueError("Onboard UTC and server receive time differ by more than five minutes")
    if not 6.5e6 <= orbit.semi_major_axis_m <= 8.0e6:
        raise ValueError("Semi-major axis is outside the supported low-Earth-orbit range")
    if not 0.0 <= orbit.eccentricity < 0.1:
        raise ValueError("Eccentricity is outside the supported range")
    return orbit


def _norm(vector: tuple[float, ...] | list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _epoch_julian(orbit: Orbit) -> tuple[float, float]:
    return jday(
        orbit.epoch.year,
        orbit.epoch.month,
        orbit.epoch.day,
        orbit.epoch.hour,
        orbit.epoch.minute,
        orbit.epoch.second + orbit.epoch.microsecond / 1e6,
    )


def _gmst(epoch: datetime) -> float:
    jd, fraction = jday(
        epoch.year, epoch.month, epoch.day, epoch.hour, epoch.minute,
        epoch.second + epoch.microsecond / 1e6,
    )
    full_jd = jd + fraction
    centuries = (full_jd - 2451545.0) / 36525.0
    degrees = (
        280.46061837 + 360.98564736629 * (full_jd - 2451545.0)
        + 0.000387933 * centuries**2 - centuries**3 / 38710000.0
    ) % 360.0
    return math.radians(degrees)


def telemetry_teme_state(orbit: Orbit) -> tuple[tuple[float, ...], tuple[float, ...]]:
    x, y, _ = orbit.position_ecef_m
    vx, vy, vz = orbit.velocity_ecef_m_s
    inertial_velocity = (
        vx - EARTH_ROTATION_RAD_S * y,
        vy + EARTH_ROTATION_RAD_S * x,
        vz,
    )
    theta = _gmst(orbit.epoch)
    cosine, sine = math.cos(theta), math.sin(theta)

    def rotate(vector: tuple[float, ...]) -> tuple[float, ...]:
        return (
            (cosine * vector[0] - sine * vector[1]) / 1000.0,
            (sine * vector[0] + cosine * vector[1]) / 1000.0,
            vector[2] / 1000.0,
        )

    return rotate(orbit.position_ecef_m), rotate(inertial_velocity)


def _decode_parameters(parameters: list[float]) -> tuple[float, float, float, float, float, float]:
    mean_motion, eccentricity_sine, eccentricity_cosine, inclination, raan, mean_longitude = parameters
    eccentricity = math.hypot(eccentricity_sine, eccentricity_cosine)
    argument_of_perigee = math.atan2(eccentricity_sine, eccentricity_cosine) % (2.0 * math.pi)
    mean_anomaly = (mean_longitude - raan - argument_of_perigee) % (2.0 * math.pi)
    return mean_motion, eccentricity, inclination, raan % (2.0 * math.pi), argument_of_perigee, mean_anomaly


def _satellite_from_parameters(parameters: list[float], orbit: Orbit, catalog_number: int) -> Satrec:
    mean_motion, eccentricity, inclination, raan, argument_of_perigee, mean_anomaly = _decode_parameters(
        parameters
    )
    jd, fraction = _epoch_julian(orbit)
    satellite = Satrec()
    satellite.sgp4init(
        WGS72, "i", catalog_number, jd + fraction - SGP4_EPOCH_JD,
        0.0, 0.0, 0.0, eccentricity, argument_of_perigee, inclination,
        mean_anomaly, mean_motion * 2.0 * math.pi / 1440.0, raan,
    )
    return satellite


def _residual(parameters: list[float], orbit: Orbit, catalog_number: int) -> list[float]:
    satellite = _satellite_from_parameters(parameters, orbit, catalog_number)
    jd, fraction = _epoch_julian(orbit)
    error, position, velocity = satellite.sgp4(jd, fraction)
    if error:
        raise ValueError(f"SGP4 fitting error {error}: {SGP4_ERRORS.get(error, 'unknown error')}")
    target_position, target_velocity = telemetry_teme_state(orbit)
    return [
        *(position[index] - target_position[index] for index in range(3)),
        *((velocity[index] - target_velocity[index]) * 1000.0 for index in range(3)),
    ]


def _solve_linear(matrix: list[list[float]], right_hand_side: list[float]) -> list[float]:
    augmented = [row[:] + [value] for row, value in zip(matrix, right_hand_side)]
    size = len(right_hand_side)
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("SGP4 fit Jacobian is singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                augmented[row][index] - factor * augmented[column][index]
                for index in range(size + 1)
            ]
    return [augmented[index][-1] for index in range(size)]


def fit_sgp4(orbit: Orbit, catalog_number: int = 98247) -> FittedElements:
    mean_motion = math.sqrt(EARTH_MU / orbit.semi_major_axis_m**3) * 86400.0 / (2.0 * math.pi)
    parameters = [
        mean_motion,
        orbit.eccentricity * math.sin(orbit.argument_of_perigee_rad),
        orbit.eccentricity * math.cos(orbit.argument_of_perigee_rad),
        orbit.inclination_rad,
        orbit.raan_rad,
        orbit.raan_rad + orbit.argument_of_perigee_rad + orbit.mean_anomaly_rad,
    ]
    steps = [1e-5, 1e-6, 1e-6, 1e-5, 1e-5, 1e-5]
    limits = [0.05, 0.01, 0.01, 0.03, 0.03, 0.08]

    for _ in range(15):
        residual = _residual(parameters, orbit, catalog_number)
        if _norm(residual[:3]) < 0.001 and _norm(residual[3:]) < 0.001:
            break
        jacobian = [[0.0] * 6 for _ in range(6)]
        for column, step in enumerate(steps):
            upper, lower = parameters[:], parameters[:]
            upper[column] += step
            lower[column] -= step
            upper_residual = _residual(upper, orbit, catalog_number)
            lower_residual = _residual(lower, orbit, catalog_number)
            for row in range(6):
                jacobian[row][column] = (upper_residual[row] - lower_residual[row]) / (2.0 * step)
        update = _solve_linear(jacobian, [-value for value in residual])
        ratios = [limit / abs(value) for value, limit in zip(update, limits) if value]
        scale = min([1.0, *ratios])
        old_score = _norm(residual)
        accepted = False
        for damping in (scale, scale / 2.0, scale / 4.0, scale / 8.0, scale / 16.0):
            candidate = [value + damping * delta for value, delta in zip(parameters, update)]
            if _norm(_residual(candidate, orbit, catalog_number)) < old_score:
                parameters = candidate
                accepted = True
                break
        if not accepted:
            raise ValueError("SGP4 fit failed to reduce the telemetry residual")

    residual = _residual(parameters, orbit, catalog_number)
    position_error_m = _norm(residual[:3]) * 1000.0
    velocity_error_m_s = _norm(residual[3:])
    if position_error_m > 10.0 or velocity_error_m_s > 0.05:
        raise ValueError(
            f"SGP4 fit did not converge: {position_error_m:.3f} m, {velocity_error_m_s:.4f} m/s"
        )
    mean_motion, eccentricity, inclination, raan, argument_of_perigee, mean_anomaly = _decode_parameters(
        parameters
    )
    return FittedElements(
        mean_motion, eccentricity, inclination, raan, argument_of_perigee,
        mean_anomaly, position_error_m, velocity_error_m_s,
    )


def tle_checksum(line_without_checksum: str) -> str:
    total = sum(int(char) if char.isdigit() else 1 if char == "-" else 0 for char in line_without_checksum)
    return line_without_checksum + str(total % 10)


def tle_epoch(epoch: datetime) -> str:
    epoch = epoch.astimezone(timezone.utc)
    start = datetime(epoch.year, 1, 1, tzinfo=timezone.utc)
    day = 1.0 + (epoch - start).total_seconds() / 86400.0
    return f"{epoch.year % 100:02d}{day:012.8f}"


def parse_tle_epoch(field: str) -> datetime:
    short_year = int(field[:2])
    year = short_year + (1900 if short_year >= 57 else 2000)
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=float(field[2:]) - 1.0)


def previous_state(text: str, catalog_number: int) -> tuple[datetime, float, int, int] | None:
    lines = text.splitlines()
    for index, line1 in enumerate(lines):
        if line1.startswith(f"1 {catalog_number:05d}") and index + 1 < len(lines):
            line2 = lines[index + 1]
            if len(line1) >= 68 and len(line2) >= 68 and line2.startswith(f"2 {catalog_number:05d}"):
                return parse_tle_epoch(line1[18:32]), float(line2[52:63]), int(line1[64:68]), int(line2[63:68])
    return None


def build_tle(
    orbit: Orbit, fitted: FittedElements, catalog_number: int, previous: tuple | None,
) -> tuple[str, str]:
    element_set, revolution = 1, 0
    if previous:
        old_epoch, old_mean_motion, old_element_set, old_revolution = previous
        delta_days = (orbit.epoch - old_epoch).total_seconds() / 86400.0
        if abs(delta_days) < 0.5e-8:
            element_set, revolution = old_element_set, old_revolution
        elif delta_days > 0:
            element_set = old_element_set % 9999 + 1
            revolution = (
                old_revolution
                + max(0, math.floor(delta_days * (old_mean_motion + fitted.mean_motion_rev_day) / 2.0))
            ) % 100000
    eccentricity = f"{fitted.eccentricity:.7f}".split(".")[1]
    degrees = lambda value: math.degrees(value) % 360.0
    line1 = (
        f"1 {catalog_number:05d}U {'':8s} {tle_epoch(orbit.epoch)}"
        f"  .00000000  00000-0  00000-0 0 {element_set:4d}"
    )
    line2 = (
        f"2 {catalog_number:05d} {degrees(fitted.inclination_rad):8.4f}"
        f" {degrees(fitted.raan_rad):8.4f} {eccentricity}"
        f" {degrees(fitted.argument_of_perigee_rad):8.4f}"
        f" {degrees(fitted.mean_anomaly_rad):8.4f} {fitted.mean_motion_rev_day:11.8f}{revolution:5d}"
    )
    if len(line1) != 68 or len(line2) != 68:
        raise AssertionError(f"Invalid TLE line width: {len(line1)}, {len(line2)}")
    return tle_checksum(line1), tle_checksum(line2)


def validate_tle(line1: str, line2: str, orbit: Orbit) -> tuple[float, float]:
    satellite = Satrec.twoline2rv(line1, line2, WGS72)
    jd, fraction = _epoch_julian(orbit)
    error, position, velocity = satellite.sgp4(jd, fraction)
    if error:
        raise ValueError(f"Generated TLE propagation error {error}: {SGP4_ERRORS.get(error, 'unknown error')}")
    target_position, target_velocity = telemetry_teme_state(orbit)
    position_error_m = _norm(tuple(position[i] - target_position[i] for i in range(3))) * 1000.0
    velocity_error_m_s = _norm(tuple(velocity[i] - target_velocity[i] for i in range(3))) * 1000.0
    if position_error_m > MAX_POSITION_ERROR_M or velocity_error_m_s > MAX_VELOCITY_ERROR_M_S:
        raise ValueError(
            f"Generated TLE residual exceeds limit: {position_error_m:.3f} m, {velocity_error_m_s:.4f} m/s"
        )
    return position_error_m, velocity_error_m_s


def render_files(
    orbit: Orbit, fitted: FittedElements, latest_text: str, history_text: str,
) -> tuple[str, str, tuple[float, float]]:
    latest_blocks: list[str] = []
    history_blocks = [block.strip() for block in re.split(r"\n\s*\n", history_text.strip()) if block.strip()]
    date_tag = orbit.epoch.strftime("%Y%m%d")
    validation = (0.0, 0.0)
    for name, catalog_number in SATELLITES:
        line1, line2 = build_tle(orbit, fitted, catalog_number, previous_state(latest_text, catalog_number))
        validation = validate_tle(line1, line2, orbit)
        latest_blocks.append(f"{name}\n{line1}\n{line2}")
        history_name = f"{name.removesuffix('-MEAN')}-{date_tag}-MEAN"
        history_blocks = [block for block in history_blocks if block.splitlines()[0] != history_name]
        history_blocks.append(f"{history_name}\n{line1}\n{line2}")
    return "\n\n".join(latest_blocks) + "\n", "\n\n".join(history_blocks) + "\n", validation


def atomic_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Read saved JSON/JSONP instead of the server")
    parser.add_argument("--telemetry-url", default=os.getenv("TELEMETRY_URL", DEFAULT_URL))
    parser.add_argument("--latest", type=Path, default=Path(__file__).resolve().parents[1] / "latest.tle")
    parser.add_argument("--history", type=Path, default=Path(__file__).resolve().parents[1] / "history.tle")
    parser.add_argument("--max-age-hours", type=float, default=48.0)
    parser.add_argument("--allow-stale", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    payload = parse_jsonp(args.input.read_text(encoding="utf-8")) if args.input else fetch_payload(args.telemetry_url)
    orbit = extract_orbit(payload, max_age_hours=args.max_age_hours, allow_stale=args.allow_stale)
    fitted = fit_sgp4(orbit)
    latest_text = args.latest.read_text(encoding="utf-8") if args.latest.exists() else ""
    history_text = args.history.read_text(encoding="utf-8") if args.history.exists() else ""
    new_latest, new_history, residual = render_files(orbit, fitted, latest_text, history_text)
    if args.dry_run:
        print(new_latest, end="")
    else:
        atomic_write(args.latest, new_latest)
        atomic_write(args.history, new_history)
        print(
            f"Built ephemeris for {orbit.epoch.isoformat()}; "
            f"quantized TLE residual {residual[0]:.3f} m, {residual[1]:.4f} m/s"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, KeyError, OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
