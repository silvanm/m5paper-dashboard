"""Backend for Weather & Departures Display.

Fetches data from:
- Netatmo: indoor/outdoor temp, humidity, CO2 (via Kitchen Display backend)
- Kachelmann: weather forecast (hourly + 3-day) with period conditions
- transport.opendata.ch: ZVV bus departures
"""

import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Zurich")

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Weather & Departures Display API")

# IP allowlist
ALLOWED_IPS = [ip.strip() for ip in os.getenv("ALLOWED_IPS", "").split(",") if ip.strip()]


@app.middleware("http")
async def ip_filter(request: Request, call_next):
    if ALLOWED_IPS:
        # Cloud Run: real client IP is in X-Forwarded-For (first entry)
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
        else:
            client_ip = request.client.host if request.client else None
        if client_ip not in ALLOWED_IPS:
            log.warning("Blocked request from %s", client_ip)
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    return await call_next(request)


# Config
KACHELMANN_API_KEY = os.getenv("KACHELMANN_API_KEY")
WEATHER_LAT = os.getenv("WEATHER_LATITUDE", "47.382")
WEATHER_LON = os.getenv("WEATHER_LONGITUDE", "8.482")
BUS_STOP_NAME = os.getenv("BUS_STOP_NAME", "Zürich, Rautistrasse")

# Additional stops with line/direction filters
EXTRA_STOPS = [
    {"stop": "Zürich, Lindenplatz", "lines": {"2"}, "destinations": {"Zürich, Klusplatz"}},
    {"stop": "Zürich, Fellenbergstrasse", "lines": {"3"}, "destinations": {"Zürich, Klusplatz"}},
]


# --- Models ---


class BusDeparture(BaseModel):
    line: str
    dest: str
    time: str
    platform: str


class CompactBusDeparture(BaseModel):
    line: str
    dest: str
    times: list[str]


class WeatherPeriod(BaseModel):
    label: str          # "vormittags", "nachmittags", "abends"
    condition: str      # "sunny", "partly_cloudy", "cloudy", "rainy", "snowy"


class DashboardData(BaseModel):
    timestamp: str
    next_update: str
    refresh_mode: str           # "high", "low", "sleep"
    sleep_minutes: int          # actual sleep duration for device
    temp_outdoor: float | None
    wind_kmh: int | None
    wind_dir: str | None
    wind_gust_kmh: int | None
    temp_min: float | None
    temp_max: float | None
    rain_probability_pct: int   # 0-100 from Kachelmann 3-day
    precip_total_mm: float      # total precipitation mm
    weather_condition: str
    weather_text: str
    weather_periods: list[WeatherPeriod]  # 3 periods: morning/afternoon/evening
    temp_outdoor_24h: list[float]
    rain_mm_24h: list[float]
    current_hour: int
    hour_labels: list[int]
    bus_stop_name: str
    bus_departures: list[BusDeparture]
    bus_departures_compact: list[CompactBusDeparture]
    battery_pct: int
    wifi_ok: bool
    weather_api_ok: bool
    bus_api_ok: bool


# --- Indoor/Outdoor data (via Kitchen Display backend) ---

KITCHEN_DISPLAY_URL = "https://kitchendisplay2025.muehlemann.com/api/weather"


async def fetch_netatmo() -> dict:
    """Fetch indoor/outdoor data from Kitchen Display backend (which talks to Netatmo)."""
    fallback = {
        "temp_indoor": None,
        "humidity_indoor": None,
        "co2_ppm": None,
        "temp_outdoor": None,
        "humidity_outdoor": None,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(KITCHEN_DISPLAY_URL, timeout=10)
        if resp.status_code != 200:
            log.error("Kitchen Display API error: %s %s", resp.status_code, resp.text[:200])
            return fallback

        data = resp.json()
        return {
            "temp_indoor": data.get("indoorTemp"),
            "humidity_indoor": None,
            "co2_ppm": int(data["co2"]) if data.get("co2") is not None else None,
            "temp_outdoor": data.get("outdoorTemp"),
            "humidity_outdoor": None,
        }


# --- Weather (Kachelmann hourly + 3-day summary) ---


async def fetch_weather_hourly() -> dict:
    """Fetch weather from Kachelmann hourly forecast (standard/1h)."""
    fallback = {
        "temp_min": None,
        "temp_max": None,
        "wind_kmh": None,
        "wind_dir": None,
        "wind_gust_kmh": None,
        "weather_condition": "unknown",
        "weather_text": "Keine Daten",
        "weather_periods": [],
        "temp_outdoor_24h": [0.0] * 24,
        "rain_24h": [0.0] * 24,
        "precip_total_mm": 0.0,
    }
    if not KACHELMANN_API_KEY:
        return fallback

    headers = {"X-API-Key": KACHELMANN_API_KEY}
    now = datetime.now(LOCAL_TZ)

    async with httpx.AsyncClient() as client:
        hourly_url = f"https://api.kachelmannwetter.com/v02/forecast/{WEATHER_LAT}/{WEATHER_LON}/standard/1h"
        resp = await client.get(hourly_url, headers=headers)
        if resp.status_code != 200:
            log.error(
                "Kachelmann hourly error: %s %s", resp.status_code, resp.text[:200]
            )
            return fallback

        hourly_data = resp.json()
        entries = hourly_data.get("data", [])

        # Build arrays for next 24h, indexed by offset from current hour
        temps_next_24h = [0.0] * 24
        rain_next_24h = [0.0] * 24  # precipitation in mm (precCurrent)
        cur_wind_ms = None
        cur_wind_dir = None
        max_wind_gust_ms = 0.0
        first_entry = None

        # Per-period data for deriving conditions (keyed by hour 0-23)
        hourly_cloud = {}  # hour -> cloudCoverage
        hourly_rain = {}   # hour -> precCurrent

        for entry in entries:
            dt_str = entry.get("dateTime", "")
            try:
                dt = datetime.fromisoformat(dt_str).astimezone(LOCAL_TZ)
                now_hour_start = now.replace(minute=0, second=0, microsecond=0)
                delta_h = round((dt - now_hour_start).total_seconds() / 3600)
                if delta_h < 0 or delta_h >= 24:
                    continue
                temps_next_24h[delta_h] = round(entry.get("temp", 0), 1)
                rain_next_24h[delta_h] = round(entry.get("precCurrent", 0) or 0, 1)

                # Track wind gust
                gust = entry.get("windGust", 0) or 0
                if gust > max_wind_gust_ms:
                    max_wind_gust_ms = gust

                # Track per-hour cloud + rain for period derivation
                hour = dt.hour
                hourly_cloud[hour] = entry.get("cloudCoverage", 50)
                hourly_rain[hour] = entry.get("precCurrent", 0) or 0

                if first_entry is None:
                    first_entry = entry
            except (ValueError, KeyError):
                continue

        # Fill slot 0 with slot 1 if missing (API starts at next full hour)
        if temps_next_24h[0] == 0.0 and temps_next_24h[1] != 0.0:
            temps_next_24h[0] = temps_next_24h[1]
            rain_next_24h[0] = rain_next_24h[1]

        # Current weather condition from first entry
        weather_condition = "unknown"
        weather_text = "Keine Daten"
        if first_entry:
            cur_wind_ms = first_entry.get("windSpeed")
            cur_wind_dir = first_entry.get("windDirection")
            cloud = first_entry.get("cloudCoverage", 50)
            has_rain = (first_entry.get("precCurrent", 0) or 0) > 0
            if has_rain:
                weather_condition = "rainy"
                weather_text = "Regen"
            elif cloud < 25:
                weather_condition = "sunny"
                weather_text = "Sonnig"
            elif cloud < 75:
                weather_condition = "partly_cloudy"
                weather_text = "Teilweise bewölkt"
            else:
                weather_condition = "cloudy"
                weather_text = "Bewölkt"

        # Derive period conditions from hourly data (fallback if 3-day unavailable)
        periods = _derive_periods_from_hourly(hourly_cloud, hourly_rain)

        active_temps = [t for t in temps_next_24h if t != 0]
        day_temp_min = min(active_temps) if active_temps else None
        day_temp_max = max(active_temps) if active_temps else None
        wind_kmh = int(cur_wind_ms * 3.6) if cur_wind_ms else None
        wind_gust_kmh = int(max_wind_gust_ms * 3.6) if max_wind_gust_ms else None
        precip_total = round(sum(rain_next_24h), 1)

        return {
            "temp_min": day_temp_min,
            "temp_max": day_temp_max,
            "wind_kmh": wind_kmh,
            "wind_dir": _deg_to_compass(cur_wind_dir),
            "wind_gust_kmh": wind_gust_kmh,
            "weather_condition": weather_condition,
            "weather_text": weather_text,
            "weather_periods": periods,
            "temps_next_24h": temps_next_24h,
            "rain_next_24h": rain_next_24h,
            "precip_total_mm": precip_total,
        }


def _derive_periods_from_hourly(
    hourly_cloud: dict[int, float], hourly_rain: dict[int, float]
) -> list[dict]:
    """Derive morning/afternoon/evening weather conditions from hourly data."""
    period_ranges = [
        ("vormittags", range(6, 12)),
        ("nachmittags", range(12, 18)),
        ("abends", range(18, 24)),
    ]
    periods = []
    for label, hours in period_ranges:
        clouds = [hourly_cloud[h] for h in hours if h in hourly_cloud]
        rains = [hourly_rain[h] for h in hours if h in hourly_rain]
        avg_cloud = sum(clouds) / len(clouds) if clouds else 50
        max_rain = max(rains) if rains else 0
        if max_rain > 0.1:
            cond = "rainy"
        elif avg_cloud < 25:
            cond = "sunny"
        elif avg_cloud < 75:
            cond = "partly_cloudy"
        else:
            cond = "cloudy"
        periods.append({"label": label, "condition": cond})
    return periods


async def fetch_weather_3day() -> dict:
    """Fetch Kachelmann 3-day forecast for rain probability and period weather symbols."""
    fallback = {"rain_probability_pct": 0, "periods": None}
    if not KACHELMANN_API_KEY:
        return fallback

    headers = {"X-API-Key": KACHELMANN_API_KEY}
    now = datetime.now(LOCAL_TZ)
    today_str = now.date().isoformat()

    async with httpx.AsyncClient() as client:
        url = f"https://api.kachelmannwetter.com/v02/forecast/{WEATHER_LAT}/{WEATHER_LON}/3day"
        resp = await client.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            log.error("Kachelmann 3-day error: %s %s", resp.status_code, resp.text[:200])
            return fallback

        data = resp.json()
        entries = data.get("data", [])

        # Try to find today's entry
        day_entry = None
        for entry in entries:
            if entry.get("dateTime") == today_str:
                day_entry = entry
                break

        # Fall back to first entry (tomorrow) if today not available
        if day_entry is None and entries:
            day_entry = entries[0]

        if not day_entry:
            return fallback

        rain_prob = day_entry.get("precProb", 0)

        # Extract period weather symbols if available
        tod = day_entry.get("timeOfDay", {})
        periods = None
        if tod:
            symbol_map = {
                "morning": "vormittags",
                "afternoon": "nachmittags",
                "evening": "abends",
            }
            periods = []
            for api_key, label in symbol_map.items():
                period_data = tod.get(api_key, {})
                symbol = period_data.get("weatherSymbol", "partlycloudy")
                periods.append({
                    "label": label,
                    "condition": _kachelmann_symbol_to_condition(symbol),
                })

        return {
            "rain_probability_pct": rain_prob,
            "periods": periods,
        }


def _deg_to_compass(deg) -> str | None:
    if deg is None:
        return None
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[int((deg + 22.5) / 45) % 8]


def _kachelmann_symbol_to_condition(symbol: str) -> str:
    """Map Kachelmann weatherSymbol to our icon condition names."""
    s = symbol.lower()
    if "rain" in s or "shower" in s or "thunder" in s or "drizzle" in s:
        return "rainy"
    if "snow" in s or "sleet" in s:
        return "snowy"
    if s == "partlycloudy" or s == "partly_cloudy":
        return "partly_cloudy"
    if "cloud" in s or "overcast" in s:
        return "cloudy"
    if "sun" in s or "clear" in s or "fair" in s:
        return "sunny"
    return "partly_cloudy"


# --- ZVV Bus Departures ---


def _parse_stationboard(data: dict, line_filter: set | None = None,
                        dest_filter: set | None = None, limit: int = 30) -> list[dict]:
    """Parse stationboard response, optionally filtering by line and destination."""
    departures = []
    for entry in data.get("stationboard", []):
        dest = entry.get("to", "?")
        if dest in HIDDEN_DESTINATIONS:
            continue
        line = entry.get("number", entry.get("category", "?"))
        if line_filter and line not in line_filter:
            continue
        if dest_filter and dest not in dest_filter:
            continue
        if len(departures) >= limit:
            break
        stop = entry.get("stop", {})
        dep_time = stop.get("departure", "")
        try:
            dt = datetime.fromisoformat(dep_time)
            time_str = dt.strftime("%H:%M")
            sort_key = dt.isoformat()
        except (ValueError, TypeError):
            time_str = dep_time[:5] if dep_time else "??:??"
            sort_key = time_str
        departures.append({
            "line": line, "dest": dest, "time": time_str,
            "platform": stop.get("platform") or "", "_sort": sort_key,
        })
    return departures


async def fetch_bus_departures(limit: int = 15, minutes_ahead: int | None = None) -> list[dict]:
    """Fetch bus/tram departures from main stop + extra stops, merged by time.

    If minutes_ahead is set, only include departures within that time window.
    """
    url = "https://transport.opendata.ch/v1/stationboard"
    now = datetime.now(LOCAL_TZ)

    # If filtering by time, fetch more to ensure we have enough after filtering
    fetch_limit = max(limit, 50) if minutes_ahead else limit

    async with httpx.AsyncClient() as client:
        # Fetch main stop
        resp = await client.get(url, params={"station": BUS_STOP_NAME, "limit": fetch_limit})
        if resp.status_code != 200:
            log.error("ZVV API error for %s: %s", BUS_STOP_NAME, resp.status_code)
            return []
        all_deps = _parse_stationboard(resp.json(), limit=fetch_limit)

        # Fetch extra stops (filtered by line/destination)
        for extra in EXTRA_STOPS:
            try:
                resp2 = await client.get(url, params={"station": extra["stop"], "limit": 30})
                if resp2.status_code == 200:
                    deps = _parse_stationboard(
                        resp2.json(),
                        line_filter=extra.get("lines"),
                        dest_filter=extra.get("destinations"),
                        limit=30,
                    )
                    all_deps.extend(deps)
            except Exception as e:
                log.error("ZVV API error for %s: %s", extra["stop"], e)

        # Sort all by departure time
        all_deps.sort(key=lambda d: d["_sort"])

        # Filter by time window if requested
        if minutes_ahead:
            from datetime import timedelta
            cutoff = now + timedelta(minutes=minutes_ahead)
            cutoff_str = cutoff.isoformat()
            all_deps = [d for d in all_deps if d["_sort"] <= cutoff_str]

        # Strip internal sort key
        for d in all_deps:
            del d["_sort"]
        return all_deps


# --- Refresh Schedule ---

HIDDEN_DESTINATIONS = {"Zürich, Dunkelhölzli"}


def get_refresh_schedule(now: datetime) -> tuple[str, int]:
    """Return (mode, sleep_minutes) based on time of day and weekday/weekend.

    Schedule:
    - 05:00-09:00 every day: high (15min)
    - 09:00-17:00 weekends: high (15min)
    - 09:00-21:00 weekdays: low (60min)
    - 17:00-21:00 weekends: low (60min)
    - 21:00-05:00 every day: sleep (until 05:00)
    """
    hour = now.hour
    is_weekend = now.weekday() >= 5  # Saturday=5, Sunday=6

    if hour >= 21 or hour < 5:
        # Sleep until 05:00
        if hour >= 21:
            minutes_until_5 = (24 - hour + 5) * 60 - now.minute
        else:
            minutes_until_5 = (5 - hour) * 60 - now.minute
        return ("sleep", minutes_until_5)

    if 5 <= hour < 9:
        return ("high", 15)

    if is_weekend and 9 <= hour < 17:
        return ("high", 15)

    # Weekday 09-21 or weekend 17-21
    return ("low", 60)


def strip_zurich_prefix(dest: str) -> str:
    """Strip 'Zürich, ' or 'Zürich ' prefix from destination names."""
    if dest.startswith("Zürich, "):
        return dest[len("Zürich, "):]
    if dest.startswith("Zürich ") and not dest.startswith("Zürichs"):
        return dest[len("Zürich "):]
    return dest


def format_compact_departures(departures: list[dict]) -> list[CompactBusDeparture]:
    """Group departures by (line, dest), strip Zürich, format times compactly."""
    from collections import OrderedDict

    groups: OrderedDict[tuple[str, str], list[str]] = OrderedDict()

    for dep in departures:
        raw_dest = dep.get("dest", "?")
        if raw_dest in HIDDEN_DESTINATIONS:
            continue
        line = dep.get("line", "?")
        dest = strip_zurich_prefix(raw_dest)
        time = dep.get("time", "??:??")
        key = (line, dest)
        if key not in groups:
            groups[key] = []
        groups[key].append(time)

    result = []
    for (line, dest), times in groups.items():
        formatted = []
        prev_hour = None
        for t in times:
            hour = t[:2] if len(t) >= 5 else None
            if prev_hour is None:
                formatted.append(t)
            elif hour != prev_hour:
                formatted.append(t)
            else:
                formatted.append(t[2:])  # ":MM"
            prev_hour = hour
        result.append(CompactBusDeparture(line=line, dest=dest, times=formatted))

    return result


# --- Main endpoint ---


@app.get("/dashboard", response_model=DashboardData)
async def get_dashboard():
    now = datetime.now(LOCAL_TZ)
    timestamp = now.strftime("%a %d.%m.%Y, %H:%M")

    from datetime import timedelta
    refresh_mode, sleep_min = get_refresh_schedule(now)
    next_time = now + timedelta(minutes=sleep_min)
    next_update = next_time.strftime("%H:%M")

    # Fetch all data sources
    netatmo_ok = True
    kachelmann_ok = True
    bus_ok = True

    try:
        netatmo = await fetch_netatmo()
    except Exception as e:
        log.error("Netatmo fetch error: %s", e)
        netatmo = {"temp_outdoor": None}
        netatmo_ok = False

    try:
        weather = await fetch_weather_hourly()
    except Exception as e:
        log.error("Weather hourly fetch error: %s", e)
        weather = {
            "temp_min": None, "temp_max": None,
            "wind_kmh": None, "wind_dir": None, "wind_gust_kmh": None,
            "weather_condition": "unknown", "weather_text": "Fehler",
            "weather_periods": [], "temps_next_24h": [0.0] * 24,
            "rain_next_24h": [0.0] * 24, "precip_total_mm": 0.0,
        }
        kachelmann_ok = False

    # Fetch 3-day forecast for rain probability and period weather symbols
    weather_3day = {"rain_probability_pct": 0, "periods": None}
    try:
        weather_3day = await fetch_weather_3day()
    except Exception as e:
        log.error("Weather 3-day fetch error: %s", e)

    # Use 3-day period symbols if available, otherwise fall back to hourly-derived
    periods = weather_3day.get("periods") or weather.get("weather_periods", [])
    rain_prob = weather_3day.get("rain_probability_pct", 0)

    try:
        bus_deps = await fetch_bus_departures(
            limit=50, minutes_ahead=sleep_min if refresh_mode == "low" else None,
        )
    except Exception as e:
        log.error("Bus fetch error: %s", e)
        bus_deps = []
        bus_ok = False

    # Arrays are already "from now" for next 24h
    temps_24h = weather.get("temps_next_24h", [0.0] * 24)
    rain_24h = weather.get("rain_next_24h", [0.0] * 24)
    hour_labels = [(now.hour + i) % 24 for i in range(24)]

    # Outdoor temp: prefer Netatmo, fall back to Kachelmann forecast
    temp_outdoor = netatmo.get("temp_outdoor")
    if temp_outdoor is None and temps_24h[0] != 0.0:
        temp_outdoor = temps_24h[0]
        log.info("Using Kachelmann forecast temp as Netatmo fallback: %.1f", temp_outdoor)

    # Build compact departures for low-refresh mode
    bus_departures_compact = format_compact_departures(bus_deps) if refresh_mode == "low" else []

    return DashboardData(
        timestamp=timestamp,
        next_update=next_update,
        refresh_mode=refresh_mode,
        sleep_minutes=sleep_min,
        temp_outdoor=temp_outdoor,
        wind_kmh=weather.get("wind_kmh"),
        wind_dir=weather.get("wind_dir"),
        wind_gust_kmh=weather.get("wind_gust_kmh"),
        temp_min=weather.get("temp_min"),
        temp_max=weather.get("temp_max"),
        rain_probability_pct=rain_prob,
        precip_total_mm=weather.get("precip_total_mm", 0.0),
        weather_condition=weather.get("weather_condition", "unknown"),
        weather_text=weather.get("weather_text", ""),
        weather_periods=[WeatherPeriod(**p) for p in periods],
        temp_outdoor_24h=temps_24h,
        rain_mm_24h=rain_24h,
        current_hour=now.hour,
        hour_labels=hour_labels,
        bus_stop_name=BUS_STOP_NAME,
        bus_departures=[BusDeparture(**d) for d in bus_deps],
        bus_departures_compact=bus_departures_compact,
        battery_pct=73,  # TODO: report from device
        wifi_ok=True,
        weather_api_ok=kachelmann_ok and netatmo_ok,
        bus_api_ok=bus_ok,
    )
