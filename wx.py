import json
import time
import base64
import threading
import os
import subprocess
from collections import deque, Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import tinytuya
import requests
import string

DEVICE_ID    = "bf84292b7ff63bbc2dnchs"
LOCAL_KEY    = "@*H!|H|3)Eq1Bg{d"
IP           = "192.168.178.77"
GITHUB_REPO  = "."
SNAPSHOT_DIR = "snapshots"
MAX_SNAPSHOTS = 48
WIND_WINDOW_MIN = 30
STATION_ELEVATION_M = 270   # Mamer, Luxembourg ASL
atis_counter = 0

# Open-Meteo station coordinates (Mamer, Luxembourg)
FORECAST_LAT = 49.63
FORECAST_LON = 6.02
FORECAST_PERIODS    = 6      # current hour + next 5
FORECAST_CACHE_TTL  = 900    # 15 min

os.makedirs(SNAPSHOT_DIR, exist_ok=True)

latest        = {}
all_seen_keys = {}
live_log      = []
last_snap_min = -1
MAX_LOG = 300

# Each entry: (timestamp_s, direction_code_str_or_None, speed_kmh_float_or_None)
# Speed is stored already converted (divided by 10) so consumers never re-divide.
wind_history: deque = deque()

DIR_ORDER = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
             "S","SSW","SW","WSW","W","WNW","NW","NNW"]
DIR_IDX   = {d: i for i, d in enumerate(DIR_ORDER)}
DIR_FULL  = {
    "N":"North","NNE":"North North-East","NE":"North-East",
    "ENE":"East North-East","E":"East","ESE":"East South-East",
    "SE":"South-East","SSE":"South South-East","S":"South",
    "SSW":"South South-West","SW":"South-West","WSW":"West South-West",
    "W":"West","WNW":"West North-West","NW":"North-West","NNW":"North North-West",
}

VALID_DIR_CHARS = set(ord(c) for c in "NSEW")

# WMO weather interpretation codes -> short summary string
# https://open-meteo.com/en/docs  (weathercode / WMO 4677 table)
WMO_SUMMARY = {
    0:  "CLEAR",
    1:  "MAINLY CLEAR", 2:  "PARTLY CLOUDY", 3:  "OVERCAST",
    45: "FOG",          48: "ICING FOG",
    51: "LIGHT DRIZZLE", 53: "DRIZZLE",       55: "HEAVY DRIZZLE",
    56: "FRZG DRIZZLE",  57: "HVY FRZG DRIZ",
    61: "LIGHT RAIN",   63: "RAIN",           65: "HEAVY RAIN",
    66: "FRZG RAIN",    67: "HVY FRZG RAIN",
    71: "LIGHT SNOW",   73: "SNOW",           75: "HEAVY SNOW",
    77: "SNOW GRAINS",
    80: "LIGHT SHOWERS", 81: "SHOWERS",       82: "HVY SHOWERS",
    85: "SNOW SHOWERS",  86: "HVY SNOW SHWRS",
    95: "THUNDERSTORM",
    96: "TSTM+HAIL",    99: "TSTM+HVY HAIL",
}

def wmo_summary(code) -> str:
    if code is None:
        return "UNKNOWN"
    try:
        return WMO_SUMMARY.get(int(code), f"WX{int(code)}")
    except (ValueError, TypeError):
        return "UNKNOWN"

def degrees_to_dir(deg) -> str:
    """Convert a bearing in degrees to a 16-point compass abbreviation."""
    try:
        deg = float(deg) % 360
    except (TypeError, ValueError):
        return "---"
    return DIR_ORDER[int((deg + 11.25) / 22.5) % 16]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_atis_version(now):
    global atis_counter
    letters = string.ascii_uppercase
    first  = letters[(atis_counter // 26) % 26]
    second = letters[atis_counter % 26]
    cycle  = f"{first}{second}"
    atis_counter += 1
    return f"{now.strftime('%d-%m')}-{cycle}"

def build_version(now):
    return f"YCV-WX-MAM-HOLZ-{get_atis_version(now)}"

def dps_tenth(dps: dict, key: str):
    """Return a Tuya tenth-scaled value as a float, or None."""
    v = dps.get(key)
    return round(v / 10, 1) if v is not None else None

def parse_dir(b64val):
    """
    Decode wind direction from Tuya blob field 134.
    Bytes 1-3 contain direction letters (N, S, E, W only).
    Returns a DIR_ORDER string or "CALM" if no valid compass letters found.
    """
    try:
        raw = base64.b64decode(b64val)
        s = ""
        for i in range(1, 4):
            if i < len(raw) and raw[i] in VALID_DIR_CHARS:
                s += chr(raw[i])
        return s if s else "CALM"
    except Exception:
        return "CALM"

def calc_qnh(pressure_hpa, temp_c, elevation_m):
    """ICAO hypsometric formula for sea-level pressure."""
    if pressure_hpa is None or temp_c is None:
        return None
    T = temp_c + 273.15
    return round(pressure_hpa * ((T / (T - 0.0065 * elevation_m)) ** 5.257), 1)

def dir_spread(dir_set: set) -> int:
    """Angular spread of a direction set, in 16ths of a circle."""
    idxs = sorted(DIR_IDX[d] for d in dir_set if d in DIR_IDX)
    if len(idxs) < 2:
        return 0
    gaps = [idxs[i + 1] - idxs[i] for i in range(len(idxs) - 1)]
    gaps.append(16 - idxs[-1] + idxs[0])
    return 16 - max(gaps)


# ---------------------------------------------------------------------------
# Open-Meteo: visibility (5-min cache)
# ---------------------------------------------------------------------------

_last_visibility: int | None = None
_last_vis_fetch: float = 0

def get_visibility() -> int | None:
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={FORECAST_LAT}&longitude={FORECAST_LON}"
            f"&current=visibility&timezone=UTC"
        )
        r = requests.get(url, timeout=5)
        return int(r.json()["current"]["visibility"])
    except Exception as e:
        print("Open-Meteo visibility error:", e)
        return None

def get_visibility_cached() -> int | None:
    global _last_visibility, _last_vis_fetch
    if time.time() - _last_vis_fetch > 300:
        _last_visibility = get_visibility()
        _last_vis_fetch  = time.time()
    return _last_visibility


# ---------------------------------------------------------------------------
# Open-Meteo: hourly forecast (15-min cache)
# ---------------------------------------------------------------------------

_forecast_cache: dict | None = None
_forecast_fetch_ts: float = 0
_forecast_lock = threading.Lock()

def _fetch_forecast_raw() -> dict | None:
    """
    Fetch FORECAST_PERIODS hours of hourly data from Open-Meteo starting at
    the current UTC hour.  Returns a structured dict or None on failure.

    Each period:
        time_hm         "HH:MM" UTC
        date_dmy        "DD-MM"
        temp_c          int
        precip_prob_pct int   (0-100, %)
        precip_mm       float (mm)
        wind_kmh        int
        wind_dir        str   (16-pt compass)
        weather_code    int   (WMO 4677)
        summary         str   (human-readable)
    """
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={FORECAST_LAT}&longitude={FORECAST_LON}"
            f"&hourly=temperature_2m,precipitation_probability,precipitation"
            f",weathercode,windspeed_10m,winddirection_10m"
            f"&wind_speed_unit=kmh&timezone=UTC&forecast_days=2"
        )
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data   = r.json()
        hourly = data["hourly"]
        times  = hourly["time"]                        # "YYYY-MM-DDTHH:00"
        temps  = hourly["temperature_2m"]
        prob   = hourly["precipitation_probability"]
        precip = hourly["precipitation"]
        codes  = hourly["weathercode"]
        wspeed = hourly["windspeed_10m"]
        wdir   = hourly["winddirection_10m"]

        now_utc  = datetime.now(timezone.utc)
        now_hour = now_utc.replace(minute=0, second=0, microsecond=0)

        periods = []
        for i, t_str in enumerate(times):
            t_dt = datetime.strptime(t_str, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
            if t_dt < now_hour:
                continue
            if len(periods) >= FORECAST_PERIODS:
                break
            periods.append({
                "time_hm":         t_dt.strftime("%H:%M"),
                "date_dmy":        t_dt.strftime("%d-%m"),
                "temp_c":          int(round(temps[i]))        if temps[i]  is not None else None,
                "precip_prob_pct": int(prob[i])                if prob[i]   is not None else None,
                "precip_mm":       round(float(precip[i]), 1)  if precip[i] is not None else None,
                "wind_kmh":        int(round(wspeed[i]))       if wspeed[i] is not None else None,
                "wind_dir":        degrees_to_dir(wdir[i])     if wdir[i]   is not None else None,
                "weather_code":    int(codes[i])               if codes[i]  is not None else None,
                "summary":         wmo_summary(codes[i]),
            })

        if not periods:
            return None

        fetched_hm = now_utc.strftime("%H:%M")
        valid_from = periods[0]["time_hm"]  + "Z " + periods[0]["date_dmy"]
        valid_to   = periods[-1]["time_hm"] + "Z " + periods[-1]["date_dmy"]

        return {
            "fetched_utc": fetched_hm,
            "valid_from":  valid_from,
            "valid_to":    valid_to,
            "periods":     periods,
        }

    except Exception as e:
        print("Open-Meteo forecast error:", e)
        return None

def get_forecast_cached() -> dict | None:
    global _forecast_cache, _forecast_fetch_ts
    with _forecast_lock:
        age = time.time() - _forecast_fetch_ts
        if age > FORECAST_CACHE_TTL or _forecast_cache is None:
            result = _fetch_forecast_raw()
            if result is not None:
                _forecast_cache    = result
                _forecast_fetch_ts = time.time()
            # On failure keep old cache if available; caller checks for None
        return _forecast_cache


# ---------------------------------------------------------------------------
# Wind history management
# ---------------------------------------------------------------------------

def _prune_wind_history():
    cutoff = time.time() - WIND_WINDOW_MIN * 60
    while wind_history and wind_history[0][0] < cutoff:
        wind_history.popleft()

def record_wind(dps: dict):
    if "134" not in dps and "131" not in dps:
        return
    raw_speed = latest.get("131")
    speed_kmh = round(raw_speed / 10, 1) if raw_speed is not None else None
    dir_raw   = latest.get("134")
    dir_code  = parse_dir(dir_raw) if dir_raw is not None else None
    if dir_code == "CALM":
        dir_code = None
    wind_history.append((time.time(), dir_code, speed_kmh))
    _prune_wind_history()

def analyse_wind() -> dict | None:
    _prune_wind_history()
    samples = list(wind_history)
    if not samples:
        return None

    speeds = [s[2] for s in samples if s[2] is not None]
    dirs   = [s[1] for s in samples if s[1] is not None]

    avg_speed = round(sum(speeds) / len(speeds), 1) if speeds else None
    max_speed = round(max(speeds), 1)                if speeds else None

    if not dirs:
        return {
            "avg_speed_kmh":  avg_speed,
            "max_speed_kmh":  max_speed,
            "dominant_code":  None,
            "dominant_full":  None,
            "variable":       False,
            "var_from":       None,
            "var_to":         None,
            "sample_count":   len(samples),
        }

    counts   = Counter(dirs)
    dominant = counts.most_common(1)[0][0]
    dom_frac = counts[dominant] / len(dirs)
    spread   = dir_spread(set(dirs))
    variable = (spread >= 3) and (dom_frac < 0.5)

    var_from = var_to = None
    if variable:
        idxs = sorted({DIR_IDX[d] for d in dirs})
        n    = len(idxs)
        gaps = []
        for i in range(n):
            next_i = (i + 1) % n
            gap = (idxs[next_i] - idxs[i]) % 16
            gaps.append((gap, i))
        largest_gap_pos = max(gaps, key=lambda x: x[0])[1]
        arc_start_pos   = (largest_gap_pos + 1) % n
        arc_end_pos     = largest_gap_pos
        var_from = DIR_ORDER[idxs[arc_start_pos]]
        var_to   = DIR_ORDER[idxs[arc_end_pos]]

    return {
        "avg_speed_kmh":  avg_speed,
        "max_speed_kmh":  max_speed,
        "dominant_code":  dominant,
        "dominant_full":  DIR_FULL.get(dominant, dominant),
        "variable":       variable,
        "var_from":       var_from,
        "var_to":         var_to,
        "sample_count":   len(samples),
    }

def wind_strings(wa: dict | None) -> tuple[str | None, str | None]:
    if wa is None:
        return None, None
    if wa["variable"]:
        vf, vt = wa["var_from"], wa["var_to"]
        return (
            f"V {vf}-{vt}",
            f"VARIABLE {DIR_FULL.get(vf, vf)} AND {DIR_FULL.get(vt, vt)}",
        )
    return wa["dominant_code"], wa["dominant_full"]


# ---------------------------------------------------------------------------
# Payload assembly
# ---------------------------------------------------------------------------

def make_payload() -> dict:
    l   = latest
    now = datetime.now(timezone.utc)
    wa  = analyse_wind()
    dir_code, dir_full = wind_strings(wa)

    temp_c = dps_tenth(l, "38")
    p_abs  = l.get("54")
    p_qnh  = calc_qnh(p_abs, temp_c, STATION_ELEVATION_M)

    raw_light  = l.get("135")
    light_klux = round(raw_light * 10 / 1000, 2) if raw_light is not None else None

    cur_speed_raw = l.get("131")
    cur_speed_kmh = int(round(cur_speed_raw / 10)) if cur_speed_raw is not None else None
    gust_raw      = l.get("57")
    gust_kmh      = int(round(gust_raw / 10))      if gust_raw      is not None else None

    vis = get_visibility_cached()

    return {
        "report_time": {
            "iso":      now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unix":     round(now.timestamp(), 2),
            "date_dmy": now.strftime("%d-%m-%Y"),
            "time_hm":  now.strftime("%H:%M"),
            "version":  build_version(now),
        },
        "outdoor": {
            "temperature_c": temp_c,
            "humidity_pct":  l.get("39"),
            "feels_like_c":  dps_tenth(l, "65"),
            "heat_index_c":  dps_tenth(l, "66"),
        },
        "wind": {
            "direction_code":      dir_code,
            "direction_full":      dir_full,
            "current_direction":   parse_dir(l["134"]) if "134" in l else None,
            "current_speed_kmh":   cur_speed_kmh,
            "gust_kmh":            gust_kmh,
            "avg_speed_kmh":       wa["avg_speed_kmh"] if wa else None,
            "max_speed_kmh":       wa["max_speed_kmh"] if wa else None,
            "variable":            wa["variable"]      if wa else False,
            "var_from":            wa["var_from"]      if wa else None,
            "var_to":              wa["var_to"]        if wa else None,
            "analysis_window_min": WIND_WINDOW_MIN,
            "sample_count":        wa["sample_count"]  if wa else 0,
        },
        "rain": {
            "event_mm":      dps_tenth(l, "59"),
            "daily_mm":      dps_tenth(l, "60"),
            "total_mm":      dps_tenth(l, "127"),
            "rate_mm_per_h": dps_tenth(l, "61"),
        },
        "atmosphere": {
            "pressure_abs_hpa": p_abs,
            "pressure_qnh_hpa": p_qnh,
            "uv_index":         l.get("62"),
            "light_klux":       light_klux,
        },
        "battery_pct": l.get("4"),
        "visibility": {
            "visibility_m":      vis,
            "visibility_source": "Open-Meteo API",
        },
    }


# ---------------------------------------------------------------------------
# Snapshot / git
# ---------------------------------------------------------------------------

def save_snapshot():
    ts  = latest.get("_ts", 0)
    age = time.time() - ts
    if age > 600:
        print(f"[SNAP] Skipped -- data is {int(age)}s stale (no Tuya updates)")
        return None, None

    payload = make_payload()
    fname   = datetime.now(timezone.utc).strftime("WX-REPORT-%d-%m-%y-%H-%M.json")
    fpath   = os.path.join(SNAPSHOT_DIR, fname)
    with open(fpath, "w") as f:
        json.dump(payload, f, indent=2)
    with open(os.path.join(SNAPSHOT_DIR, "latest.json"), "w") as f:
        json.dump(payload, f, indent=2)

    snaps = sorted(
        [x for x in os.listdir(SNAPSHOT_DIR) if x.startswith("WX-REPORT-") and x.endswith(".json")],
        reverse=True,
    )
    for old in snaps[MAX_SNAPSHOTS:]:
        os.remove(os.path.join(SNAPSHOT_DIR, old))
        print(f"[SNAP] Pruned {old}")

    print(f"[SNAP] Saved {fname}")
    threading.Thread(target=_git_push, args=(fname,), daemon=True).start()
    return fname, payload

def _git_push(fname):
    try:
        subprocess.run(["git", "-C", GITHUB_REPO, "add", SNAPSHOT_DIR], check=True, capture_output=True)
        subprocess.run(["git", "-C", GITHUB_REPO, "commit", "-m", f"wx {fname}"], check=True, capture_output=True)
        subprocess.run(["git", "-C", GITHUB_REPO, "push", "--rebase"], check=True, capture_output=True)
        print(f"[GIT] Pushed {fname}")
    except subprocess.CalledProcessError as e:
        print(f"[GIT] Failed: {(e.stderr or b'').decode().strip()}")


# ---------------------------------------------------------------------------
# Background threads
# ---------------------------------------------------------------------------

def scheduler():
    global last_snap_min
    while True:
        m = datetime.now(timezone.utc).minute
        if m in (27, 57) and m != last_snap_min:
            last_snap_min = m
            threading.Thread(target=save_snapshot, daemon=True).start()
        time.sleep(20)

def listener():
    while True:
        try:
            device = tinytuya.OutletDevice(DEVICE_ID, IP, LOCAL_KEY)
            device.set_version(3.4)
            device.set_socketPersistent(True)
            last_update = time.time()
            while True:
                data = device.receive()
                if data and "dps" in data:
                    dps = data["dps"]
                    all_seen_keys.update(dps)
                    latest.update(dps)
                    latest["_ts"] = time.time()
                    last_update   = time.time()
                    record_wind(dps)
                if time.time() - last_update > 300:
                    raise Exception("No Tuya updates for 5 minutes -- reconnecting")
        except Exception as e:
            print(f"[Listener restart] {e}")
            time.sleep(5)

def heartbeat():
    while True:
        try:
            device = tinytuya.OutletDevice(DEVICE_ID, IP, LOCAL_KEY)
            device.set_version(3.4)
            while True:
                try:
                    data = device.status()
                    if data and "dps" in data:
                        dps = data["dps"]
                        all_seen_keys.update(dps)
                        latest.update(dps)
                        latest["_ts"] = time.time()
                        record_wind(dps)
                except Exception as e:
                    print(f"[Heartbeat] Poll error: {e}")
                    break
                time.sleep(10)
        except Exception as e:
            print(f"[Heartbeat] Device init error: {e}")
        time.sleep(5)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            with open("index.html", "rb") as f:
                self.wfile.write(f.read())
        elif self.path == "/report":
            self.send_json(make_payload())
        elif self.path == "/forecast":
            fc = get_forecast_cached()
            if fc is not None:
                self.send_json(fc)
            else:
                self.send_json({"error": "forecast unavailable"}, status=503)
        elif self.path == "/data":
            d = dict(latest)
            d["_wind_analysis"] = analyse_wind()
            d["_wind_history"]  = [
                {"ts": e[0], "dir": e[1], "speed_kmh": e[2]}
                for e in list(wind_history)
            ]
            self.send_json(d)
        elif self.path == "/log":
            self.send_json(live_log[-100:])
        elif self.path == "/snapshots":
            snaps = sorted(
                [x for x in os.listdir(SNAPSHOT_DIR) if x.startswith("WX-REPORT-") and x.endswith(".json")],
                reverse=True,
            )
            self.send_json({"count": len(snaps), "latest": snaps[:5]})
        elif self.path == "/health":
            ts  = latest.get("_ts", 0)
            age = round(time.time() - ts, 1)
            self.send_json({
                "data_age_s":      age,
                "stale":           age > 120,
                "sample_count":    len(wind_history),
                "last_update":     datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None,
                "forecast_cached": _forecast_cache is not None,
                "forecast_age_s":  round(time.time() - _forecast_fetch_ts, 1) if _forecast_cache else None,
            })
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/snapshot/now":
            fname, payload = save_snapshot()
            if fname:
                self.send_json({"ok": True, "file": fname, "payload": payload})
            else:
                self.send_json({"ok": False, "reason": "data stale"}, status=503)
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Pre-warm forecast cache before first broadcast
    threading.Thread(target=get_forecast_cached, daemon=True).start()

    threading.Thread(target=listener,  daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    server = HTTPServer(("0.0.0.0", 8090), Handler)
    print("=== Tuya WX ===  http://0.0.0.0:8090")
    print("  GET  /report       -> clean JSON weather payload")
    print("  GET  /forecast     -> 6-hour Open-Meteo hourly forecast (15-min cache)")
    print("  GET  /data         -> full dashboard state + wind history")
    print("  GET  /health       -> staleness + forecast cache status")
    print("  POST /snapshot/now -> force snapshot save + git push")
    print(f"  Snapshots at :27 and :57 UTC | elevation {STATION_ELEVATION_M}m (Mamer)")
    server.serve_forever()