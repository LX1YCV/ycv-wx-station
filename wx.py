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

os.makedirs(SNAPSHOT_DIR, exist_ok=True)

latest        = {}
all_seen_keys = {}
live_log      = []
last_snap_min = -1
MAX_LOG = 300
wind_history  = deque()

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

def parse_dir(b64val):
    """
    Decode wind direction from Tuya blob field 134.
    Bytes 1-3 contain direction letters (N, S, E, W only).
    Byte 2 = primary axis. Byte 1/3 = prefix/suffix for intercardinals.
    If no valid letters found, the station is reporting CALM.
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
    """
    ICAO hypsometric formula for QNH (sea-level pressure).
    QNH = Pstation * (T / (T - 0.0065 * h))^5.257
    where T is temperature in Kelvin at station elevation.
    """
    if pressure_hpa is None or temp_c is None:
        return None
    T = temp_c + 273.15
    return round(pressure_hpa * ((T / (T - 0.0065 * elevation_m)) ** 5.257), 1)

def dir_spread(dirs):
    """Angular spread in 16ths (>= 3 means >= 60 deg)."""
    idxs = sorted({DIR_IDX[d] for d in dirs if d in DIR_IDX})
    if len(idxs) < 2:
        return 0
    gaps = [idxs[i+1] - idxs[i] for i in range(len(idxs)-1)]
    gaps.append(16 - idxs[-1] + idxs[0])
    return 16 - max(gaps)

# =========================
# OPEN-METEO VISIBILITY
# =========================

last_visibility = None
last_fetch = 0

def get_visibility():
    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=49.63&longitude=6.02"
            "&current=visibility"
            "&timezone=UTC"
        )
        r = requests.get(url, timeout=5)
        data = r.json()
        return data["current"]["visibility"]  # meters (int)
    except Exception as e:
        print("Open-Meteo error:", e)
        return None

def get_visibility_cached():
    global last_visibility, last_fetch
    if time.time() - last_fetch > 300:  # 5 min cache
        last_visibility = get_visibility()
        last_fetch = time.time()
    return last_visibility

def analyse_wind():
    now = time.time()
    cutoff = now - WIND_WINDOW_MIN * 60
    while wind_history and wind_history[0][0] < cutoff:
        wind_history.popleft()

    samples = list(wind_history)
    if not samples:
        return None

    speeds = [s[2] for s in samples if s[2] is not None]
    dirs   = [s[1] for s in samples if s[1] is not None and s[1] in DIR_IDX]
    avg_speed = round(sum(speeds) / len(speeds), 1) if speeds else None
    max_speed = max(speeds) if speeds else None

    if not dirs:
        return {"avg_speed_kmh": avg_speed, "max_speed_kmh": max_speed,
                "dominant_code": None, "dominant_full": None,
                "variable": False, "var_from": None, "var_to": None,
                "sample_count": len(samples)}

    counts   = Counter(dirs)
    dominant = counts.most_common(1)[0][0]
    dom_frac = counts[dominant] / len(dirs)
    spread   = dir_spread(dirs)
    variable = spread >= 3 and dom_frac < 0.5

    var_from = var_to = None
    if variable:
        idxs = sorted({DIR_IDX[d] for d in dirs})
        gaps = [(i, idxs[(i+1) % len(idxs)] - idxs[i] if i < len(idxs)-1
                 else 16 - idxs[-1] + idxs[0]) for i in range(len(idxs))]
        lg_i = max(gaps, key=lambda x: x[1])[0]
        arc_start = idxs[(lg_i + 1) % len(idxs)]
        arc_end   = idxs[lg_i]
        var_from  = DIR_ORDER[arc_start]
        var_to    = DIR_ORDER[arc_end]

    return {"avg_speed_kmh": avg_speed, "max_speed_kmh": max_speed,
            "dominant_code": dominant, "dominant_full": DIR_FULL.get(dominant, dominant),
            "variable": variable, "var_from": var_from, "var_to": var_to,
            "sample_count": len(samples)}

def wind_strings(wa):
    if wa is None:
        return None, None
    if wa["variable"]:
        vf, vt = wa["var_from"], wa["var_to"]
        return (f"V {vf}-{vt}",
                f"VARIABLE {DIR_FULL.get(vf,vf)} AND {DIR_FULL.get(vt,vt)}")
    return wa["dominant_code"], wa["dominant_full"]

def make_payload():
    l   = latest
    now = datetime.now(timezone.utc)
    version = build_version(now)
    wa  = analyse_wind()
    dir_code, dir_full = wind_strings(wa)
    cur_dir = parse_dir(l["134"]) if "134" in l else None

    def t10(k):
        v = l.get(k); return round(v / 10, 1) if v is not None else None
    def r10(k):
        v = l.get(k); return round(v / 10, 1) if v is not None else None
    def w10(k):
        v = l.get(k); return round(v / 10, 1) if v is not None else None

    temp_c  = t10("38")
    p_abs   = l.get("54")
    p_qnh   = calc_qnh(p_abs, temp_c, STATION_ELEVATION_M)

    raw_light = l.get("135")
    light_klux = round(raw_light * 10 / 1000, 2) if raw_light is not None else None

    return {
        "report_time": {
            "iso":      now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unix":     round(now.timestamp(), 2),
            "date_dmy": now.strftime("%d-%m-%Y"),
            "time_hm":  now.strftime("%H:%M"),
            "version":  version,
        },
        "outdoor": {
            "temperature_c":  temp_c,
            "humidity_pct":   l.get("39"),
            "feels_like_c":   t10("65"),
            "heat_index_c":   t10("66"),
        },
        "wind": {
            "direction_code":      dir_code,
            "direction_full":      dir_full,
            "current_direction":   cur_dir,
            "current_speed_kmh":   int(w10("131")),
            "avg_speed_kmh":       round(wa["avg_speed_kmh"] / 10, 1) if wa and wa["avg_speed_kmh"] is not None else w10("56"),
            "gust_kmh":            int(w10("57")),
            "variable":            wa["variable"] if wa else False,
            "var_from":            wa["var_from"] if wa else None,
            "var_to":              wa["var_to"] if wa else None,
            "analysis_window_min": WIND_WINDOW_MIN,
            "sample_count":        wa["sample_count"] if wa else 0,
        },
        "rain": {
            "event_mm":      r10("59"),
            "daily_mm":      r10("60"),
            "total_mm":      r10("127"),
            "rate_mm_per_h": r10("61"),
        },
        "atmosphere": {
            "pressure_abs_hpa": p_abs,
            "pressure_qnh_hpa": p_qnh,
            "uv_index":         l.get("62"),
            "light_klux":       light_klux,
        },
        "battery_pct": l.get("4"),
        "visibility": {
            "visibility_m":      get_visibility_cached(),
            "visibility_source": "Open-Meteo API",
        },
    }

def save_snapshot():
    # Stale data guard -- bail if latest hasn't updated in 10 min
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

    snaps = sorted([x for x in os.listdir(SNAPSHOT_DIR)
                    if x.startswith("WX-REPORT-") and x.endswith(".json")], reverse=True)
    for old in snaps[MAX_SNAPSHOTS:]:
        os.remove(os.path.join(SNAPSHOT_DIR, old))
        print(f"[SNAP] Pruned {old}")

    print(f"[SNAP] Saved {fname}")
    threading.Thread(target=_git_push, args=(fname,), daemon=True).start()
    return fname, payload

def _git_push(fname):
    try:
        subprocess.run(["git","-C",GITHUB_REPO,"add",SNAPSHOT_DIR], check=True, capture_output=True)
        subprocess.run(["git","-C",GITHUB_REPO,"commit","-m",f"wx {fname}"], check=True, capture_output=True)
        subprocess.run(["git","-C",GITHUB_REPO,"push"], check=True, capture_output=True)
        print(f"[GIT] Pushed {fname}")
    except subprocess.CalledProcessError as e:
        print(f"[GIT] Failed: {(e.stderr or b'').decode().strip()}")

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
                    last_update = time.time()

                    if "134" in dps or "131" in dps:
                        code = parse_dir(latest.get("134", ""))
                        if code != "CALM":
                            wind_history.append((time.time(), code, latest.get("131")))

                # Stall detection: no data for 5 min -> force reconnect
                if time.time() - last_update > 300:
                    raise Exception("No Tuya updates for 5 minutes -- reconnecting")

        except Exception as e:
            print(f"[Listener restart] {e}")
            time.sleep(5)

def heartbeat():
    """
    Polls device status every 10s as a fallback / keepalive.
    Outer loop ensures a dead socket never silently kills this thread.
    """
    while True:
        try:
            device = tinytuya.OutletDevice(DEVICE_ID, IP, LOCAL_KEY)
            device.set_version(3.4)
            while True:
                try:
                    data = device.status()
                    if data and "dps" in data:
                        all_seen_keys.update(data["dps"])
                        latest.update(data["dps"])
                        latest["_ts"] = time.time()
                except Exception as e:
                    print(f"[Heartbeat] Poll error: {e}")
                    break  # break inner loop; outer loop reinits device
                time.sleep(10)
        except Exception as e:
            print(f"[Heartbeat] Device init error: {e}")
        time.sleep(5)  # brief pause before reconnect attempt

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
        elif self.path == "/data":
            d = dict(latest)
            d["_wind_analysis"] = analyse_wind()
            d["_wind_history"]  = [{"ts": e[0], "dir": e[1], "speed": e[2]}
                                    for e in list(wind_history)]
            self.send_json(d)
        elif self.path == "/log":
            self.send_json(live_log[-100:])
        elif self.path == "/snapshots":
            snaps = sorted([x for x in os.listdir(SNAPSHOT_DIR)
                            if x.startswith("WX-REPORT-") and x.endswith(".json")], reverse=True)
            self.send_json({"count": len(snaps), "latest": snaps[:5]})
        elif self.path == "/health":
            ts  = latest.get("_ts", 0)
            age = round(time.time() - ts, 1)
            self.send_json({
                "data_age_s":   age,
                "stale":        age > 120,
                "sample_count": len(wind_history),
                "last_update":  datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None,
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

if __name__ == "__main__":
    threading.Thread(target=listener,  daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    server = HTTPServer(("0.0.0.0", 8090), Handler)
    print("=== Tuya WX ===  http://0.0.0.0:8090")
    print("  GET  /report       -> clean JSON for website/API")
    print("  GET  /data         -> full dashboard state")
    print("  GET  /health       -> data age + stale flag")
    print("  POST /snapshot/now -> force save+push")
    print(f"  Snapshots at :27 and :57 UTC | elevation {STATION_ELEVATION_M}m (Mamer)")
    server.serve_forever()
