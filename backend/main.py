"""Dashboard backend for M5PaperS3 home dashboard.

Fetches data from:
- Netatmo: indoor/outdoor temp, humidity, CO2 (OAuth tokens from Firestore)
- Kachelmann: weather forecast with hourly data
- transport.opendata.ch: ZVV bus departures
"""

import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Zurich")

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="M5Paper Dashboard API")

# Config
KACHELMANN_API_KEY = os.getenv("KACHELMANN_API_KEY")
WEATHER_LAT = os.getenv("WEATHER_LATITUDE", "47.382")
WEATHER_LON = os.getenv("WEATHER_LONGITUDE", "8.482")
BUS_STOP_NAME = os.getenv("BUS_STOP_NAME", "Zürich, Letzigrund")


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


class ClothingItem(BaseModel):
    category: str       # KOPF, OBERTEIL, HOSE, HAENDE, SCHUHE
    sprite: str         # sprite filename without extension (e.g. "cap", "raincoat")
    label: str          # display label (e.g. "Mütze", "Regenjacke")


class DashboardData(BaseModel):
    timestamp: str
    next_update: str
    refresh_mode: str           # "high", "low", "sleep"
    sleep_minutes: int          # actual sleep duration for device
    temp_outdoor: float | None
    wind_kmh: int | None
    wind_dir: str | None
    temp_min: float | None
    temp_max: float | None
    rain_max_mm: float
    weather_condition: str
    weather_text: str
    temp_outdoor_24h: list[float]
    rain_mm_24h: list[float]
    current_hour: int
    hour_labels: list[int]
    clothing: list[ClothingItem]
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


async def fetch_weather() -> dict:
    """Fetch weather from Kachelmann hourly forecast (standard/1h)."""
    fallback = {
        "temp_min": None,
        "temp_max": None,
        "wind_kmh": None,
        "wind_dir": None,
        "weather_condition": "unknown",
        "weather_text": "Keine Daten",
        "temp_outdoor_24h": [0.0] * 24,
        "rain_24h": [0.0] * 24,
    }
    if not KACHELMANN_API_KEY:
        return fallback

    headers = {"X-API-Key": KACHELMANN_API_KEY}
    now = datetime.now(LOCAL_TZ)
    today_date = now.date()

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
        first_entry = None

        for entry in entries:
            dt_str = entry.get("dateTime", "")
            try:
                dt = datetime.fromisoformat(dt_str).astimezone(LOCAL_TZ)
                # Offset in hours from current hour (0 = current hour, 1 = next, ...)
                now_hour_start = now.replace(minute=0, second=0, microsecond=0)
                delta_h = round((dt - now_hour_start).total_seconds() / 3600)
                if delta_h < 0 or delta_h >= 24:
                    continue
                temps_next_24h[delta_h] = round(entry.get("temp", 0), 1)
                rain_next_24h[delta_h] = round(entry.get("precCurrent", 0) or 0, 1)
                if first_entry is None:
                    first_entry = entry
            except (ValueError, KeyError):
                continue

        # Fill slot 0 with slot 1 if missing (API starts at next full hour)
        if temps_next_24h[0] == 0.0 and temps_next_24h[1] != 0.0:
            temps_next_24h[0] = temps_next_24h[1]
            rain_next_24h[0] = rain_next_24h[1]

        # Use first entry (closest to now) for wind and weather condition
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
                weather_condition = "clear"
                weather_text = "Klar"
            elif cloud < 75:
                weather_condition = "partly_cloudy"
                weather_text = "Teilweise bewölkt"
            else:
                weather_condition = "cloudy"
                weather_text = "Bewölkt"

        active_temps = [t for t in temps_next_24h if t != 0]
        day_temp_min = min(active_temps) if active_temps else None
        day_temp_max = max(active_temps) if active_temps else None

        wind_kmh = int(cur_wind_ms * 3.6) if cur_wind_ms else None

        return {
            "temp_min": day_temp_min,
            "temp_max": day_temp_max,
            "wind_kmh": wind_kmh,
            "wind_dir": _deg_to_compass(cur_wind_dir),
            "weather_condition": weather_condition,
            "weather_text": weather_text,
            "temps_next_24h": temps_next_24h,
            "rain_next_24h": rain_next_24h,
        }


def _deg_to_compass(deg) -> str | None:
    if deg is None:
        return None
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[int((deg + 22.5) / 45) % 8]


def _symbol_to_condition(symbol: str) -> str:
    s = str(symbol).lower()
    if "rain" in s or "regen" in s or "shower" in s:
        return "rainy"
    if "snow" in s or "schnee" in s:
        return "snowy"
    if "cloud" in s or "wolk" in s or "bewölkt" in s:
        if "partly" in s or "teilweise" in s or "sun" in s:
            return "partly_cloudy"
        return "cloudy"
    if "sun" in s or "clear" in s or "sonn" in s or "klar" in s:
        return "sunny"
    return "partly_cloudy"


def _symbol_to_text_de(symbol: str) -> str:
    """Translate Kachelmann weather symbols to German text."""
    mapping = {
        "sunny": "Sonnig",
        "clear": "Klar",
        "partly cloudy": "Teilweise bewölkt",
        "partly_cloudy": "Teilweise bewölkt",
        "cloudy": "Bewölkt",
        "overcast": "Stark bewölkt",
        "rainy": "Regen",
        "rain": "Regen",
        "snowy": "Schnee",
        "snow": "Schnee",
    }
    return mapping.get(symbol.lower(), symbol.capitalize() if symbol else "Keine Daten")


# --- Clothing Recommendations ---


def recommend_clothing(
    temps_remaining: list[float],
    rain_remaining: list[float],
    wind_kmh: int | None,
) -> list[ClothingItem]:
    """Determine clothing based on remaining-day forecast (temps, rain mm, wind)."""
    if not temps_remaining or all(t == 0 for t in temps_remaining):
        t_min = 10.0
    else:
        t_min = min(temps_remaining)

    max_rain_mm = max(rain_remaining) if rain_remaining else 0
    total_rain_mm = sum(rain_remaining) if rain_remaining else 0
    wind = wind_kmh or 0
    # Wind chill approximation: feels colder with wind
    feels_like = t_min - (wind * 0.1)

    items = []

    # KOPF
    if feels_like < 5:
        items.append(ClothingItem(category="KOPF", sprite="cap", label="Mütze"))
    else:
        items.append(ClothingItem(category="KOPF", sprite="nocap", label="Ohne"))

    # OBERTEIL — rain >= 1mm/h or total >= 3mm means rain jacket
    if max_rain_mm >= 1.0 or total_rain_mm >= 3.0:
        items.append(ClothingItem(category="OBERTEIL", sprite="raincoat", label="Regenjacke"))
    elif feels_like < 15:
        items.append(ClothingItem(category="OBERTEIL", sprite="longsleeves", label="Langarm"))
    else:
        items.append(ClothingItem(category="OBERTEIL", sprite="shortsleeves", label="Kurzarm"))

    # HOSE — heavy rain: rain pants
    if max_rain_mm >= 3.0 or total_rain_mm >= 8.0:
        items.append(ClothingItem(category="HOSE", sprite="rainpants", label="Regenhose"))
    elif feels_like < 15:
        items.append(ClothingItem(category="HOSE", sprite="pants", label="Lange Hose"))
    else:
        items.append(ClothingItem(category="HOSE", sprite="shorts", label="Shorts"))

    # HÄNDE
    if feels_like < 3:
        items.append(ClothingItem(category="HAENDE", sprite="mittens", label="Fäustlinge"))
    elif feels_like < 8:
        items.append(ClothingItem(category="HAENDE", sprite="gloves", label="Handschuhe"))
    else:
        items.append(ClothingItem(category="HAENDE", sprite="nogloves", label="Ohne"))

    # SCHUHE
    if feels_like < 5 or max_rain_mm >= 3.0:
        items.append(ClothingItem(category="SCHUHE", sprite="wintershoes", label="Winterschuhe"))
    else:
        items.append(ClothingItem(category="SCHUHE", sprite="sneakers", label="Sneakers"))

    return items


# --- ZVV Bus Departures ---


async def fetch_bus_departures(limit: int = 15) -> list[dict]:
    """Fetch next bus departures from transport.opendata.ch."""
    url = "https://transport.opendata.ch/v1/stationboard"
    params = {"station": BUS_STOP_NAME, "limit": limit}

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            log.error("ZVV API error: %s", resp.status_code)
            return []

        data = resp.json()
        departures = []
        for entry in data.get("stationboard", []):
            dest = entry.get("to", "?")
            if dest in HIDDEN_DESTINATIONS:
                continue
            if len(departures) >= limit:
                break
            stop = entry.get("stop", {})
            dep_time = stop.get("departure", "")
            # Parse ISO time to HH:MM
            try:
                dt = datetime.fromisoformat(dep_time)
                time_str = dt.strftime("%H:%M")
            except (ValueError, TypeError):
                time_str = dep_time[:5] if dep_time else "??:??"

            departures.append(
                {
                    "line": entry.get("number", entry.get("category", "?")),
                    "dest": entry.get("to", "?"),
                    "time": time_str,
                    "platform": stop.get("platform") or "",
                }
            )
        return departures


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
        weather = await fetch_weather()
    except Exception as e:
        log.error("Weather fetch error: %s", e)
        weather = {
            "temp_min": None,
            "temp_max": None,
            "wind_kmh": None,
            "wind_dir": None,
            "weather_condition": "unknown",
            "weather_text": "Fehler",
            "temps_next_24h": [0.0] * 24,
            "rain_next_24h": [0.0] * 24,
        }
        kachelmann_ok = False

    try:
        bus_limit = 30 if refresh_mode == "low" else 15
        bus_deps = await fetch_bus_departures(limit=bus_limit)
    except Exception as e:
        log.error("Bus fetch error: %s", e)
        bus_deps = []
        bus_ok = False

    # Arrays are already "from now" for next 24h
    temps_24h = weather.get("temps_next_24h", [0.0] * 24)
    rain_24h = weather.get("rain_next_24h", [0.0] * 24)
    hour_labels = [(now.hour + i) % 24 for i in range(24)]

    # For clothing: use forecast from now until midnight
    hours_until_midnight = max(24 - now.hour, 1)
    temps_remaining = temps_24h[:hours_until_midnight]
    rain_remaining = rain_24h[:hours_until_midnight]
    max_rain = max(rain_remaining) if rain_remaining else 0

    clothing = recommend_clothing(temps_remaining, rain_remaining, weather.get("wind_kmh"))

    # Build compact departures for low-refresh mode
    bus_departures_compact = format_compact_departures(bus_deps) if refresh_mode == "low" else []

    return DashboardData(
        timestamp=timestamp,
        next_update=next_update,
        refresh_mode=refresh_mode,
        sleep_minutes=sleep_min,
        temp_outdoor=netatmo.get("temp_outdoor"),
        wind_kmh=weather.get("wind_kmh"),
        wind_dir=weather.get("wind_dir"),
        temp_min=weather.get("temp_min"),
        temp_max=weather.get("temp_max"),
        rain_max_mm=max_rain,
        weather_condition=weather.get("weather_condition", "unknown"),
        weather_text=weather.get("weather_text", ""),
        temp_outdoor_24h=temps_24h,
        rain_mm_24h=rain_24h,
        current_hour=now.hour,
        hour_labels=hour_labels,
        clothing=clothing,
        bus_stop_name=BUS_STOP_NAME,
        bus_departures=[BusDeparture(**d) for d in bus_deps],
        bus_departures_compact=bus_departures_compact,
        battery_pct=73,  # TODO: report from device
        wifi_ok=True,
        weather_api_ok=kachelmann_ok and netatmo_ok,
        bus_api_ok=bus_ok,
    )
