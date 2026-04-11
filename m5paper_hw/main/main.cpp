/**
 * Wardrobe Display — M5PaperS3
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

static bool wifi_try_connect(const char *ssid, const char *pass) {
    s_retry_count = 0;
    xEventGroupClearBits(s_wifi_events, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT);

    wifi_config_t wc = {};
    strncpy((char*)wc.sta.ssid, ssid, sizeof(wc.sta.ssid));
    strncpy((char*)wc.sta.password, pass, sizeof(wc.sta.password));
    esp_wifi_set_config(WIFI_IF_STA, &wc);
    esp_wifi_connect();

    printf("Trying WiFi: %s\n", ssid);
    EventBits_t bits = xEventGroupWaitBits(s_wifi_events,
        WIFI_CONNECTED_BIT | WIFI_FAIL_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(10000));
    return (bits & WIFI_CONNECTED_BIT) != 0;
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

    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_start();

    // Try each configured network
    if (wifi_try_connect(WIFI_SSID, WIFI_PASS)) return true;
    if (wifi_try_connect(WIFI_SSID2, WIFI_PASS2)) return true;
    return false;
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
    uint16_t row_buf[WEATHER_ICON_W];

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

// Map weather condition string to icon data
static const uint8_t* condition_to_icon(const char *cond) {
    if (strcmp(cond, "sunny") == 0) return weather_sunny;
    if (strcmp(cond, "cloudy") == 0) return weather_cloudy;
    if (strcmp(cond, "rainy") == 0) return weather_rainy;
    if (strcmp(cond, "snowy") == 0) return weather_snowy;
    return weather_partly_cloudy;  // default
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
    d.fillRect(0, 0, SCREEN_W, 44, black);
    d.setTextColor(bg);
    d.setFont(&fonts::FreeSansBold12pt7b);
    d.setTextDatum(top_left);
    d.drawString("WETTER & ABFAHRTEN", 16, 12);

    int bat_y = 12;
    d.setFont(&fonts::FreeSans9pt7b);
    snprintf(buf, sizeof(buf), "%s  |  Update ~%s", ts, nu);
    d.setTextDatum(top_right);
    d.drawString(buf, SCREEN_W - 135, bat_y  );

    // Battery icon (top right)
    int bat_pct = M5.Power.getBatteryLevel();
    int bat_x = SCREEN_W - 70;
    d.drawRect(bat_x, bat_y, 40, 18, bg);
    d.fillRect(bat_x + 40, bat_y + 5, 4, 8, bg);
    int fill_w = (36 * bat_pct) / 100;
    d.fillRect(bat_x + 2, bat_y + 2, fill_w, 14, bg);
    snprintf(buf, sizeof(buf), "%d%%", bat_pct);
    d.setTextDatum(top_right);
    d.setFont(&fonts::FreeSans9pt7b);
    d.drawString(buf, bat_x - 4, bat_y);

    // ====== LEFT COLUMN (weather + chart) ======
    int lx = 16;
    int col_split = 510;
    int ly = 54;
    uint16_t white_bg = M5.Display.color565(255, 255, 255);

    // -- Row 1: Actual temp (left) + 3 period icons (right) --
    d.setFont(&fonts::FreeSans9pt7b);
    d.setTextColor(mid);
    d.setTextDatum(top_left);
    d.drawString("AKTUELL", lx + 8, ly + 2);

    cJSON *to_j = cJSON_GetObjectItem(data, "temp_outdoor");
    d.setFont(&fonts::FreeSansBold24pt7b);
    d.setTextSize(2);
    d.setTextColor(black);
    d.setTextDatum(top_left);
    if (to_j && !cJSON_IsNull(to_j)) {
        snprintf(buf, sizeof(buf), "%.0f", to_j->valuedouble);
        drawTemp(lx + 4, ly + 30, buf, black, 10);
    } else {
        drawTemp(lx + 4, ly + 30, "--", black, 10);
    }
    d.setTextSize(1);

    // 3 period weather icons
    cJSON *periods = cJSON_GetObjectItem(data, "weather_periods");
    int icon_start_x = 195;
    int icon_col_w = 106;
    int n_periods = periods ? cJSON_GetArraySize(periods) : 0;
    printf("weather_periods: %d entries\n", n_periods);

    for (int i = 0; i < n_periods && i < 3; i++) {
        cJSON *period = cJSON_GetArrayItem(periods, i);
        cJSON *pcond_j = cJSON_GetObjectItem(period, "condition");
        cJSON *plabel_j = cJSON_GetObjectItem(period, "label");
        if (!pcond_j || !plabel_j) continue;

        const char *pcond = pcond_j->valuestring;
        const char *plabel = plabel_j->valuestring;
        printf("  period %d: %s = %s\n", i, plabel, pcond);

        int cx = icon_start_x + i * icon_col_w;

        // Draw weather icon directly (no box)
        const uint8_t *icon = condition_to_icon(pcond);
        drawSprite2bit(cx, ly, WEATHER_ICON_W, WEATHER_ICON_H, icon, white_bg);

        // Period label — centered below icon
        char lbl[32];
        sanitize_utf8(lbl, plabel, sizeof(lbl));
        d.setFont(&fonts::FreeSans9pt7b);
        d.setTextColor(dark);
        d.setTextDatum(top_center);
        d.drawString(lbl, cx + WEATHER_ICON_W / 2, ly + WEATHER_ICON_H);
    }

    ly += WEATHER_ICON_H + 24;

    // -- Row 2: Stats — max/min + rain/wind/gusts/precip --
    d.drawLine(lx, ly, col_split - 10, ly, light);
    ly += 6;

    // Left: max / min temps
    cJSON *tmin_j = cJSON_GetObjectItem(data, "temp_min");
    cJSON *tmax_j = cJSON_GetObjectItem(data, "temp_max");

    d.setFont(&fonts::FreeSans9pt7b);
    d.setTextColor(mid);
    d.setTextDatum(top_left);
    d.drawString("max", lx + 8, ly + 26);
    d.setFont(&fonts::FreeSansBold18pt7b);
    d.setTextColor(black);
    if (tmax_j && !cJSON_IsNull(tmax_j)) {
        snprintf(buf, sizeof(buf), "%.0f", tmax_j->valuedouble);
        drawTemp(lx + 50, ly + 20, buf, black, 4);
    }

    d.setFont(&fonts::FreeSans9pt7b);
    d.setTextColor(mid);
    d.drawString("min", lx + 8, ly + 64);
    d.setFont(&fonts::FreeSansBold18pt7b);
    d.setTextColor(black);
    if (tmin_j && !cJSON_IsNull(tmin_j)) {
        snprintf(buf, sizeof(buf), "%.0f", tmin_j->valuedouble);
        drawTemp(lx + 50, ly + 58, buf, black, 4);
    }

    // Right: rain%, wind, gusts, precipitation
    int rc_x = 230;
    int row_h = 20;

    cJSON *rain_prob_j = cJSON_GetObjectItem(data, "rain_probability_pct");
    cJSON *wind_j = cJSON_GetObjectItem(data, "wind_kmh");
    cJSON *gust_j = cJSON_GetObjectItem(data, "wind_gust_kmh");
    cJSON *precip_j = cJSON_GetObjectItem(data, "precip_total_mm");

    d.setFont(&fonts::FreeSansBold12pt7b);
    d.setTextColor(black);
    d.setTextDatum(top_left);
    snprintf(buf, sizeof(buf), "Regen  %d%%", rain_prob_j ? rain_prob_j->valueint : 0);
    d.drawString(buf, rc_x, ly);

    d.setFont(&fonts::FreeSans9pt7b);
    d.setTextColor(dark);
    snprintf(buf, sizeof(buf), "Niederschlag  %.1f mm", precip_j ? precip_j->valuedouble : 0.0);
    d.drawString(buf, rc_x, ly + (row_h + 8) * 3);
    snprintf(buf, sizeof(buf), "Wind  %d km/h", wind_j ? wind_j->valueint : 0);
    d.drawString(buf, rc_x, ly + row_h + 8);
    snprintf(buf, sizeof(buf), "Boen  %d km/h", gust_j ? gust_j->valueint : 0);
    d.drawString(buf, rc_x, ly + (row_h + 8) * 2);
    
    ly += 106;

    // -- Row 3: 24h Temperature & Rain chart --
    d.drawLine(lx, ly, col_split - 10, ly, light);
    ly += 10;

    d.setTextColor(black);
    d.setFont(&fonts::FreeSansBold9pt7b);
    d.setTextDatum(top_left);
    d.drawString("TEMPERATUR & REGEN - 24h", lx + 4, ly);
    ly += 24;

    int chart_x = lx + 34, chart_w = col_split - lx - 100;
    int chart_h = 160, chart_b = ly + chart_h;

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
    d.setTextColor(mid);
    d.setTextDatum(middle_right);
    for (int t = (int)c_min; t <= (int)c_max; t += 5) {
        int y = chart_b - (int)(((t - c_min) / c_range) * chart_h);
        snprintf(buf, sizeof(buf), "%d", t);
        d.drawString(buf, chart_x - 4, y);
        d.drawLine(chart_x, y, chart_x + chart_w, y, faint);
    }

    // Rain mm Y-axis (right)
    float rain_scale = 10.0f;
    d.setTextColor(light);
    d.setTextDatum(middle_left);
    for (int mm = 0; mm <= 10; mm += 5) {
        int y = chart_b - (mm * chart_h) / (int)rain_scale;
        snprintf(buf, sizeof(buf), "%dmm", mm);
        d.drawString(buf, chart_x + chart_w + 4, y);
    }

    // X-axis labels
    d.setTextColor(mid);
    d.setTextDatum(top_center);
    for (int i = 0; i < 24; i += 4) {
        int x = chart_x + (i * chart_w) / 23;
        snprintf(buf, sizeof(buf), "%02d", hours[i]);
        d.drawString(buf, x, chart_b + 7);
    }

    // Vertical markers at midnight (00) and noon (12)
    for (int i = 0; i < 24; i++) {
        if (hours[i] == 0 || hours[i] == 12) {
            int x = chart_x + (i * chart_w) / 23;
            drawDashedLineV(x, ly, chart_b, light, 3, 3);
        }
    }

    // Rain bars
    int bar_w = chart_w / 24 - 1;
    if (bar_w < 3) bar_w = 3;
    for (int i = 0; i < 24; i++) {
        if (rain[i] > 0.05f) {
            int x = chart_x + (i * chart_w) / 23 - bar_w / 2;
            int bh = (int)(rain[i] / rain_scale * chart_h);
            if (bh > chart_h) bh = chart_h;
            if (bh < 2) bh = 2;
            int y = chart_b - bh;
            uint32_t color = rain[i] > 5.0f ? dark : (rain[i] > 1.0f ? mid : light);
            d.fillRect(x, y, bar_w, bh, color);
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
    d.fillCircle(chart_x, dot_y, 4, black);
    d.fillCircle(chart_x, dot_y, 1, bg);

    // ====== RIGHT COLUMN (bus departures) ======
    int rx = col_split + 10;
    int rw = SCREEN_W - rx - 10;

    // Vertical divider
    d.drawLine(col_split, 54, col_split, SCREEN_H - 28, light);

    // ====== BUS DEPARTURES ======
    int bus_y = 54;
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

    const char *refresh_mode = cJSON_GetObjectItem(data, "refresh_mode")->valuestring;

    if (strcmp(refresh_mode, "low") == 0) {
        // Compact mode: badge+dest top, times below with auto line wrap
        cJSON *compact = cJSON_GetObjectItem(data, "bus_departures_compact");
        int times_max_w = bus_w - 20;  // available width for times
        int ey = bus_y + 34;

        for (int i = 0; i < cJSON_GetArraySize(compact) && i < 8; i++) {
            cJSON *item = cJSON_GetArrayItem(compact, i);

            // Build times into two lines with word wrap
            cJSON *times_arr2 = cJSON_GetObjectItem(item, "times");
            char line1[128] = {};
            char line2[128] = {};
            d.setFont(&fonts::FreeSansBold12pt7b);

            for (int t = 0; t < cJSON_GetArraySize(times_arr2); t++) {
                const char *tval = cJSON_GetArrayItem(times_arr2, t)->valuestring;
                // Try adding to line1
                char test[256];
                snprintf(test, sizeof(test), "%s%s%s", line1, strlen(line1) ? "  " : "", tval);
                if (d.textWidth(test) <= times_max_w) {
                    strncpy(line1, test, sizeof(line1) - 1);
                } else {
                    // Overflow to line2
                    size_t len2 = strlen(line2);
                    if (len2 > 0 && len2 < sizeof(line2) - 8) strcat(line2, "  ");
                    strncat(line2, tval, sizeof(line2) - strlen(line2) - 1);
                }
            }

            // Calculate entry height: header(28) + line1(22) + line2(22 if needed) + pad(6)
            int entry_h = 28 + 27 + (strlen(line2) ? 27 : 0) + 6;

            // Don't draw past screen
            if (ey + entry_h > SCREEN_H - 30) break;

            if (i % 2 == 0) d.fillRect(rx, ey, bus_w, entry_h - 2, faint);

            // Line badge
            int badge_x = rx + 8, badge_y2 = ey + 4, badge_w2 = 36, badge_h2 = 24;
            d.fillRect(badge_x, badge_y2, badge_w2, badge_h2, black);
            d.setTextColor(bg);
            d.setFont(&fonts::FreeSansBold9pt7b);
            d.setTextDatum(middle_center);
            d.drawString(cJSON_GetObjectItem(item, "line")->valuestring,
                         badge_x + badge_w2 / 2, badge_y2 + badge_h2 / 2);

            // Destination
            char dest[48];
            sanitize_utf8(dest, cJSON_GetObjectItem(item, "dest")->valuestring, sizeof(dest));
            if (strlen(dest) > 20) { dest[20] = '\0'; strcat(dest, ".."); }
            d.setTextColor(black);
            d.setFont(&fonts::FreeSansBold9pt7b);
            d.setTextDatum(top_left);
            d.drawString(dest, rx + 52, ey + 8);

            // Times line 1
            d.setTextColor(dark);
            d.setFont(&fonts::FreeSansBold12pt7b);
            d.setTextDatum(top_left);
            d.drawString(line1, rx + 7, ey + 33);

            // Times line 2 (if overflow)
            if (strlen(line2)) {
                d.drawString(line2, rx + 7, ey + 55);
            }

            ey += entry_h;
        }
    } else {
        // Normal mode: one entry per departure
        cJSON *deps = cJSON_GetObjectItem(data, "bus_departures");
        int entry_h = 40;

        for (int i = 0; i < cJSON_GetArraySize(deps) && i < 10; i++) {
            cJSON *dep = cJSON_GetArrayItem(deps, i);
            int ey = bus_y + 34 + i * entry_h;

            if (i % 2 == 0) d.fillRect(rx, ey, bus_w, entry_h - 2, faint);

            // Line badge
            int badge_x = rx + 8, badge_y2 = ey + 6, badge_w2 = 36, badge_h2 = 24;
            d.fillRect(badge_x, badge_y2, badge_w2, badge_h2, black);
            d.setTextColor(bg);
            d.setFont(&fonts::FreeSansBold9pt7b);
            d.setTextDatum(middle_center);
            d.drawString(cJSON_GetObjectItem(dep, "line")->valuestring,
                         badge_x + badge_w2 / 2, badge_y2 + badge_h2 / 2);

            // Destination
            char dest[64];
            sanitize_utf8(dest, cJSON_GetObjectItem(dep, "dest")->valuestring, sizeof(dest));
            if (strlen(dest) > 30) { dest[30] = '\0'; strcat(dest, ".."); }
            d.setTextColor(black);
            d.setFont(&fonts::FreeSans9pt7b);
            d.setTextDatum(top_left);
            d.drawString(dest, rx + 52, ey + 10);

            // Time
            d.setFont(&fonts::FreeSansBold12pt7b);
            d.setTextDatum(top_right);
            d.drawString(cJSON_GetObjectItem(dep, "time")->valuestring, rx + bus_w - 10, ey + 8);
        }
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

    // Sleep duration determined by backend based on time-of-day/weekday schedule
    int sleep_minutes = cJSON_GetObjectItem(data, "sleep_minutes")->valueint;
    cJSON_Delete(data);

    printf("Dashboard rendered. Sleeping for %d minutes...\n", sleep_minutes);

    // Wait for e-ink display to finish refreshing before sleeping
    vTaskDelay(pdMS_TO_TICKS(5000));

    // Disconnect WiFi to save power
    esp_wifi_stop();

    // Deep sleep
    esp_sleep_enable_timer_wakeup((uint64_t)sleep_minutes * 60 * 1000000ULL);
    esp_deep_sleep_start();
}
