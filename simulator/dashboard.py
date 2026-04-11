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
    """Load weather icon PNGs (kept at original size, scaled per use)."""
    icons = {}
    for name in ("sunny", "partly_cloudy", "cloudy", "rainy", "snowy"):
        path = os.path.join(ICONS_DIR, f"{name}.png")
        if os.path.exists(path):
            img = pygame.image.load(path).convert_alpha()
            icons[name] = img
    return icons


def draw_dashboard(screen: pygame.Surface, data: dict, fonts: dict, icons: dict):
    screen.fill(BG)
    f_sm = fonts["sm"]
    f_md = fonts["md"]
    f_lg = fonts["lg"]
    f_xl = fonts["xl"]
    f_xxl = fonts["xxl"]
    f_title = fonts["title"]
    f_stat = fonts["stat"]
    f_period = fonts["period"]

    left_w = 510

    # ====== HEADER ======
    pygame.draw.rect(screen, BLACK, (0, 0, SCREEN_W, 46))
    f_title.render_to(screen, (20, 10), "WETTER & ABFAHRTEN", BG)
    ts_text = f"Stand: {data['timestamp']}  ·  Nächstes Update ~{data['next_update']}"
    f_sm.render_to(screen, (280, 16), ts_text, BG)

    # ====== LEFT PANEL — WEATHER ======
    y = 54

    # -- Row 1: Actual temp (left) + 3 period icons (right) --
    f_sm.render_to(screen, (24, y + 2), "AKTUELL", MID)
    to = data.get("temp_outdoor")
    f_xxl.render_to(screen, (20, y + 18), f"{to:.1f}°" if to is not None else "--°", BLACK)

    # Three period weather icons
    icon_size = 80
    icon_start_x = 210
    col_w = 100
    periods = data.get("weather_periods", [])

    for i, p in enumerate(periods[:3]):
        cx = icon_start_x + i * col_w
        box_w = 90

        pygame.draw.rect(screen, FAINT, (cx, y, box_w, icon_size + 24), border_radius=5)
        pygame.draw.rect(screen, LIGHT, (cx, y, box_w, icon_size + 24), width=1, border_radius=5)

        icon_surf = icons.get(p.get("condition", "partly_cloudy")) or icons.get("partly_cloudy")
        if icon_surf:
            scaled = pygame.transform.smoothscale(icon_surf, (icon_size, icon_size))
            screen.blit(scaled, (cx + (box_w - icon_size) // 2, y + 2))

        lbl = p.get("label", "")
        lbl_rect = f_period.get_rect(lbl)
        f_period.render_to(screen, (cx + (box_w - lbl_rect.width) // 2, y + icon_size + 6), lbl, DARK)

    # Vertical separators between icon boxes
    for i in range(1, 3):
        sx = icon_start_x + i * col_w - 5
        pygame.draw.line(screen, LIGHT, (sx, y + 10), (sx, y + icon_size + 14), 1)

    y += icon_size + 32

    # -- Row 2: Stats — max/min + rain/wind/gusts/precip --
    pygame.draw.line(screen, LIGHT, (20, y), (left_w - 10, y), 1)
    y += 8

    # Left: max / min
    tmax = data.get("temp_max")
    tmin = data.get("temp_min")
    f_stat.render_to(screen, (24, y + 4), "max", MID)
    f_xl.render_to(screen, (64, y - 4), f"{tmax:.0f}°" if tmax is not None else "--°", BLACK)
    f_stat.render_to(screen, (24, y + 46), "min", MID)
    f_xl.render_to(screen, (64, y + 38), f"{tmin:.0f}°" if tmin is not None else "--°", BLACK)

    # Right: rain %, wind, gusts, precip
    rc_x = 240
    row_h = 22

    rain_pct = data.get("rain_probability_pct", 0)
    wind = data.get("wind_kmh")
    gust = data.get("wind_gust_kmh")
    precip = data.get("precip_total_mm", 0)

    f_lg.render_to(screen, (rc_x, y), f"Regen  {rain_pct}%", BLACK)
    f_stat.render_to(screen, (rc_x, y + row_h + 6), f"↗ Wind  {wind} km/h" if wind else "↗ Wind  --", DARK)
    f_stat.render_to(screen, (rc_x, y + (row_h + 6) * 2), f"⇉ Böen  {gust} km/h" if gust else "⇉ Böen  --", DARK)
    f_stat.render_to(screen, (rc_x, y + (row_h + 6) * 3), f"☂ Niederschlag  {precip:.1f} mm", DARK)

    y += 100

    # -- Row 3: 24h Temperature & Rain chart --
    pygame.draw.line(screen, LIGHT, (20, y), (left_w - 10, y), 1)
    y += 6

    f_md.render_to(screen, (20, y), "TEMPERATUR & REGEN  —  24h", BLACK)
    y += 20

    chart_x = 50
    chart_w = left_w - 80
    chart_h = 155
    chart_bottom = y + chart_h

    temps = data.get("temp_outdoor_24h", [0] * 24)
    rain = data.get("rain_mm_24h", [0] * 24)
    hour_labels = data.get("hour_labels", list(range(24)))

    # Y-axis range
    valid_temps = [t for t in temps if t != 0]
    if valid_temps:
        temp_min_chart = min(min(valid_temps) - 2, 0)
        temp_max_chart = max(valid_temps) + 3
    else:
        temp_min_chart, temp_max_chart = -5, 25
    temp_range = max(temp_max_chart - temp_min_chart, 1)

    # Y-axis labels + grid
    for t in range(int(temp_min_chart), int(temp_max_chart) + 1, 5):
        cy = int(chart_bottom - ((t - temp_min_chart) / temp_range) * chart_h)
        f_sm.render_to(screen, (chart_x - 30, cy - 5), f"{t}°", MID)
        pygame.draw.line(screen, FAINT, (chart_x, cy), (chart_x + chart_w, cy), 1)

    # X-axis labels
    for i in range(0, 24, 3):
        x = chart_x + int((i / 23) * chart_w)
        f_sm.render_to(screen, (x - 8, chart_bottom + 4), f"{hour_labels[i]:02d}", MID)

    # Rain bars (mm-based, graduated shading)
    bar_w = max(chart_w // 24 - 2, 4)
    rain_scale = max(max(rain) if rain else 1, 2)  # scale to max rain or 2mm
    for i in range(24):
        if rain[i] > 0.05:
            x = chart_x + int((i / 23) * chart_w) - bar_w // 2
            bar_h = int((rain[i] / rain_scale) * chart_h)
            if bar_h < 2:
                bar_h = 2
            by = chart_bottom - bar_h
            if rain[i] > 5.0:
                color = LIGHT
            elif rain[i] > 1.0:
                color = FAINT
            else:
                color = (218, 215, 206)
            pygame.draw.rect(screen, color, (x, by, bar_w, bar_h))
            if rain[i] > 2.0:
                for hy in range(by, chart_bottom, 4):
                    pygame.draw.line(screen, MID, (x, hy), (x + bar_w, hy), 1)

    # Rain mm Y-axis (right)
    for mm in range(0, int(rain_scale) + 1, max(1, int(rain_scale) // 4)):
        ry = int(chart_bottom - (mm / rain_scale) * chart_h)
        f_sm.render_to(screen, (chart_x + chart_w + 6, ry - 5), f"{mm}mm", LIGHT)

    # Temperature line
    points = []
    for i in range(24):
        px = chart_x + int((i / 23) * chart_w)
        py = int(chart_bottom - ((temps[i] - temp_min_chart) / temp_range) * chart_h)
        points.append((px, py))
    if len(points) > 1:
        pygame.draw.lines(screen, BLACK, False, points, 3)

    # "Now" marker
    now_x = chart_x + 2
    for dy in range(y, chart_bottom, 4):
        pygame.draw.line(screen, MID, (now_x, dy), (now_x, min(dy + 2, chart_bottom)), 1)
    f_sm.render_to(screen, (now_x + 4, y), "JETZT", BLACK)
    if points:
        pygame.draw.circle(screen, BLACK, points[0], 5)
        pygame.draw.circle(screen, BG, points[0], 2)

    # Legend
    leg_y = chart_bottom + 18
    pygame.draw.line(screen, BLACK, (chart_x, leg_y), (chart_x + 24, leg_y), 3)
    f_sm.render_to(screen, (chart_x + 30, leg_y - 5), "Temperatur", DARK)
    pygame.draw.rect(screen, FAINT, (chart_x + 120, leg_y - 6, 16, 12))
    f_sm.render_to(screen, (chart_x + 142, leg_y - 5), "Regen mm", DARK)

    # ====== VERTICAL DIVIDER ======
    div_x = left_w + 10
    pygame.draw.line(screen, LIGHT, (div_x, 56), (div_x, SCREEN_H - 30), 2)

    # ====== RIGHT PANEL — BUS DEPARTURES ======
    bus_x = div_x + 20
    bus_y = 56
    bus_w = SCREEN_W - bus_x - 10

    # Bus header
    pygame.draw.rect(screen, BLACK, (bus_x, bus_y, bus_w, 34))
    f_md.render_to(screen, (bus_x + 14, bus_y + 8), "ABFAHRTEN", BG)
    stop_name = data.get("bus_stop_name", "")
    sw, _ = f_sm.get_rect(stop_name).size
    f_sm.render_to(screen, (bus_x + bus_w - sw - 12, bus_y + 12), stop_name, BG)

    # Bus entries
    departures = data.get("bus_departures", [])
    entry_h = 52
    max_dest_w = bus_w - 66 - 90 - 10

    for i, dep in enumerate(departures):
        ey = bus_y + 44 + i * entry_h
        if ey + entry_h > SCREEN_H - 40:
            break

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

    fonts = {
        "sm": pygame.freetype.SysFont("Helvetica Neue", 13),
        "md": pygame.freetype.SysFont("Helvetica Neue", 15, bold=True),
        "lg": pygame.freetype.SysFont("Helvetica Neue", 22, bold=True),
        "xl": pygame.freetype.SysFont("Helvetica Neue", 48, bold=True),
        "xxl": pygame.freetype.SysFont("Helvetica Neue", 64, bold=True),
        "title": pygame.freetype.SysFont("Helvetica Neue", 22, bold=True),
        "stat": pygame.freetype.SysFont("Helvetica Neue", 18),
        "period": pygame.freetype.SysFont("Helvetica Neue", 14),
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
