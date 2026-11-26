"""PM2.5 <-> AQI conversion and health bands (US EPA 2024 PM2.5 breakpoints).

The US scale is what phone apps and the Open-Meteo ``us_aqi`` field show.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

WHO_24H_GUIDELINE = 15.0  # µg/m³, WHO Air Quality Guidelines 2021 (24-hour mean)
MAX_READING = 1000.0  # sanity cap applied to either input

# (label, aqi_low, aqi_high, pm25_low, pm25_high) - edit here if the standard changes
BANDS = (
    ("Good", 0, 50, 0.0, 9.0),
    ("Moderate", 51, 100, 9.1, 35.4),
    ("Unhealthy for sensitive groups", 101, 150, 35.5, 55.4),
    ("Unhealthy", 151, 200, 55.5, 125.4),
    ("Very unhealthy", 201, 300, 125.5, 225.4),
    ("Hazardous", 301, 500, 225.5, 325.4),
)


@dataclass(frozen=True)
class AirState:
    pm25: float  # µg/m³
    aqi: int  # US AQI, capped at 500
    level: int  # 0 (good) .. 5 (hazardous); index into BANDS
    label: str
    who_multiple: float  # multiples of the WHO 24-hour guideline

    def to_dict(self) -> dict:
        return asdict(self)


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(value) or not 0 <= value <= MAX_READING:
        raise ValueError(f"{name} must be between 0 and {MAX_READING:g}")
    return float(value)


def _lerp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    """Linear interpolation of x from [x0, x1] onto [y0, y1], clamped to the range."""
    return y0 + (min(max(x, x0), x1) - x0) * (y1 - y0) / (x1 - x0)


def classify(*, pm25: object = None, aqi: object = None) -> AirState:
    """Describe the air from exactly one reading: PM2.5 in µg/m³, or US AQI."""
    if (pm25 is None) == (aqi is None):
        raise ValueError("provide exactly one of pm25 or aqi")
    if pm25 is not None:
        conc = _number(pm25, "pm25")
        level = next((i for i, b in enumerate(BANDS) if conc <= b[4]), len(BANDS) - 1)
        _, a0, a1, p0, p1 = BANDS[level]
        index = round(_lerp(conc, p0, p1, a0, a1))
    else:
        raw = min(_number(aqi, "aqi"), BANDS[-1][2])
        level = next(i for i, b in enumerate(BANDS) if raw <= b[2])
        _, a0, a1, p0, p1 = BANDS[level]
        conc, index = _lerp(raw, a0, a1, p0, p1), round(raw)
    return AirState(
        pm25=round(conc, 1),
        aqi=index,
        level=level,
        label=BANDS[level][0],
        who_multiple=round(conc / WHO_24H_GUIDELINE, 1),
    )
