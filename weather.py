"""Current temperature and weather code for the strip's clock box.

Location from ip-api.com, weather from Open-Meteo (both plain HTTP, no key),
refreshed every 15 minutes in a background thread. Local time comes from
the Pi's own clock and time zone.
"""

import threading
import time

from espn import SESSION

temp_f = None      # None = unknown
code = -1          # WMO weather code
is_day = True
updated_at = 0.0   # last good reading (health)


def _loop():
    global temp_f, code, is_day, updated_at
    lat = lon = None
    while True:
        try:
            if lat is None:
                d = SESSION.get("http://ip-api.com/json/?fields=status,lat,lon", timeout=8).json()
                if d.get("status") == "success":
                    lat, lon = d["lat"], d["lon"]
                    print(f"[Wx] location {lat:.3f},{lon:.3f}", flush=True)
            if lat is not None:
                d = SESSION.get("http://api.open-meteo.com/v1/forecast", timeout=8, params={
                    "latitude": f"{lat:.3f}", "longitude": f"{lon:.3f}",
                    "current": "temperature_2m,weather_code,is_day",
                    "temperature_unit": "fahrenheit", "timezone": "auto"}).json()
                cur = d["current"]
                temp_f = round(cur["temperature_2m"])
                code = int(cur.get("weather_code", -1))
                is_day = bool(cur.get("is_day", 1))
                updated_at = time.time()
                time.sleep(15 * 60)
                continue
        except Exception as e:  # noqa: BLE001
            print(f"[Wx] {e}", flush=True)
        time.sleep(60)


def start():
    threading.Thread(target=_loop, daemon=True).start()
