
# YCV MESH WX Station

Automated weather station with JSON reporting, HTTP API, and Meshcore integration.

---

## Overview

The YCV MESH WX Station captures outdoor weather conditions and publishes them as structured JSON reports. Snapshots are saved every 30 minutes (at **:27** and **:57 UTC**) and pushed automatically to GitHub. A live payload is always available via the HTTP `/report` endpoint.

- **Snapshot filename format:** `WX-REPORT-DD-MM-YY-HH-MM.json`
- **Latest report:** always available as `latest.json`
- **Retention:** up to 48 files (24 hours)

---

## JSON Report Structure

### `report_time`

| Field | Type | Example | Description |
|---|---|---|---|
| `iso` | string | `2026-04-25T09:27:00Z` | ISO 8601 UTC timestamp |
| `unix` | float | `1777070820.0` | Unix epoch (seconds, UTC) |
| `date_dmy` | string | `25-04-2026` | Date in DD-MM-YYYY format |
| `time_hm` | string | `09:27` | Time HH:MM (24-hour, UTC) |
| `version` | string | `YCV-WX-MAM-HOLZ-26-04-AA` | Report version identifier |

**Version format:** `YCV-WX-MAM-HOLZ-<DD>-<MM>-<XX>` where `<XX>` is a continuity suffix cycling AA → AB → ... → ZZ → AA.

---

### `outdoor`

| Field | Type | Example | Description |
|---|---|---|---|
| `temperature_c` | float | `17.7` | Outdoor air temperature (°C) |
| `humidity_pct` | integer | `36` | Relative outdoor humidity (0–100%) |
| `feels_like_c` | float | `17.1` | Apparent temperature (°C) |
| `heat_index_c` | float | `17.1` | Humidity-adjusted temperature (°C) |

---

### `wind`

Wind direction is derived from a **30-minute rolling history** of vane samples.

**Variability** is declared when the angular spread across all samples spans ≥ 3 of the 16 compass steps (≥ 60°) and no single direction accounts for ≥ 50% of samples.

| Field | Type | Example | Description |
|---|---|---|---|
| `direction_code` | string | `V NNE-NE` | 30-min dominant direction. One of 16 cardinal directions (N, NNE, NE, ENE, E, ESE, SE, SSE, S, SSW, SW, WSW, W, WNW, NW, NNW). Format: `V <FROM>-<TO>` if variable, `<DIR>` if constant. |
| `direction_full` | string | `VARIABLE North North-East AND North-East` | Human-readable dominant direction |
| `current_direction` | string | `CALM` | Human-readable current direction |
| `current_speed_kmh` | integer | `32` | Instantaneous wind speed (km/h) |
| `avg_speed_kmh` | float | `24.3` | Mean speed across the 30-minute window (km/h) |
| `gust_kmh` | integer | `46` | Peak gust recorded in the window (km/h) |
| `variable` | boolean | `true` | `true` if wind direction is variable |
| `var_from` | string | `NNE` | Start of variable range, or `null` |
| `var_to` | string | `NE` | End of variable range, or `null` |
| `analysis_window_min` | integer | `30` | Duration of the analysis window (min) |
| `sample_count` | integer | `87` | Number of wind samples collected in the window |

---

### `rain`

| Field | Type | Example | Description |
|---|---|---|---|
| `event_mm` | float | `0.0` | Rain in the current precipitation event (mm) |
| `daily_mm` | float | `0.0` | Rain since midnight (mm) |
| `total_mm` | float | `0.0` | ⚠️ **DEPRECATED** - Lifetime accumulated rain (mm) |
| `rate_mm_per_h` | float | `0.0` | Current rain rate (mm/h) |

---

### `atmosphere`

| Field | Type | Example | Description |
|---|---|---|---|
| `pressure_abs_hpa` | integer | `983` | Absolute barometric pressure (hPa) |
| `pressure_qnh_hpa` | float | `1015.5` | ICAO sea-level reduced pressure (hPa), accounting for temperature and elevation (270 m) |
| `uv_index` | integer | `8` | WHO scale UV index: 0–2 Low, 3–5 Moderate, 6–7 High, 8–10 Very High |
| `light_klux` | float | `77.3` | Solar illuminance (kilolux) |

---

### `battery_pct`

| Field | Type | Example | Description |
|---|---|---|---|
| `battery_pct` | integer | `100` | Weather station battery level (%) |

---

### `visibility`

| Field | Type | Example | Description |
|---|---|---|---|
| `visibility_m` | integer | `12000` | Meteorological visibility (m) |
| `visibility_source` | string | `Open-Meteo` | Source of visibility data |

---

## HTTP Endpoints

| Method | Endpoint | Response | Description |
|---|---|---|---|
| `GET` | `/report` | JSON | Current parsed weather payload (live) |
| `GET` | `/data` | JSON | Raw DPS keys |
| `GET` | `/snapshots` | JSON | List of saved snapshot filenames |
| `POST` | `/snapshot/now` | JSON | Force a snapshot save and git push |
| `GET` | `/` | HTML | Web dashboard |

---

## Data Stability

Field names are always consistent across reports. Missing or invalid data is represented as `null`; fields are never removed.

| Value | Meaning |
|---|---|
| `null` | Sensor unavailable or data invalid |
| `0` | Valid measured zero |

Weather information is generated regardless of network connectivity.

---

## Meshcore Integration

### Rate Limits

- Commands are rate-limited to **1 per 5 minutes**
- Administrators can bypass the rate limit but **not** the duty cycle limit
- Automated weather broadcasts are sent at the same time as the LARU weather broadcaster
- Messages exceeding **135 characters** are split into separate messages
- Messages are encoded as plain text; line breaks are preserved

### Channel

| Channel | Key |
|---|---|
| `#ycv-wx` | `4080ced5003b56202ad5704169f213fd` |

### Commands

| Command | Description | Example Response |
|---|---|---|
| *(automated)* | Automated WX broadcast | Full weather report with temp, humidity, wind, rain, QNH, UVI, light, visibility |
| `/WX` *(default)* | METAR-style weather | `WX 1237Z 21C FL21 H34 Q1017 WIND V NNE-NE 23km/h VIS 39.6km` |
| `/WX` *(basic)* | Simple weather | `WX 1237Z TEMP 21C HUM 34% WIND 23km/h Q1017` |
| `/WX` *(advanced)* | Advanced weather | `WX 1237Z T21/FL21 H34 Q1017 WIND V NNE-NE 23 km/h VIS 39.6km UVI8` |
| `/WX` *(RPT)* | Repeater-friendly | `1237Z T21C H34 W23 V40km` |
| `/WX ALL` | Full report *(admin only)* | Full automated WX report |
| `/WX WIND` | Current wind | `WIND 23 km/h V NNE-NE GUST 59 AVG 23` |
| `/WX VIS` | Current visibility | `VIS 39.6 km` |
| `/WX TEMP` | Current conditions | `TEMP 21C FL21 HUM34%` |
| `/WX BARO` | Atmospheric pressure | `QNH 1017.5 hPa, BARO: 983 hPa` |
| `/WX LIGHT` | UV index & light | `UVI 8 (VERY HIGH), LIGHT 82.06 klux` |
| `/WX RAIN` | Precipitation | `RAIN EVENT 0.0mm DAILY 0.0mm RATE 0.0mm/h` |
| `/WX MODE [mode]` | Set `/WX` response format | `WX format set to [MODE]` |

---

## Example JSON Report

```json
{
  "report_time": {
    "iso": "2026-04-26T12:02:39Z",
    "unix": 1777204959.28,
    "date_dmy": "26-04-2026",
    "time_hm": "12:02",
    "version": "YCV-WX-MAM-HOLZ-26-04-AA"
  },
  "outdoor": {
    "temperature_c": 20.3,
    "humidity_pct": 32,
    "feels_like_c": 20.5,
    "heat_index_c": 20.3
  },
  "wind": {
    "direction_code": "V SSW-ESE",
    "direction_full": "VARIABLE South South-West AND East South-East",
    "current_direction": "CALM",
    "current_speed_kmh": 0,
    "avg_speed_kmh": 30.1,
    "gust_kmh": 73,
    "variable": true,
    "var_from": "SSW",
    "var_to": "ESE",
    "analysis_window_min": 30,
    "sample_count": 28
  },
  "rain": {
    "event_mm": 0.0,
    "daily_mm": 0.0,
    "total_mm": 0.0,
    "rate_mm_per_h": 0.0
  },
  "atmosphere": {
    "pressure_abs_hpa": 987,
    "pressure_qnh_hpa": 1018.6,
    "uv_index": 9,
    "light_klux": 85.54
  },
  "battery_pct": 100,
  "visibility": {
    "visibility_m": 10200,
    "visibility_source": "Open-Meteo API"
  }
}
```
