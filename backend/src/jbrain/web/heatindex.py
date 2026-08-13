"""The NWS heat index ("feels like" from heat + humidity), shared by the forecast
(`jbrain.web.weather`) and archive (`jbrain.web.weather_history`) tools.

A pure computation with no I/O — lifted here so both weather modules derive the SAME
figure the National Weather Service publishes, and so `weather.py` can compute it without
importing `weather_history.py` (which imports back from `weather.py` — a cycle). Kept
re-exported from `weather_history` for its existing callers.
"""

from __future__ import annotations

import math


def heat_index_f(temp_f: float, rh: float) -> float:
    """The NWS heat index ("feels like" from heat + humidity) in °F, from an air
    temperature (°F) and relative humidity (%). Below ~80 °F apparent, the Steadman
    average applies; at or above it the Rothfusz regression with the NWS low- and
    high-humidity adjustments — the same math the National Weather Service publishes."""
    # Steadman's simpler form first; only escalate to Rothfusz when it lands in-range.
    simple = 0.5 * (temp_f + 61.0 + (temp_f - 68.0) * 1.2 + rh * 0.094)
    if (simple + temp_f) / 2 < 80:
        return simple
    hi = (
        -42.379
        + 2.04901523 * temp_f
        + 10.14333127 * rh
        - 0.22475541 * temp_f * rh
        - 6.83783e-3 * temp_f * temp_f
        - 5.481717e-2 * rh * rh
        + 1.22874e-3 * temp_f * temp_f * rh
        + 8.5282e-4 * temp_f * rh * rh
        - 1.99e-6 * temp_f * temp_f * rh * rh
    )
    if rh < 13 and 80 <= temp_f <= 112:
        hi -= ((13 - rh) / 4) * math.sqrt((17 - abs(temp_f - 95.0)) / 17)
    elif rh > 85 and 80 <= temp_f <= 87:
        hi += ((rh - 85) / 10) * ((87 - temp_f) / 5)
    return hi
