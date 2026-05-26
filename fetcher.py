#!/usr/bin/env python3
"""
fetcher.py
----------
Fetches all data needed for the Golden inversion infographic and writes
a single sounding.json file that nginx serves statically.

Run by cron every 30 minutes:
    */30 * * * * cd /opt/golden && uv run python fetcher.py >> /var/log/golden-fetcher.log 2>&1

No web framework, no matplotlib, no MetPy.
Dependencies: requests, pandas  (see pyproject.toml)
"""

import datetime
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

# ── Configuration ──────────────────────────────────────────────────────────────

OUTPUT_PATH   = Path(
    __import__("os").environ.get("GOLDEN_OUTPUT_PATH", "/var/www/golden_inversion/sounding.json")
)
SOUNDING_HOUR = 21

GOLDEN_LAT    = 51.30
GOLDEN_LON    = -116.98
GOLDEN_ELEV_M = 785

DOGTOOTH_ELEV_M   = 2060
WHITE_WALL_ELEV_M = 2450

DOGTOOTH_URL   = "https://www.mountainweather.ca/data/DOGSNOWSAFETY.HTM"
WHITE_WALL_URL = "https://mountainweather.ca/data/TOP_FTP.HTM"
VENTING_URL    = "https://envistaweb.env.gov.bc.ca/aqo/files/bulletin/venting.html"
TIMEZONE       = "America/Vancouver"

PRESSURE_LEVELS = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300]
LAPSE_RATE      = 6.5       # °C / km  (standard environmental)

RETRY_ATTEMPTS = 2
RETRY_DELAY    = 5          # seconds between retries

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────────

def expected_temp(valley_t: float, valley_elev: float, target_elev: float) -> float:
    return valley_t - LAPSE_RATE * (target_elev - valley_elev) / 1000.0


def with_retry(fn, name: str):
    """Call fn(), retrying up to RETRY_ATTEMPTS times on any exception."""
    for attempt in range(RETRY_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == RETRY_ATTEMPTS:
                raise
            log.warning("  %s attempt %d failed (%s) — retrying in %ds",
                        name, attempt + 1, exc, RETRY_DELAY)
            time.sleep(RETRY_DELAY)

def load_previous_sounding() -> list:
    """Return the last successfully written sounding, or empty list."""
    try:
        old = json.loads(OUTPUT_PATH.read_text())
        sounding = old.get("sounding", [])
        if sounding:
            log.info("  using previous sounding (%d levels)", len(sounding))
        return sounding
    except Exception:
        return []            

def load_previous(key: str) -> Optional[float]:
    """
    Return the last successfully written temperature for a given key
    ('valley', 'dogtooth', 'white_wall') from the existing sounding.json.
    Used as last-resort fallback when all live fetches fail.
    """
    try:
        old = json.loads(OUTPUT_PATH.read_text())
        val = old.get("temps", {}).get(key)
        if val is not None:
            log.info("  using previous value for %s: %s°C", key, val)
        return val
    except Exception:
        return None


# ── Fetchers ───────────────────────────────────────────────────────────────────

def fetch_sounding() -> list:
    """
    GFS pressure-level sounding from Open-Meteo.
    Returns list of dicts in skewt-js format: {press, hght, temp, dwpt, wdir, wspd(m/s)}
    Two requests to keep URL length reasonable.
    """
    vars_A, vars_B = [], []
    for p in PRESSURE_LEVELS:
        vars_A += [f"temperature_{p}hPa", f"dewpoint_{p}hPa"]
        vars_B += [f"windspeed_{p}hPa", f"winddirection_{p}hPa",
                   f"geopotential_height_{p}hPa"]

    base = {
        "latitude": GOLDEN_LAT, "longitude": GOLDEN_LON,
        "timezone": TIMEZONE, "forecast_days": 3, "models": "gfs_seamless",
    }

    rA = requests.get("https://api.open-meteo.com/v1/forecast",
                      params={**base, "hourly": ",".join(vars_A)}, timeout=30)
    rA.raise_for_status()
    rB = requests.get("https://api.open-meteo.com/v1/forecast",
                      params={**base, "hourly": ",".join(vars_B)}, timeout=30)
    rB.raise_for_status()

    h = {**rA.json()["hourly"], **rB.json()["hourly"]}
    times  = pd.to_datetime(h["time"])
    target = pd.Timestamp(datetime.date.today()) + pd.Timedelta(hours=SOUNDING_HOUR)
    import numpy as np
    idx = int(np.argmin([abs((t - target).total_seconds()) for t in times]))

    def _v(key):
        lst = h.get(key)
        return lst[idx] if lst and idx < len(lst) else None

    levels = []
    for p in PRESSURE_LEVELS:
        t = _v(f"temperature_{p}hPa")
        if t is None:
            continue
        hgt  = _v(f"geopotential_height_{p}hPa")
        wspd = _v(f"windspeed_{p}hPa")
        levels.append({
            "press": float(p),
            "hght":  round(float(hgt),  0) if hgt  is not None else None,
            "temp":  round(float(t),    1),
            "dwpt":  round(float(_v(f"dewpoint_{p}hPa")), 1)
                         if _v(f"dewpoint_{p}hPa") is not None else None,
            "wdir":  round(float(_v(f"winddirection_{p}hPa")), 0)
                         if _v(f"winddirection_{p}hPa") is not None else None,
            "wspd":  round(float(wspd) / 3.6, 2)
                         if wspd is not None else None,
        })
    return levels


def fetch_valley_temp() -> Optional[float]:
    """
    Current temperature at Golden Airport (CYGE) from the hourly METAR —
    a real observed valley-floor reading, not a model forecast.
    """
    r = requests.get(
        "https://aviationweather.gov/api/data/metar",
        params={"ids": "CYGE", "format": "json"},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if data and data[0].get("temp") is not None:
        return round(float(data[0]["temp"]), 1)
    raise ValueError("CYGE METAR returned no temperature")


def fetch_mountain_temp(url: str) -> Optional[float]:
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    temps = []
    for line in r.text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            int(parts[0]); int(parts[1]); int(parts[2])
            temps.append(float(parts[3]))
        except (ValueError, IndexError):
            pass
    if not temps:
        raise ValueError(f"no temperature data found at {url}")
    return round(temps[-1], 1)


def fetch_venting() -> dict:
    result = {
        "today_am": None, "today_pm": None, "tomorrow": None,
        "raw_line": None, "bulletin_date": None,
    }
    r = requests.get(VENTING_URL, timeout=15)
    r.raise_for_status()
    text = r.text

    date_m = re.search(r'(\d{1,2}-[A-Z]+-\d{4})', text, re.IGNORECASE)
    if date_m:
        result["bulletin_date"] = date_m.group(1).upper()

    line_m = re.search(r'(GOLDEN\s+.*)', text, re.IGNORECASE)
    if not line_m:
        return result

    raw = line_m.group(1).rstrip()
    result["raw_line"] = raw

    pairs = re.findall(r'(\d+|NA)/(POOR|FAIR|GOOD|NA)', raw, re.IGNORECASE)
    for key, (vi_str, cat) in zip(["today_am", "today_pm", "tomorrow"], pairs[:3]):
        if vi_str.upper() != "NA":
            result[key] = {"vi": int(vi_str), "cat": cat.upper()}
    return result


# ── Scoring ────────────────────────────────────────────────────────────────────

def score(sounding: list, vi: dict,
          valley_t: Optional[float],
          dog_t:    Optional[float],
          ww_t:     Optional[float]) -> tuple:

    points, hints = 0, []

    # A) Venting Index
    best = vi.get("today_pm") or vi.get("today_am") or vi.get("tomorrow")
    if best:
        v, cat = best["vi"], best["cat"]
        if v <= 33:
            points += 2
            hints.append(f"Venting Index POOR ({v}) — smoke traps easily in valleys")
        elif v <= 54:
            points += 1
            hints.append(f"Venting Index FAIR ({v}) — marginal smoke dispersal")
        else:
            hints.append(f"Venting Index GOOD ({v}) — air should disperse well")
    else:
        hints.append("Venting Index unavailable")

    # B) Valley–mountain ΔT
    if valley_t is not None and (dog_t is not None or ww_t is not None):
        if dog_t is not None:
            obs   = valley_t - dog_t
            exp   = LAPSE_RATE * (DOGTOOTH_ELEV_M - GOLDEN_ELEV_M) / 1000
            label = f"Dogtooth ({DOGTOOTH_ELEV_M} m)"
        else:
            obs   = valley_t - ww_t
            exp   = LAPSE_RATE * (WHITE_WALL_ELEV_M - GOLDEN_ELEV_M) / 1000
            label = f"White Wall ({WHITE_WALL_ELEV_M} m)"
        anomaly = exp - obs
        if anomaly >= 4.0:
            points += 2
            hints.append(f"Valley→{label} ΔT only {obs:.1f}°C (normal ≈ {exp:.1f}°C) — strong inversion signal")
        elif anomaly >= 2.0:
            points += 1
            hints.append(f"Valley→{label} ΔT {obs:.1f}°C (normal ≈ {exp:.1f}°C) — slight inversion possible")
        else:
            hints.append(f"Valley→{label} ΔT {obs:.1f}°C ≈ normal ({exp:.1f}°C)")
    else:
        hints.append("No valley temperature for ΔT check")

    # C) Sounding low-level lapse
    low = [s for s in sounding if 850 <= s["press"] <= 1000]
    low.sort(key=lambda s: s["press"], reverse=True)
    if len(low) >= 2:
        t_bot, t_top = low[0]["temp"], low[-1]["temp"]
        if t_top > t_bot:
            points += 2
            hints.append(f"Sounding shows INVERSION below 850 hPa ({t_bot:.1f}°C → {t_top:.1f}°C aloft)")
        elif (t_bot - t_top) < 2.0:
            points += 1
            hints.append(f"Near-neutral lapse below 850 hPa ({t_bot:.1f}°C → {t_top:.1f}°C) — weak mixing")
        else:
            hints.append(f"Normal lapse below 850 hPa ({t_bot:.1f}°C → {t_top:.1f}°C)")
    else:
        hints.append("Insufficient sounding levels for lapse check")

    return points, hints


def verdict(points: int,
            valley_t: Optional[float],
            dog_t:    Optional[float],
            ww_t:     Optional[float]) -> str:
    mtn_temps  = [t for t in [dog_t, ww_t] if t is not None]
    true_inv   = valley_t is not None and any(t > valley_t for t in mtn_temps)
    lapse_weak = (
        valley_t is not None and dog_t is not None and
        (valley_t - dog_t) < LAPSE_RATE * (DOGTOOTH_ELEV_M - GOLDEN_ELEV_M) / 1000 * 0.6
    )
    if true_inv:
        return "There's a real inversion. Don't light that wood stove."
    if lapse_weak:
        return "No inversion yet, but lapse rate is weak — smoke won't mix well tonight."
    return "No inversion. Normal lapse rate. Air is mixing."


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log.info("Fetching data …")

    tasks = {
        "sounding": fetch_sounding,
        "valley_t": fetch_valley_temp,
        "dog_t":    lambda: fetch_mountain_temp(DOGTOOTH_URL),
        "ww_t":     lambda: fetch_mountain_temp(WHITE_WALL_URL),
        "venting":  fetch_venting,
    }

    results: dict = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(with_retry, fn, name): name
                   for name, fn in tasks.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
                log.info("  ✓ %s", name)
            except Exception as exc:
                results[name] = None
                log.warning("  ✗ %s  — %s", name, exc)

    #sounding = results.get("sounding") or []
    sounding = results.get("sounding") or load_previous_sounding()
    vi       = results.get("venting")  or {}

    # Use previous JSON values as last resort for temperatures
    valley_t = results.get("valley_t") or load_previous("valley")
    dog_t    = results.get("dog_t")    or load_previous("dogtooth")
    ww_t     = results.get("ww_t")     or load_previous("white_wall")

    pts, hints = score(sounding, vi, valley_t, dog_t, ww_t)
    risk_label = "HIGH" if pts >= 4 else "MODERATE" if pts >= 2 else "LOW"

    payload = {
        "generated":    datetime.datetime.now().isoformat(timespec="seconds"),
        "sounding_hour": SOUNDING_HOUR,
        "sounding":     sounding,
        "venting":      vi,
        "temps": {
            "valley":     valley_t,
            "dogtooth":   dog_t,
            "white_wall": ww_t,
        },
        "elevations": {
            "valley":     GOLDEN_ELEV_M,
            "dogtooth":   DOGTOOTH_ELEV_M,
            "white_wall": WHITE_WALL_ELEV_M,
        },
        "score":   pts,
        "risk":    risk_label,
        "hints":   hints,
        "verdict": verdict(pts, valley_t, dog_t, ww_t),
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    tmp = OUTPUT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.rename(OUTPUT_PATH)

    log.info("Wrote %s  (score=%d/6  %s)", OUTPUT_PATH, pts, risk_label)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.error("Fatal: %s", exc)
        sys.exit(1)
