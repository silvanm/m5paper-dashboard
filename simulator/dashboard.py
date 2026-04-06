"""M5PaperS3 Dashboard Simulator — renders dashboard at 960x540 using pygame.

Fetches data from the backend API and renders a pixel-accurate e-ink-style display.
Press R to refresh, Q/Esc to quit.
"""

import os
import sys
import math
import requests
import pygame
import pygame.freetype

SCREEN_W = 960
SCREEN_H = 540
API_URL = "http://127.0.0.1:8090/dashboard"
ICONS_DIR = os.path.join(os.path.dirname(__file__), "icons")

# E-ink grayscale palette
BG = (232, 228, 216)
BLACK = (26, 26, 26)
DARK = (58, 58, 58)
MID = (122, 122, 122)
LIGHT = (176, 176, 168)
FAINT = (208, 205, 196)


def fetch_data() -> dict | None:
    try:
        resp = requests.get(API_URL, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"API error: {e}")
        return None


def load_weather_icons() -> dict[str, pygame.Surface]:
    """Load and scale weather icon PNGs."""
    icons = {}
    for name in ("sunny", "partly_cloudy", "cloudy", "rainy", "snowy"):
        path = os.path.join(ICONS_DIR, f"{name}.png")
        if os.path.exists(path):
            img = pygame.image.load(path).convert_alpha()
            icons[name] = pygame.transform.smoothscale(img, (80, 80))
    return icons


def draw_dashboard(screen: pygame.Surface, data: dict, fonts: dict, icons: dict):
    screen.fill(BG)
    f_sm = fonts["sm"]
    f_md = fonts["md"]
    f_lg = fonts["lg"]
    f_xl = fonts["xl"]
    f_title = fonts["title"]

    # ====== HEADER ======
    pygame.draw.rect(screen, BLACK, (0, 0, SCREEN_W, 56))
    f_title.render_to(screen, (20, 15), "WARDROBE DISPLAY", BG)
    ts_text = f"Stand: {data['timestamp']}  ·  Nächstes Update ~{data['next_update']}"
    f_sm.render_to(screen, (260, 22), ts_text, BG)

    # ====== CURRENT TEMPS - LEFT PANEL ======
    panel_y = 72
    panel_h = 160

    # Indoor
    f_sm.render_to(screen, (30, panel_y + 6), "INNEN", DARK)
    ti = data.get("temp_indoor")
    f_xl.render_to(screen, (20, panel_y + 36), f"{ti:.0f}°" if ti else "--°", BLACK)
    hi = data.get("humidity_indoor")
    co2 = data.get("co2_ppm")
    f_sm.render_to(screen, (30, panel_y + 118), f"Luftfeucht. {hi}%" if hi else "Luftfeucht. --", MID)
    f_sm.render_to(screen, (30, panel_y + 138), f"CO₂  {co2} ppm" if co2 else "CO₂  -- ppm", MID)

    # Dashed divider
    for y in range(panel_y, panel_y + panel_h, 8):
        pygame.draw.line(screen, LIGHT, (215, y), (215, y + 4), 1)

    # Outdoor
    f_sm.render_to(screen, (235, panel_y + 6), "AUSSEN", DARK)
    to = data.get("temp_outdoor")
    f_xl.render_to(screen, (230, panel_y + 36), f"{to:.0f}°" if to else "--°", BLACK)
    ho = data.get("humidity_outdoor")
    wind = data.get("wind_kmh")
    wdir = data.get("wind_dir") or ""
    f_sm.render_to(screen, (235, panel_y + 118), f"Luftfeucht. {ho}%" if ho else "Luftfeucht. --", MID)
    f_sm.render_to(screen, (235, panel_y + 138), f"Wind {wind} km/h {wdir}" if wind else "Wind --", MID)

    # Weather icon (bitmap)
    cond = data.get("weather_condition", "unknown")
    icon = icons.get(cond) or icons.get("partly_cloudy")
    if icon:
        screen.blit(icon, (430, panel_y + 10))

    # Min/Max + condition text
    tmin = data.get("temp_min")
    tmax = data.get("temp_max")
    minmax = f"↓ {tmin:.0f}°  ↑ {tmax:.0f}°" if tmin is not None and tmax is not None else "-- / --"
    f_md.render_to(screen, (430, panel_y + 100), minmax, DARK)
    f_sm.render_to(screen, (430, panel_y + 122), data.get("weather_text", ""), MID)

    # ====== SEPARATOR ======
    pygame.draw.line(screen, LIGHT, (20, panel_y + panel_h + 8), (530, panel_y + panel_h + 8), 1)

    # ====== CHART AREA ======
    chart_x = 55
    chart_y = 270
    chart_w = 470
    chart_h = 175
    chart_bottom = chart_y + chart_h

    f_md.render_to(screen, (20, chart_y - 20), "TEMPERATUR & REGEN  —  24h", BLACK)

    temps = data.get("temp_outdoor_24h", [0] * 24)
    rain = data.get("rain_prob_24h", [0] * 24)
    hour_labels = data.get("hour_labels", list(range(24)))

    # Y-axis range (outdoor temps only, no indoor)
    valid_temps = [t for t in temps if t != 0]
    if valid_temps:
        temp_min_chart = min(min(valid_temps) - 2, 0)
        temp_max_chart = max(valid_temps) + 3
    else:
        temp_min_chart, temp_max_chart = -5, 25
    temp_range = max(temp_max_chart - temp_min_chart, 1)

    # Y-axis labels + grid
    for t in range(int(temp_min_chart), int(temp_max_chart) + 1, 5):
        y = int(chart_bottom - ((t - temp_min_chart) / temp_range) * chart_h)
        f_sm.render_to(screen, (chart_x - 35, y - 5), f"{t}°", MID)
        pygame.draw.line(screen, FAINT, (chart_x, y), (chart_x + chart_w, y), 1)

    # X-axis labels
    for i in range(0, 24, 3):
        x = chart_x + int((i / 23) * chart_w)
        f_sm.render_to(screen, (x - 8, chart_bottom + 6), f"{hour_labels[i]:02d}", MID)

    # Rain probability bars
    bar_w = max(chart_w // 24 - 2, 4)
    for i in range(24):
        if rain[i] > 0:
            x = chart_x + int((i / 23) * chart_w) - bar_w // 2
            bar_h = int((rain[i] / 100) * chart_h)
            y = chart_bottom - bar_h
            # Graduated shading based on probability
            if rain[i] > 60:
                color = LIGHT
            elif rain[i] > 30:
                color = FAINT
            else:
                # Very light for low probability
                color = (218, 215, 206)
            pygame.draw.rect(screen, color, (x, y, bar_w, bar_h))
            if rain[i] > 40:
                for hy in range(y, chart_bottom, 4):
                    pygame.draw.line(screen, MID, (x, hy), (x + bar_w, hy), 1)

    # Rain % Y-axis (right)
    for p in range(0, 101, 25):
        y = int(chart_bottom - (p / 100) * chart_h)
        f_sm.render_to(screen, (chart_x + chart_w + 8, y - 5), f"{p}%", LIGHT)

    # Temperature line - Outdoor (solid, smooth)
    points_out = []
    for i in range(24):
        x = chart_x + int((i / 23) * chart_w)
        y = int(chart_bottom - ((temps[i] - temp_min_chart) / temp_range) * chart_h)
        points_out.append((x, y))
    if len(points_out) > 1:
        pygame.draw.lines(screen, BLACK, False, points_out, 3)

    # "Now" marker — at left edge of chart
    now_x = chart_x + 2
    for dy in range(chart_y + 4, chart_bottom, 4):
        pygame.draw.line(screen, MID, (now_x, dy), (now_x, min(dy + 2, chart_bottom)), 1)
    f_sm.render_to(screen, (now_x + 4, chart_y + 2), "JETZT", BLACK)
    if points_out:
        pygame.draw.circle(screen, BLACK, points_out[0], 5)
        pygame.draw.circle(screen, BG, points_out[0], 2)

    # Legend
    leg_y = chart_bottom + 30
    pygame.draw.line(screen, BLACK, (chart_x, leg_y), (chart_x + 24, leg_y), 3)
    f_sm.render_to(screen, (chart_x + 30, leg_y - 5), "Temperatur", DARK)
    pygame.draw.rect(screen, FAINT, (chart_x + 120, leg_y - 6, 16, 12))
    f_sm.render_to(screen, (chart_x + 142, leg_y - 5), "Regen %", DARK)

    # ====== RIGHT PANEL - BUS DEPARTURES ======
    bus_x = 570
    bus_y = 72
    bus_w = 370

    # Vertical divider
    pygame.draw.line(screen, LIGHT, (bus_x - 20, 66), (bus_x - 20, SCREEN_H - 30), 2)

    # Bus header
    pygame.draw.rect(screen, BLACK, (bus_x, bus_y, bus_w, 34))
    f_md.render_to(screen, (bus_x + 14, bus_y + 8), "ABFAHRTEN", BG)
    stop_name = data.get("bus_stop_name", "")
    sw, _ = f_sm.get_rect(stop_name).size
    f_sm.render_to(screen, (bus_x + bus_w - sw - 12, bus_y + 12), stop_name, BG)

    # Bus entries
    departures = data.get("bus_departures", [])
    entry_h = 52
    # Max destination width: bus_w - badge(66) - time(90) - padding
    max_dest_w = bus_w - 66 - 90 - 10

    for i, dep in enumerate(departures):
        ey = bus_y + 44 + i * entry_h

        if i % 2 == 0:
            pygame.draw.rect(screen, FAINT, (bus_x, ey, bus_w, entry_h - 2))

        # Line badge
        badge_w, badge_h = 42, 28
        badge_x, badge_y = bus_x + 12, ey + 10
        pygame.draw.rect(screen, BLACK, (badge_x, badge_y, badge_w, badge_h))
        lw, _ = f_md.get_rect(dep["line"]).size
        f_md.render_to(screen, (badge_x + (badge_w - lw) // 2, badge_y + 5), dep["line"], BG)

        # Destination (truncated if too long)
        dest = dep["dest"]
        dest_rect = f_md.get_rect(dest)
        while dest_rect.width > max_dest_w and len(dest) > 3:
            dest = dest[:-1]
            dest_rect = f_md.get_rect(dest + "…")
        if dest != dep["dest"]:
            dest += "…"
        f_md.render_to(screen, (bus_x + 66, ey + 10), dest, BLACK)

        # Platform
        plat = dep.get("platform", "")
        if plat:
            f_sm.render_to(screen, (bus_x + 66, ey + 32), f"Steig {plat}", MID)

        # Departure time — right-aligned
        time_str = dep["time"]
        tw, _ = f_lg.get_rect(time_str).size
        f_lg.render_to(screen, (bus_x + bus_w - tw - 14, ey + 12), time_str, BLACK)

    # Footer below bus
    foot_y = bus_y + 44 + len(departures) * entry_h + 16
    ts_parts = data.get("timestamp", ", --:--").split(", ")
    f_sm.render_to(screen, (bus_x + 12, foot_y), f"Abfahrten ab Stand {ts_parts[-1]}", MID)

    # ====== BOTTOM STATUS BAR ======
    pygame.draw.rect(screen, FAINT, (0, SCREEN_H - 28, SCREEN_W, 28))

    weather_ok = "OK" if data.get("weather_api_ok") else "ERR"
    bus_ok = "OK" if data.get("bus_api_ok") else "ERR"
    status = f"WiFi OK  ·  Wetter-API {weather_ok}  ·  ZVV-API {bus_ok}"
    f_sm.render_to(screen, (20, SCREEN_H - 20), status, MID)

    next_upd = f"Nächstes Update ~{data['next_update']}"
    nw, _ = f_sm.get_rect(next_upd).size
    f_sm.render_to(screen, (SCREEN_W - nw - 20, SCREEN_H - 20), next_upd, MID)

    # Border
    pygame.draw.rect(screen, (136, 136, 136), (0, 0, SCREEN_W, SCREEN_H), 2)


def main():
    pygame.init()
    screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))
    pygame.display.set_caption("M5PaperS3 Dashboard Simulator")

    # Fonts — Helvetica Neue for a clean, modern look
    fonts = {
        "sm": pygame.freetype.SysFont("Helvetica Neue", 13),
        "md": pygame.freetype.SysFont("Helvetica Neue", 15, bold=True),
        "lg": pygame.freetype.SysFont("Helvetica Neue", 22, bold=True),
        "xl": pygame.freetype.SysFont("Helvetica Neue", 64, bold=True),
        "title": pygame.freetype.SysFont("Helvetica Neue", 22, bold=True),
    }

    icons = load_weather_icons()
    data = fetch_data()
    if not data:
        print("Failed to fetch data. Is the backend running on port 8090?")
        sys.exit(1)

    draw_dashboard(screen, data, fonts, icons)
    pygame.display.flip()
    pygame.image.save(screen, os.path.join(os.path.dirname(__file__), "dashboard_screenshot.png"))
    print("Screenshot saved to simulator/dashboard_screenshot.png")

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_r:
                    data = fetch_data()
                    if data:
                        draw_dashboard(screen, data, fonts, icons)
                        pygame.display.flip()
                        pygame.image.save(screen, os.path.join(os.path.dirname(__file__), "dashboard_screenshot.png"))
                        print("Refreshed")
                if event.key in (pygame.K_q, pygame.K_ESCAPE):
                    running = False

    pygame.quit()


if __name__ == "__main__":
    main()
