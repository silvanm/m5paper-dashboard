/**
 * M5PaperS3 Home Dashboard
 *
 * Wakes from deep sleep, connects to WiFi, fetches dashboard JSON from backend,
 * renders the full dashboard on the e-ink display, then goes back to deep sleep.
 */

#include <cstdio>
#include <cstring>
#include <cmath>
#include <M5Unified.h>
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_sleep.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "cJSON.h"
#include "weather_icons_2bit.h"
#include "clothing_sprites.h"

// ---- Configuration ----
// WiFi and API credentials — create secrets.h from secrets.h.example
#include "secrets.h"
#define SLEEP_MINUTES   15
#define MAX_HTTP_BUF    16384

// ---- Colors (M5GFX uses 16-bit or named colors, we use grayscale values) ----
#define C_BG        0xEEEEEE  // warm white
#define C_BLACK     0x1A1A1A
#define C_DARK      0x3A3A3A
#define C_MID       0x7A7A7A
#define C_LIGHT     0xB0B0A8
#define C_FAINT     0xD0CDC4

// ---- Display dimensions ----
#define SCREEN_W    960
#define SCREEN_H    540

// ---- WiFi ----
static EventGroupHandle_t s_wifi_events;
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1
static int s_retry_count = 0;

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t id, void *data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retry_count < 10) { esp_wifi_connect(); s_retry_count++; }
        else xEventGroupSetBits(s_wifi_events, WIFI_FAIL_BIT);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        s_retry_count = 0;
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
    }
}

static bool wifi_connect() {
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase(); nvs_flash_init();
    }
    esp_netif_init();
    esp_event_loop_create_default();
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&cfg);
    s_wifi_events = xEventGroupCreate();

    esp_event_handler_instance_t h1, h2;
    esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, &h1);
    esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, &h2);

    wifi_config_t wc = {};
    strncpy((char*)wc.sta.ssid, WIFI_SSID, sizeof(wc.sta.ssid));
    strncpy((char*)wc.sta.password, WIFI_PASS, sizeof(wc.sta.password));
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_set_config(WIFI_IF_STA, &wc);
    esp_wifi_start();

    EventBits_t bits = xEventGroupWaitBits(s_wifi_events,
        WIFI_CONNECTED_BIT | WIFI_FAIL_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(15000));
    return (bits & WIFI_CONNECTED_BIT) != 0;
}

// ---- HTTP fetch ----
static char *http_buf = nullptr;
static int http_buf_len = 0;

static esp_err_t http_event_handler(esp_http_client_event_t *evt) {
    if (evt->event_id == HTTP_EVENT_ON_DATA) {
        if (http_buf_len + evt->data_len < MAX_HTTP_BUF) {
            memcpy(http_buf + http_buf_len, evt->data, evt->data_len);
            http_buf_len += evt->data_len;
        }
    }
    return ESP_OK;
}

static cJSON *fetch_dashboard() {
    http_buf = (char*)malloc(MAX_HTTP_BUF);
    if (!http_buf) return nullptr;
    http_buf_len = 0;
    memset(http_buf, 0, MAX_HTTP_BUF);

    esp_http_client_config_t config = {};
    config.url = API_URL;
    config.event_handler = http_event_handler;
    config.timeout_ms = 10000;
    config.crt_bundle_attach = esp_crt_bundle_attach;

    esp_http_client_handle_t client = esp_http_client_init(&config);
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);

    if (err != ESP_OK || status != 200) {
        printf("HTTP error: %d status=%d\n", err, status);
        free(http_buf); http_buf = nullptr;
        return nullptr;
    }

    http_buf[http_buf_len] = '\0';
    cJSON *json = cJSON_Parse(http_buf);
    free(http_buf); http_buf = nullptr;
    return json;
}

// ---- UTF-8 to ASCII helper (for GFX fonts that only support ASCII) ----
static void sanitize_utf8(char *out, const char *in, int max_len) {
    int di = 0;
    for (int si = 0; in[si] && di < max_len - 1; si++) {
        unsigned char c = (unsigned char)in[si];
        if (c < 0x80) {
            out[di++] = c;
        } else if (c == 0xC3 && in[si + 1]) {
            si++;
            unsigned char c2 = (unsigned char)in[si];
            switch (c2) {
                case 0xBC: out[di++] = 'u'; break;  // ü
                case 0xB6: out[di++] = 'o'; break;  // ö
                case 0xA4: out[di++] = 'a'; break;  // ä
                case 0x9C: out[di++] = 'U'; break;  // Ü
                case 0x96: out[di++] = 'O'; break;  // Ö
                case 0x84: out[di++] = 'A'; break;  // Ä
                case 0x9F: out[di++] = 's'; out[di++] = 's'; break;  // ß
                default: out[di++] = '?'; break;
            }
        } else if (c >= 0xC0 && c < 0xE0) {
            si++;  // skip 2-byte
        } else if (c >= 0xE0 && c < 0xF0) {
            si += 2;  // skip 3-byte
        } else if (c >= 0xF0) {
            si += 3;  // skip 4-byte
        }
    }
    out[di] = '\0';
}

// ---- Drawing helpers ----
// Draw a string then a small degree circle after it
static void drawTemp(int x, int y, const char *num_str, uint32_t color, int circle_r = 3) {
    auto &d = M5.Display;
    d.setTextColor(color);
    int w = d.drawString(num_str, x, y);
    // Draw degree circle after the text
    int cx = x + w + circle_r + 2;
    int cy = y + circle_r + 2;
    d.drawCircle(cx, cy, circle_r, color);
}

static inline uint32_t gray(int v) {
    return M5.Display.color888(v, v, v);
}

static void drawDashedLineH(int x1, int x2, int y, uint32_t color, int dash=4, int gap=4) {
    for (int x = x1; x < x2; x += dash + gap) {
        M5.Display.drawLine(x, y, std::min(x + dash, x2), y, color);
    }
}

static void drawDashedLineV(int x, int y1, int y2, uint32_t color, int dash=4, int gap=4) {
    for (int y = y1; y < y2; y += dash + gap) {
        M5.Display.drawLine(x, y, x, std::min(y + dash, y2), color);
    }
}

static void drawBitmap1bit(int x, int y, int w, int h, const uint8_t *data, uint8_t fg_gray, uint8_t bg_gray) {
    uint16_t fg16 = M5.Display.color565(fg_gray, fg_gray, fg_gray);
    uint16_t bg16 = M5.Display.color565(bg_gray, bg_gray, bg_gray);
    uint16_t row_buf[WEATHER_ICON_W];  // max icon width
    int bytes_per_row = (w + 7) / 8;
    for (int row = 0; row < h; row++) {
        for (int col = 0; col < w; col++) {
            int byte_idx = row * bytes_per_row + col / 8;
            int bit_idx = 7 - (col % 8);
            row_buf[col] = (data[byte_idx] & (1 << bit_idx)) ? fg16 : bg16;
        }
        M5.Display.pushImage(x, y + row, w, 1, row_buf);
    }
}

// Draw a 2-bit grayscale sprite (4 levels, 4 pixels per byte, MSB first)
// Uses row-buffer pushImage for speed instead of per-pixel drawPixel
static void drawSprite2bit(int x, int y, int w, int h, const uint8_t *data, uint16_t bg_color) {
    // Grayscale LUT: 0=black, 1=dark, 2=light (computed at first call)
    static bool lut_init = false;
    static uint16_t lut[3];
    if (!lut_init) {
        lut[0] = M5.Display.color565(26, 26, 26);
        lut[1] = M5.Display.color565(90, 90, 90);
        lut[2] = M5.Display.color565(176, 176, 176);
        lut_init = true;
    }
    uint16_t row_buf[SPRITE_W];

    for (int row = 0; row < h; row++) {
        for (int col = 0; col < w; col++) {
            int pixel_idx = row * w + col;
            int byte_idx = pixel_idx / 4;
            int shift = 6 - (pixel_idx % 4) * 2;
            uint8_t level = (data[byte_idx] >> shift) & 0x03;
            row_buf[col] = (level < 3) ? lut[level] : bg_color;
        }
        M5.Display.pushImage(x, y + row, w, 1, row_buf);
    }
}

// Lookup sprite data by name from JSON
static const uint8_t* lookup_sprite(const char *name) {
    struct { const char *name; const uint8_t *data; } map[] = {
        {"cap", sprite_cap}, {"nocap", sprite_nocap},
        {"shortsleeves", sprite_shortsleeves}, {"longsleeves", sprite_longsleeves},
        {"raincoat", sprite_raincoat},
        {"shorts", sprite_shorts}, {"pants", sprite_pants},
        {"rainpants", sprite_rainpants},
        {"gloves", sprite_gloves}, {"mittens", sprite_mittens},
        {"nogloves", sprite_nogloves},
        {"sneakers", sprite_sneakers}, {"wintershoes", sprite_wintershoes},
    };
    for (auto &e : map) {
        if (strcmp(name, e.name) == 0) return e.data;
    }
    return nullptr;
}

// ---- Dashboard rendering ----
static void render_dashboard(cJSON *data) {
    auto &d = M5.Display;

    // Palette
    uint32_t bg    = gray(255);
    uint32_t black = gray(26);
    uint32_t dark  = gray(58);
    uint32_t mid   = gray(122);
    uint32_t light = gray(176);
    uint32_t faint = gray(208);

    d.fillScreen(bg);

    char buf[128];
    const char *ts = cJSON_GetObjectItem(data, "timestamp")->valuestring;
    const char *nu = cJSON_GetObjectItem(data, "next_update")->valuestring;

    // ====== HEADER ======
    d.fillRect(0, 0, SCREEN_W, 48, black);
    d.setTextColor(bg);
    d.setFont(&fonts::FreeSansBold12pt7b);
    d.setTextDatum(top_left);
    d.drawString("HOME DASHBOARD", 16, 16);

    d.setFont(&fonts::FreeSans9pt7b);
    snprintf(buf, sizeof(buf), "Stand: %s", ts);
    d.drawString(buf, 260, 8);
    snprintf(buf, sizeof(buf), "Nachstes Update ~%s", nu);
    d.drawString(buf, 260, 26);

    // Battery icon (top right)
    int bat_pct = M5.Power.getBatteryLevel();
    int bat_x = SCREEN_W - 70, bat_y = 14;
    d.drawRect(bat_x, bat_y, 40, 18, bg);
    d.fillRect(bat_x + 40, bat_y + 5, 4, 8, bg);
    int fill_w = (36 * bat_pct) / 100;
    d.fillRect(bat_x + 2, bat_y + 2, fill_w, 14, bg);
    snprintf(buf, sizeof(buf), "%d%%", bat_pct);
    d.setTextDatum(top_right);
    d.setFont(&fonts::FreeSans9pt7b);
    d.drawString(buf, bat_x - 4, bat_y);

    // ====== LEFT COLUMN (weather + clothing) ======
    int lx = 16, ly = 60;
    int col_split = 530;  // left column width

    // Weather icon (left)
    const char *cond = cJSON_GetObjectItem(data, "weather_condition")->valuestring;
    const uint8_t *icon_data = weather_partly_cloudy;
    if (strcmp(cond, "sunny") == 0) icon_data = weather_sunny;
    else if (strcmp(cond, "cloudy") == 0) icon_data = weather_cloudy;
    else if (strcmp(cond, "rainy") == 0) icon_data = weather_rainy;
    else if (strcmp(cond, "snowy") == 0) icon_data = weather_snowy;
    drawSprite2bit(lx, ly - 5, WEATHER_ICON_W, WEATHER_ICON_H, icon_data,
        M5.Display.color565(255, 255, 255));  // white bg

    // Temperature (right of icon)
    int tx = lx + WEATHER_ICON_W + 12;
    cJSON *to_j = cJSON_GetObjectItem(data, "temp_outdoor");
    d.setFont(&fonts::FreeSansBold24pt7b);
    d.setTextDatum(top_left);
    int temp_w = 0;
    if (to_j && !cJSON_IsNull(to_j)) {
        snprintf(buf, sizeof(buf), "%.0f", to_j->valuedouble);
        drawTemp(tx, ly, buf, black, 6);
        temp_w = d.textWidth(buf) + 20;
    } else {
        drawTemp(tx, ly, "--", black, 6);
        temp_w = d.textWidth("--") + 20;
    }

    // Wind (right of temperature)
    cJSON *wind_j = cJSON_GetObjectItem(data, "wind_kmh");
    cJSON *wdir_j = cJSON_GetObjectItem(data, "wind_dir");
    d.setFont(&fonts::FreeSansBold9pt7b);
    d.setTextColor(dark);
    d.setTextDatum(top_left);
    snprintf(buf, sizeof(buf), "%d km/h %s",
        wind_j ? wind_j->valueint : 0,
        (wdir_j && !cJSON_IsNull(wdir_j)) ? wdir_j->valuestring : "");
    d.drawString(buf, tx + temp_w, ly + 20);

    // Min/Max + Rain (below temperature)
    cJSON *tmin_j = cJSON_GetObjectItem(data, "temp_min");
    cJSON *tmax_j = cJSON_GetObjectItem(data, "temp_max");
    float rain_max_mm = cJSON_GetObjectItem(data, "rain_max_mm")->valuedouble;

    d.setFont(&fonts::FreeSans9pt7b);
    d.setTextColor(mid);
    d.setTextDatum(top_left);
    snprintf(buf, sizeof(buf), "%.0f / %.0f   Regen bis %.1f mm",
        tmin_j ? tmin_j->valuedouble : 0, tmax_j ? tmax_j->valuedouble : 0, rain_max_mm);
    d.drawString(buf, tx, ly + 52);

    // ====== CLOTHING SECTION ======
    int cloth_y = ly + 85;

    // 3x2 grid of clothing items
    cJSON *clothing = cJSON_GetObjectItem(data, "clothing");
    int grid_cols = 3, grid_rows = 2;
    int cell_w = 165, cell_h = 180;

    for (int i = 0; i < cJSON_GetArraySize(clothing) && i < 6; i++) {
        cJSON *item = cJSON_GetArrayItem(clothing, i);
        const char *category = cJSON_GetObjectItem(item, "category")->valuestring;
        const char *sprite_name = cJSON_GetObjectItem(item, "sprite")->valuestring;
        const char *label = cJSON_GetObjectItem(item, "label")->valuestring;

        int col = i % grid_cols;
        int row = i / grid_cols;
        int cx = lx + col * cell_w;
        int cy = cloth_y + row * cell_h;

        (void)category;  // category label not displayed

        // Sprite background box
        int box_x = cx + (cell_w - SPRITE_W) / 2;
        int box_y = cy + 20;
        d.fillRect(box_x - 4, box_y - 4, SPRITE_W + 8, SPRITE_H + 8, faint);

        // Draw sprite
        const uint8_t *spr = lookup_sprite(sprite_name);
        if (spr) {
            drawSprite2bit(box_x, box_y, SPRITE_W, SPRITE_H, spr,
                M5.Display.color565(255, 255, 255));  // white bg
        }

        (void)label;  // label not displayed
    }

    // ====== RIGHT COLUMN ======
    int rx = col_split + 10;
    int rw = SCREEN_W - rx - 10;

    // ====== CHART ======
    int chart_x = rx + 30, chart_y = ly + 20;
    int chart_w = rw - 80, chart_h = 160;
    int chart_b = chart_y + chart_h;

    d.setTextColor(black);
    d.setFont(&fonts::FreeSansBold9pt7b);
    d.setTextDatum(top_left);
    
    cJSON *temps_arr = cJSON_GetObjectItem(data, "temp_outdoor_24h");
    cJSON *rain_arr = cJSON_GetObjectItem(data, "rain_mm_24h");
    cJSON *hours_arr = cJSON_GetObjectItem(data, "hour_labels");

    float temps[24] = {};
    float rain[24] = {};
    int hours[24] = {};
    float tmin_c = 999, tmax_c = -999;

    for (int i = 0; i < 24 && i < cJSON_GetArraySize(temps_arr); i++) {
        temps[i] = (float)cJSON_GetArrayItem(temps_arr, i)->valuedouble;
        if (temps[i] < tmin_c) tmin_c = temps[i];
        if (temps[i] > tmax_c) tmax_c = temps[i];
    }
    for (int i = 0; i < 24 && i < cJSON_GetArraySize(rain_arr); i++)
        rain[i] = (float)cJSON_GetArrayItem(rain_arr, i)->valuedouble;
    for (int i = 0; i < 24 && i < cJSON_GetArraySize(hours_arr); i++)
        hours[i] = cJSON_GetArrayItem(hours_arr, i)->valueint;

    float c_min = fmin(tmin_c - 2, 0);
    float c_max = tmax_c + 3;
    float c_range = c_max - c_min;
    if (c_range < 1) c_range = 1;

    // Y-axis labels + grid
    d.setTextDatum(middle_right);
    for (int t = (int)c_min; t <= (int)c_max; t += 5) {
        int y = chart_b - (int)(((t - c_min) / c_range) * chart_h);
        snprintf(buf, sizeof(buf), "%d", t);
        d.drawString(buf, chart_x - 6, y);
        d.drawLine(chart_x, y, chart_x + chart_w, y, faint);
    }

    // Rain mm Y-axis (right) — scale 0-10mm
    float rain_scale = 10.0f;  // max mm for full chart height
    d.setTextColor(light);
    d.setTextDatum(middle_left);
    for (int mm = 0; mm <= 10; mm += 2) {
        int y = chart_b - (mm * chart_h) / (int)rain_scale;
        snprintf(buf, sizeof(buf), "%dmm", mm);
        d.drawString(buf, chart_x + chart_w + 6, y);
    }

    // X-axis labels
    d.setTextColor(mid);
    d.setTextDatum(top_center);
    for (int i = 0; i < 24; i += 2) {
        int x = chart_x + (i * chart_w) / 23;
        snprintf(buf, sizeof(buf), "%02d", hours[i]);
        d.drawString(buf, x, chart_b + 4);
    }

    // Vertical markers at midnight (00) and noon (12)
    for (int i = 0; i < 24; i++) {
        if (hours[i] == 0 || hours[i] == 12) {
            int x = chart_x + (i * chart_w) / 23;
            drawDashedLineV(x, chart_y, chart_b, light, 3, 3);
        }
    }

    // Rain bars
    int bar_w = chart_w / 24 - 1;
    if (bar_w < 3) bar_w = 3;
    for (int i = 0; i < 24; i++) {
        if (rain[i] > 0.05f) {
            int x = chart_x + (i * chart_w) / 23 - bar_w / 2;
            int bar_h = (int)(rain[i] / rain_scale * chart_h);
            if (bar_h > chart_h) bar_h = chart_h;
            if (bar_h < 2) bar_h = 2;
            int y = chart_b - bar_h;
            uint32_t color = rain[i] > 5.0f ? dark : (rain[i] > 1.0f ? mid : light);
            d.fillRect(x, y, bar_w, bar_h, color);
        }
    }

    // Temperature line (thick)
    int prev_x = -1, prev_y = -1;
    for (int i = 0; i < 24; i++) {
        int x = chart_x + (i * chart_w) / 23;
        int y = chart_b - (int)(((temps[i] - c_min) / c_range) * chart_h);
        if (prev_x >= 0) {
            d.drawLine(prev_x, prev_y, x, y, black);
            d.drawLine(prev_x, prev_y - 1, x, y - 1, black);
            d.drawLine(prev_x, prev_y + 1, x, y + 1, black);
        }
        prev_x = x; prev_y = y;
    }

    // Dot at first data point (now)
    int dot_y = chart_b - (int)(((temps[0] - c_min) / c_range) * chart_h);
    d.fillCircle(chart_x, dot_y, 5, black);
    d.fillCircle(chart_x, dot_y, 2, bg);

    // Legend
    int leg_y = chart_b + 30;
    d.drawLine(chart_x, leg_y, chart_x + 20, leg_y, black);
    d.drawLine(chart_x, leg_y - 1, chart_x + 20, leg_y - 1, black);
    d.setTextColor(dark);
    d.setTextDatum(top_left);
    d.setFont(&fonts::FreeSans9pt7b);
    d.drawString("Temp", chart_x + 26, leg_y - 7);
    d.fillRect(chart_x + 80, leg_y - 6, 14, 12, mid);
    d.drawString("Regen mm", chart_x + 100, leg_y - 7);

    // ====== BUS DEPARTURES ======
    int bus_y = chart_b + 44;
    int bus_w = rw;

    // Header bar
    d.fillRect(rx, bus_y, bus_w, 32, black);
    d.setTextColor(bg);
    d.setFont(&fonts::FreeSansBold9pt7b);
    d.setTextDatum(top_left);
    d.drawString("ABFAHRTEN", rx + 12, bus_y + 8);
    d.setFont(&fonts::FreeSans9pt7b);
    d.setTextDatum(top_right);
    char stop_name[64];
    sanitize_utf8(stop_name, cJSON_GetObjectItem(data, "bus_stop_name")->valuestring, sizeof(stop_name));
    d.drawString(stop_name, rx + bus_w - 10, bus_y + 10);

    // Entries
    cJSON *deps = cJSON_GetObjectItem(data, "bus_departures");
    int entry_h = 40;

    for (int i = 0; i < cJSON_GetArraySize(deps) && i < 5; i++) {
        cJSON *dep = cJSON_GetArrayItem(deps, i);
        int ey = bus_y + 34 + i * entry_h;

        // Alternating bg
        if (i % 2 == 0) d.fillRect(rx, ey, bus_w, entry_h - 2, faint);

        // Line badge
        int badge_x = rx + 8, badge_y = ey + 6, badge_w = 36, badge_h = 24;
        d.fillRect(badge_x, badge_y, badge_w, badge_h, black);
        d.setTextColor(bg);
        d.setFont(&fonts::FreeSansBold9pt7b);
        d.setTextDatum(middle_center);
        d.drawString(cJSON_GetObjectItem(dep, "line")->valuestring,
                     badge_x + badge_w / 2, badge_y + badge_h / 2);

        // Destination
        char dest[64];
        sanitize_utf8(dest, cJSON_GetObjectItem(dep, "dest")->valuestring, sizeof(dest));
        if (strlen(dest) > 20) { dest[20] = '\0'; strcat(dest, ".."); }
        d.setTextColor(black);
        d.setFont(&fonts::FreeSans9pt7b);
        d.setTextDatum(top_left);
        d.drawString(dest, rx + 52, ey + 10);

        // Time
        d.setFont(&fonts::FreeSansBold12pt7b);
        d.setTextDatum(top_right);
        d.drawString(cJSON_GetObjectItem(dep, "time")->valuestring, rx + bus_w - 10, ey + 8);
    }

    // ====== STATUS BAR ======
    d.fillRect(0, SCREEN_H - 26, SCREEN_W, 26, faint);
    d.setFont(&fonts::FreeSans9pt7b);
    d.setTextColor(mid);
    d.setTextDatum(top_left);

    // Status
    bool w_ok = cJSON_IsTrue(cJSON_GetObjectItem(data, "weather_api_ok"));
    bool b_ok = cJSON_IsTrue(cJSON_GetObjectItem(data, "bus_api_ok"));
    snprintf(buf, sizeof(buf), "WiFi OK  |  Wetter %s  |  ZVV %s",
        w_ok ? "OK" : "ERR", b_ok ? "OK" : "ERR");
    d.drawString(buf, 16, SCREEN_H - 22);

    d.setTextDatum(top_right);
    snprintf(buf, sizeof(buf), "Nachstes Update ~%s", nu);
    d.drawString(buf, SCREEN_W - 16, SCREEN_H - 22);
}

// ---- Main ----
extern "C" void app_main(void) {
    auto cfg = M5.config();
    M5.begin(cfg);
    M5.Display.setRotation(1);

    // Show connecting message
    M5.Display.fillScreen(TFT_WHITE);
    M5.Display.setTextColor(TFT_BLACK);
    M5.Display.setTextDatum(middle_center);
    M5.Display.setFont(&fonts::FreeSans18pt7b);
    M5.Display.drawString("Connecting...", SCREEN_W / 2, SCREEN_H / 2);

    if (!wifi_connect()) {
        M5.Display.fillScreen(TFT_WHITE);
        M5.Display.drawString("WiFi Failed!", SCREEN_W / 2, SCREEN_H / 2);
        printf("WiFi connection failed, sleeping...\n");
        esp_sleep_enable_timer_wakeup((uint64_t)SLEEP_MINUTES * 60 * 1000000ULL);
        esp_deep_sleep_start();
        return;
    }

    printf("WiFi connected, fetching dashboard...\n");
    cJSON *data = fetch_dashboard();

    if (!data) {
        M5.Display.fillScreen(TFT_WHITE);
        M5.Display.drawString("API Error!", SCREEN_W / 2, SCREEN_H / 2);
        printf("Failed to fetch dashboard, sleeping...\n");
        esp_sleep_enable_timer_wakeup((uint64_t)SLEEP_MINUTES * 60 * 1000000ULL);
        esp_deep_sleep_start();
        return;
    }

    printf("Rendering dashboard...\n");
    render_dashboard(data);
    cJSON_Delete(data);

    printf("Dashboard rendered. Waiting for display to finish...\n");

    // Wait for e-ink display to finish refreshing before sleeping
    vTaskDelay(pdMS_TO_TICKS(5000));

    // Disconnect WiFi to save power
    esp_wifi_stop();

    // Deep sleep for 15 minutes
    esp_sleep_enable_timer_wakeup((uint64_t)SLEEP_MINUTES * 60 * 1000000ULL);
    esp_deep_sleep_start();
}
