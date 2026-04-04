# M5Paper S3 Home Dashboard

E-ink home dashboard for the M5Paper S3 (ESP32-S3, 960×540 e-ink display). Shows weather, clothing recommendations, temperature/rain chart, and bus departures — updated every 15 minutes via deep sleep wake cycle.

## Screenshots

![Dashboard](screenshot.jpg)

## Architecture

```
┌─────────────────┐     HTTP/JSON      ┌──────────────────┐
│   M5Paper S3    │ ◄────────────────── │  Backend (FastAPI)│
│   (ESP32-S3)    │                     │  Cloud Run / local│
└─────────────────┘                     └──────┬───────────┘
                                               │
                              ┌────────────────┼────────────────┐
                              │                │                │
                    ┌─────────▼──┐   ┌─────────▼──┐   ┌────────▼───┐
                    │ Kachelmann │   │  Kitchen    │   │ transport  │
                    │ Weather API│   │  Display API│   │ .opendata  │
                    └────────────┘   └────────────┘   └────────────┘
                    Hourly forecast   Indoor/outdoor   ZVV bus
                    temp, rain, wind  temp, CO2        departures
```

## Project Structure

```
├── backend/             Python FastAPI backend
│   └── main.py          Dashboard API endpoint
├── m5paper_hw/          ESP-IDF firmware for M5Paper S3
│   └── main/
│       ├── main.cpp             Rendering + WiFi + HTTP fetch
│       ├── clothing_sprites.h   2-bit grayscale clothing icons (auto-generated)
│       └── weather_icons_2bit.h 2-bit grayscale weather icons (auto-generated)
├── assets/
│   └── sprites/         Source PNGs for clothing icons (150×150)
├── simulator/
│   ├── dashboard.py     Python dashboard mockup generator
│   └── icons/           Source PNGs for weather icons
├── tools/
│   └── png_to_header.py Converts PNGs to 2-bit C header arrays
├── Taskfile.yml         Build/flash/monitor commands
└── CLAUDE.md            AI assistant context
```

## Dashboard Features

- **Weather**: Current outdoor temperature, weather icon, wind speed/direction
- **Clothing**: 5 recommendation cards (head, top, pants, hands, shoes) based on forecast
- **Chart**: 24h temperature line + rain bars (mm) starting from current hour
- **Bus departures**: Next 5 departures from configured stop (ZVV)
- **Battery**: Real-time battery level from hardware
- **Deep sleep**: Wakes every 15 minutes to refresh

## Prerequisites

- ESP-IDF v5.3 installed at `~/esp/esp-idf-v5.3`
- Python 3.x with `httpx`, `fastapi`, `uvicorn`, `python-dotenv`
- [Task](https://taskfile.dev/) runner

## Quick Start

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install httpx fastapi uvicorn python-dotenv
task backend
```

### Firmware

```bash
# Build
task build

# Flash (device must be connected via USB)
task flash

# Flash + serial monitor
task flash-monitor

# If device is in deep sleep: hold Boot, press Reset, release Boot, then flash
```

### Regenerate Sprites

```bash
python3 tools/png_to_header.py
```

## Configuration

### Backend (`backend/.env`)

```
KACHELMANN_API_KEY=...
WEATHER_LATITUDE=47.382
WEATHER_LONGITUDE=8.482
BUS_STOP_NAME=Zürich, Rautistrasse
```

### Firmware (`m5paper_hw/main/main.cpp`)

```c
#define WIFI_SSID       "your-ssid"
#define WIFI_PASS       "your-password"
#define API_URL         "https://your-backend/dashboard"
#define SLEEP_MINUTES   15
```

---
Updated: 2026-04-04, sha: 82a1a0d
