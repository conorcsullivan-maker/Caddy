"""
Real-time weather intelligence for Caddy.

Uses the US National Weather Service API (api.weather.gov):
- Free, no API key required
- Requires a User-Agent header identifying the app
- Returns current conditions, hourly forecast, and severe weather alerts
- US-only coverage; international would require a different provider

Weather is cached per (rounded) lat/lng for 10 minutes to keep the system
responsive and avoid hammering the NWS API.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests

NWS_USER_AGENT = "Caddy AI Golf (caddy-sepia.vercel.app, contact@caddy.app)"
NWS_HEADERS = {
    "User-Agent": NWS_USER_AGENT,
    "Accept": "application/geo+json",
}

# In-memory cache: {(rounded_lat, rounded_lng): (timestamp, weather_dict)}
_WEATHER_CACHE: dict = {}
CACHE_TTL_SECONDS = 600  # 10 minutes


def _cache_key(lat: float, lng: float):
    """Round to 2 decimals (~1km) so nearby fetches share cached data."""
    return (round(float(lat), 2), round(float(lng), 2))


def _http_get(url: str, timeout: int = 8):
    try:
        r = requests.get(url, headers=NWS_HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _degrees_to_cardinal(deg: Optional[float]) -> Optional[str]:
    if deg is None:
        return None
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    idx = int((deg + 11.25) // 22.5) % 16
    return dirs[idx]


def fetch_weather(lat: float, lng: float) -> Optional[dict]:
    """Return a structured weather snapshot for the given coordinates,
    or None if NWS doesn't cover this location (e.g. outside the US)."""
    key = _cache_key(lat, lng)
    now = time.time()
    cached = _WEATHER_CACHE.get(key)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1]

    # Step 1: get the grid point + forecast URLs for this location
    point = _http_get(f"https://api.weather.gov/points/{lat},{lng}")
    if not point:
        return None
    props = point.get("properties", {})
    hourly_url = props.get("forecastHourly")
    if not hourly_url:
        return None

    # Steps 2+3 in parallel: the hourly forecast and the alerts feed are
    # independent — no reason to pay two sequential round-trips on a cache
    # miss (the first message of a round).
    with ThreadPoolExecutor(max_workers=2) as pool:
        forecast_f = pool.submit(_http_get, hourly_url)
        alerts_f = pool.submit(
            _http_get, f"https://api.weather.gov/alerts/active?point={lat},{lng}"
        )
        forecast = forecast_f.result()
        alerts_data = alerts_f.result()

    current_period = None
    upcoming_periods = []
    if forecast:
        periods = forecast.get("properties", {}).get("periods", [])
        if periods:
            current_period = periods[0]
            upcoming_periods = periods[1:6]  # next 5 hours

    alert_list = []
    if alerts_data:
        for feat in alerts_data.get("features", []):
            ap = feat.get("properties", {})
            alert_list.append({
                "event": ap.get("event"),                # "Severe Thunderstorm Warning"
                "headline": ap.get("headline"),
                "severity": ap.get("severity"),          # Minor/Moderate/Severe/Extreme
                "urgency": ap.get("urgency"),            # Past/Future/Expected/Immediate
                "ends": ap.get("ends") or ap.get("expires"),
            })

    if not current_period and not alert_list:
        return None

    out = {
        "current": _normalize_period(current_period) if current_period else None,
        "upcoming": [_normalize_period(p) for p in upcoming_periods if p],
        "alerts": alert_list,
        "fetched_at": now,
    }
    _WEATHER_CACHE[key] = (now, out)
    return out


def _normalize_period(period: dict) -> dict:
    # Dew point comes from NWS in Celsius — convert to F to match the rest of
    # the snapshot. Returns None if NWS didn't include it for this period.
    dew_c = (period.get("dewpoint") or {}).get("value")
    dew_f = round(dew_c * 9 / 5 + 32) if isinstance(dew_c, (int, float)) else None
    return {
        "name": period.get("name"),                            # "Tuesday Night"
        "time": period.get("startTime"),
        "temperature": period.get("temperature"),
        "temperature_unit": period.get("temperatureUnit", "F"),
        "wind_speed": period.get("windSpeed"),                 # "5 to 10 mph"
        "wind_direction": period.get("windDirection"),         # "SW"
        "short_forecast": period.get("shortForecast"),         # "Mostly Sunny"
        "precip_chance": (period.get("probabilityOfPrecipitation") or {}).get("value"),
        "humidity": (period.get("relativeHumidity") or {}).get("value"),
        "dew_point_f": dew_f,
    }


def has_critical_alert(weather: dict) -> bool:
    """Returns True if any active alert involves player safety
    (lightning, tornado, severe storm, etc.)."""
    if not weather:
        return False
    critical_events = ("tornado", "thunderstorm", "lightning", "severe", "extreme")
    for a in weather.get("alerts") or []:
        event = (a.get("event") or "").lower()
        severity = (a.get("severity") or "").lower()
        if any(c in event for c in critical_events):
            return True
        if severity in ("severe", "extreme"):
            return True
    return False


def _temperature_play_adjustment(temp_f: Optional[float]) -> Optional[str]:
    """Convert temperature into a rough yardage-adjustment hint. Rule of thumb:
    standard ball-flight reference is ~70°F, and each 10°F deviation shifts
    carry by about 2 yards (more in extremes). Returns a short string or None."""
    if temp_f is None:
        return None
    delta = temp_f - 70
    yards = round(delta / 5)  # ~2 yards per 10°F
    if yards == 0:
        return "Air density ~normal (reference 70°F)"
    if yards > 0:
        return f"Warm air → ball flies ~{yards} yds farther than nominal for irons"
    return f"Cold air → ball carries ~{abs(yards)} yds shorter than nominal for irons"


def format_weather_context(weather: dict) -> str:
    """Format weather + alerts as a system prompt section for Claude."""
    if not weather:
        return ""
    lines = ["\n=== LIVE WEATHER (from National Weather Service) ==="]
    cur = weather.get("current")
    if cur:
        lines.append(f"Conditions: {cur.get('short_forecast') or 'unknown'}")
        temp = cur.get("temperature")
        if temp is not None:
            lines.append(f"Temperature: {temp}°{cur.get('temperature_unit','F')}")
        if cur.get("wind_speed"):
            lines.append(f"Wind: {cur['wind_speed']} from {cur.get('wind_direction','?')}")
        if cur.get("precip_chance") is not None:
            lines.append(f"Chance of rain: {cur['precip_chance']}%")
        if cur.get("humidity") is not None:
            lines.append(f"Humidity: {cur['humidity']}%")
        if cur.get("dew_point_f") is not None:
            lines.append(f"Dew point: {cur['dew_point_f']}°F")
        adj = _temperature_play_adjustment(temp)
        if adj:
            lines.append(f"Ball flight: {adj}")

    upcoming = weather.get("upcoming") or []
    if upcoming:
        lines.append("\nNext few hours:")
        for p in upcoming[:3]:
            chance = p.get("precip_chance")
            chance_str = f" · {chance}% rain" if chance else ""
            lines.append(
                f"  • {p.get('name','?')}: {p.get('short_forecast','?')}, "
                f"{p.get('temperature')}°{p.get('temperature_unit','F')}, "
                f"wind {p.get('wind_speed','?')}{chance_str}"
            )

    alerts = weather.get("alerts") or []
    if alerts:
        lines.append("\n⚠️ ACTIVE WEATHER ALERTS ⚠️")
        for a in alerts:
            lines.append(f"  • {a.get('event','Alert')} ({a.get('severity','?')}/{a.get('urgency','?')}): {a.get('headline','')}")

        if has_critical_alert(weather):
            lines.extend([
                "",
                "SAFETY OVERRIDE — CRITICAL ALERT ACTIVE:",
                "Your top priority right now is the player's safety. Severe weather is",
                "in the area. Recommend they STOP play and seek shelter immediately.",
                "Do not give a club recommendation — get them to safety first.",
            ])

    lines.extend([
        "",
        "Use this weather data when advising on club selection. Factor temperature,",
        "humidity, and precipitation into the recommendation. For WIND direction,",
        "follow the wind-handling rules in the system prompt — the compass direction",
        "above is geographic, not relative to the player.",
    ])

    return "\n".join(lines)
