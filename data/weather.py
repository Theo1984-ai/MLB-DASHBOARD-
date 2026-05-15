"""
Open-Meteo client — free, no API key required.
https://api.open-meteo.com
"""

import urllib.request
import ssl
# System cert chain is broken on this Windows install — use unverified
# context for these public-data APIs. No auth/PII flows through these endpoints.
_UNVERIFIED_SSL = ssl._create_unverified_context()
import urllib.error
import json
from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
BASE = "https://api.open-meteo.com/v1/forecast"


def _fetch(url: str) -> dict:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mlb-dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=10, context=_UNVERIFIED_SSL) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return {}


def get_forecast(lat: float, lon: float, target_dt_local: datetime) -> dict:
    """
    Pull hourly forecast for the ballpark and return the slice nearest to first pitch.
    target_dt_local must be a tz-aware datetime in Eastern (or any tz).
    Returns: {temp_f, wind_mph, wind_dir_deg, humidity, precip_pct, condition}
             or empty dict on failure.
    """
    if not lat or not lon:
        return {}

    url = (
        f"{BASE}?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,precipitation_probability,wind_speed_10m,"
        f"wind_direction_10m,relative_humidity_2m,weather_code"
        f"&temperature_unit=fahrenheit&wind_speed_unit=mph"
        f"&timezone=America%2FNew_York&forecast_days=3&past_days=1"
    )
    data = _fetch(url)
    if not data or "hourly" not in data:
        return {}

    hourly = data["hourly"]
    times = hourly.get("time", [])
    if not times:
        return {}

    # Match the hour. Open-Meteo times are local (we requested ET timezone).
    target = target_dt_local.astimezone(EASTERN).strftime("%Y-%m-%dT%H:00")
    idx = None
    for i, t in enumerate(times):
        if t == target:
            idx = i
            break
    if idx is None:
        # Fall back to closest hour by absolute datetime distance
        target_naive = target_dt_local.astimezone(EASTERN).replace(tzinfo=None)
        best = None
        for i, t in enumerate(times):
            try:
                dt = datetime.strptime(t, "%Y-%m-%dT%H:%M")
                diff = abs((dt - target_naive).total_seconds())
                if best is None or diff < best[0]:
                    best = (diff, i)
            except ValueError:
                continue
        if best is None:
            return {}
        idx = best[1]

    return {
        "temp_f":         _safe(hourly.get("temperature_2m", []), idx),
        "wind_mph":       _safe(hourly.get("wind_speed_10m", []), idx),
        "wind_dir_deg":   _safe(hourly.get("wind_direction_10m", []), idx),
        "humidity":       _safe(hourly.get("relative_humidity_2m", []), idx),
        "precip_pct":     _safe(hourly.get("precipitation_probability", []), idx),
        "condition":      _wmo_to_text(_safe(hourly.get("weather_code", []), idx)),
        "forecast_time":  times[idx],
    }


def _safe(arr, i):
    try:
        return arr[i]
    except (IndexError, TypeError):
        return None


# WMO weather code → human label (subset; full list at open-meteo.com docs)
_WMO = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow",
    80: "Rain showers", 81: "Heavy showers", 82: "Violent showers",
    95: "Thunderstorm", 96: "Storm w/ hail", 99: "Severe storm",
}


def _wmo_to_text(code):
    if code is None:
        return "—"
    return _WMO.get(int(code), f"Code {code}")


if __name__ == "__main__":
    from data.stadiums import get_stadium
    yankee = get_stadium(147)
    target = datetime.now(tz=EASTERN).replace(hour=19, minute=0, second=0, microsecond=0)
    fc = get_forecast(yankee["lat"], yankee["lon"], target)
    print(f"Yankee Stadium @ 7pm ET on {target.date()}:")
    for k, v in fc.items():
        print(f"  {k}: {v}")
