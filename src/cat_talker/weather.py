"""Read-only weather lookup via Open-Meteo (standard library only).

Architecture:
    Gemini -> get_weather(location, days) [tools.py] -> get_weather()
    -> geocode_location() -> fetch_forecast() -> human-readable text.

No API key. All HTTP uses urllib with hard timeouts and bounded bodies.
This module never raises to the caller: failures become honest strings.
"""

import json
import urllib.parse
import urllib.request
import urllib.error


GEOCODE_ENDPOINT = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT = 10
RESPONSE_MAX_BYTES = 128 * 1024
MIN_DAYS = 1
MAX_DAYS = 7

# WMO weather-code table used by Open-Meteo current/daily blocks.
WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def describe_weather_code(code) -> str:
    """Human label for a WMO weather code; unknown codes stay honest."""
    try:
        return WEATHER_CODES.get(int(code), f"Weather code {code}")
    except (TypeError, ValueError):
        return "Unknown conditions"


def _http_get_json(url: str) -> dict:
    """GET a JSON document with timeout + bounded body. Raises on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "cat-talker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise RuntimeError(f"Weather API HTTP error: {status}")
            raw = resp.read(RESPONSE_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Weather API HTTP error: {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Weather network error: {e.reason}") from e
    except TimeoutError as e:
        raise RuntimeError("Weather request timed out.") from e
    except RuntimeError:
        raise
    except Exception as e:
        msg = str(e).lower()
        if "timed out" in msg or "timeout" in msg:
            raise RuntimeError("Weather request timed out.") from e
        raise RuntimeError(f"Weather network error: {e}") from e
    if len(raw) > RESPONSE_MAX_BYTES:
        raw = raw[:RESPONSE_MAX_BYTES]
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        raise RuntimeError(f"Weather API returned invalid data: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError("Weather API returned malformed data.")
    return data


def _place_label(place: dict) -> str:
    """'Patna, Bihar, India' style label from a geocoding result."""
    parts = [place.get("name"),
             place.get("admin1"),
             place.get("country")]
    return ", ".join(p for p in parts if isinstance(p, str) and p.strip())


def geocode_location(query: str) -> dict:
    """Resolve location text to latitude/longitude.

    Returns {"latitude", "longitude", "label"}. Raises ValueError for
    missing/malformed queries and LookupError when nothing matches.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Weather error: no location given.")
    params = urllib.parse.urlencode(
        {"name": query.strip(), "count": 5,
         "language": "en", "format": "json"})
    data = _http_get_json(GEOCODE_ENDPOINT + "?" + params)
    results = data.get("results")
    if not results:
        raise LookupError(
            f"Weather error: location '{query.strip()}' not found.")
    if not isinstance(results, list):
        raise RuntimeError("Weather geocoding returned malformed data.")
    top = results[0]
    try:
        lat = float(top["latitude"])
        lon = float(top["longitude"])
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError(
            "Weather geocoding returned malformed data.") from e
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise RuntimeError(
            "Weather geocoding returned out-of-range coordinates.")
    label = _place_label(top) or query.strip()
    return {"latitude": lat, "longitude": lon, "label": label}


def fetch_forecast(latitude: float, longitude: float, days: int = 1) -> dict:
    """Fetch and validate the Open-Meteo forecast for coordinates."""
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Weather error: bad coordinates: {e}") from e
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError("Weather error: coordinates out of range.")
    try:
        n_days = int(days)
    except (TypeError, ValueError):
        n_days = MIN_DAYS
    n_days = max(MIN_DAYS, min(MAX_DAYS, n_days))
    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "current": ("temperature_2m,relative_humidity_2m,"
                    "apparent_temperature,weather_code,"
                    "precipitation,wind_speed_10m"),
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                  "precipitation_probability_max"),
        "temperature_unit": "celsius",
        "wind_speed_unit": "kmh",
        "timezone": "auto",
        "forecast_days": n_days,
    })
    data = _http_get_json(FORECAST_ENDPOINT + "?" + params)
    current = data.get("current")
    daily = data.get("daily")
    if not isinstance(current, dict) or not isinstance(daily, dict):
        raise RuntimeError("Weather forecast returned malformed data.")
    try:
        return {
            "days": n_days,
            "temperature": float(current["temperature_2m"]),
            "feels_like": float(current["apparent_temperature"]),
            "weather_code": int(current["weather_code"]),
            "precipitation_mm": float(current.get("precipitation", 0.0)),
            "humidity_pct": float(current.get("relative_humidity_2m", 0.0)),
            "wind_kmh": float(current["wind_speed_10m"]),
            "dates": list(daily["time"]),
            "daily_codes": [int(c) for c in daily["weather_code"]],
            "highs": [float(v) for v in daily["temperature_2m_max"]],
            "lows": [float(v) for v in daily["temperature_2m_min"]],
            "rain_chance": [float(v) for v in
                             daily.get("precipitation_probability_max",
                                       [0.0] * len(daily["time"]))],
        }
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError(
            "Weather forecast returned malformed data.") from e


def _fmt(value, unit: str) -> str:
    """Format a number for display; non-numeric values stay honest."""
    try:
        return f"{float(value):g} {unit}"
    except (TypeError, ValueError):
        return f"unknown {unit}"


def render_weather(label: str, forecast: dict) -> str:
    """Human-readable weather text in Celsius and km/h."""
    lines = [f"Weather for {label}:"]
    lines.append(
        f"Current: {_fmt(forecast['temperature'], '°C')}, "
        f"feels like {_fmt(forecast['feels_like'], '°C')}, "
        f"{describe_weather_code(forecast['weather_code'])}.")
    lines.append(
        f"Precipitation: {_fmt(forecast['precipitation_mm'], 'mm')} now; "
        f"humidity {_fmt(forecast['humidity_pct'], '%')}.")
    lines.append(f"Wind: {_fmt(forecast['wind_kmh'], 'km/h')}.")
    dates = forecast["dates"]
    if dates:
        chance = (forecast["rain_chance"][0]
                  if forecast["rain_chance"] else 0.0)
        lines.append(
            f"Today ({dates[0]}): high {_fmt(forecast['highs'][0], '°C')}, "
            f"low {_fmt(forecast['lows'][0], '°C')}, "
            f"{describe_weather_code(forecast['daily_codes'][0])}, "
            f"rain chance {_fmt(chance, '%')}.")
    for i in range(1, min(forecast["days"], len(dates))):
        chance = (forecast["rain_chance"][i]
                  if i < len(forecast["rain_chance"]) else 0.0)
        lines.append(
            f"{dates[i]}: high {_fmt(forecast['highs'][i], '°C')}, "
            f"low {_fmt(forecast['lows'][i], '°C')}, "
            f"{describe_weather_code(forecast['daily_codes'][i])}, "
            f"rain chance {_fmt(chance, '%')}.")
    return "\n".join(lines)


def get_weather(location: str, days: int = 1) -> str:
    """Public tool: weather for an explicit location. Never raises."""
    try:
        if not isinstance(location, str) or not location.strip():
            return ("Weather error: please name a place "
                    "(e.g. 'Patna' or 'New Delhi').")
        try:
            place = geocode_location(location)
        except (ValueError, LookupError, RuntimeError) as e:
            return str(e)
        try:
            forecast = fetch_forecast(
                place["latitude"], place["longitude"], days)
        except (ValueError, RuntimeError) as e:
            return str(e)
        return render_weather(place["label"], forecast)
    except Exception as e:
        return f"Weather lookup failed: {e}"
