"""
golden_inversion.py
-------------------
Evening temperature-inversion infographic for Golden, BC.

Three diagnostic panels, combined into one resident-readable PNG:
  1.  Skew-T log-P sounding   — Open-Meteo GFS pressure-level data via MetPy
  2.  BC Venting Index         — scraped from envistaweb.env.gov.bc.ca
  3.  Elevation temp profile   — Golden valley vs. Kicking Horse mountain stations
                                 with a normal-lapse-rate reference line

Inversion risk is scored 0-6 and displayed as LOW / MODERATE / HIGH.

Usage:
    python golden_inversion.py              # tonight's 21:00 sounding (default)
    python golden_inversion.py --hour 18    # choose local sounding hour
    python golden_inversion.py --output golden_inversion.png

Data sources:
    • https://api.open-meteo.com                         — pressure-level sounding + 2 m temp
    • https://envistaweb.env.gov.bc.ca/aqo/files/bulletin/venting.html
                                                         — BC Venting Index bulletin (Golden)
    • https://mountainweather.ca/data/TOP_FTP.HTM        — White Wall station (2 450 m)
    • https://www.mountainweather.ca/data/DOGSNOWSAFETY.HTM — Dogtooth station (2 060 m)

Requirements (see pyproject.toml):
    metpy, requests, numpy, pandas, matplotlib

Note on valley temperature:
    Defaults to Open-Meteo 2 m forecast for Golden airport (CYGE, 785 m).
    If you have PurpleAir sensor API keys, replace fetch_valley_temp() with
    an average of nearby sensors — the signature is identical (returns float°C).
"""

from __future__ import annotations

import argparse
import datetime
import re
from typing import Optional, Tuple

import matplotlib.patheffects as pe
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

import metpy.calc as mpcalc
from metpy.plots import SkewT
from metpy.units import units as munits

# ── Station constants ──────────────────────────────────────────────────────────

GOLDEN_LAT = 51.30
GOLDEN_LON = -116.98
GOLDEN_ELEV_M = 785          # CYGE Golden Airport elevation (m ASL)

# Kicking Horse Mountain Resort stations (Kicking Horse, BC)
DOGTOOTH_URL = "https://www.mountainweather.ca/data/DOGSNOWSAFETY.HTM"
WHITE_WALL_URL = "https://mountainweather.ca/data/TOP_FTP.HTM"
DOGTOOTH_ELEV_M = 2060       # Dogtooth Snow Study Plot
WHITE_WALL_ELEV_M = 2325     # White Wall Remote Weather Station

# BC Venting Index bulletin (text format, updated by ECCC/BCENV)
VENTING_URL = "https://envistaweb.env.gov.bc.ca/aqo/files/bulletin/venting.html"

TIMEZONE = "America/Vancouver"

# ── Sounding pressure levels to fetch ─────────────────────────────────────────
# Open-Meteo supports: 1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300 hPa
PRESSURE_LEVELS_HPA = [1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300]

# Environmental (standard) lapse rate — used for "normal" temperature profile reference
NORMAL_LAPSE_RATE_C_PER_KM = 6.5

# ── Type aliases ───────────────────────────────────────────────────────────────
ViData = Optional[Tuple[int, str]]   # (ventilation_index, category_string) or None


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def elev_to_pressure(h_m: float) -> float:
    """Convert metres ASL → approximate pressure (hPa), standard atmosphere."""
    return 1013.25 * (1.0 - 2.25577e-5 * h_m) ** 5.25588


def expected_temp(valley_t: float, valley_elev: float, target_elev: float,
                  lapse: float = NORMAL_LAPSE_RATE_C_PER_KM) -> float:
    """Temperature expected at target_elev given valley_t at valley_elev."""
    return valley_t - lapse * (target_elev - valley_elev) / 1000.0


def vi_colour(vi_val: Optional[int], cat: Optional[str]) -> str:
    """Colour for a given Venting Index value / category."""
    if cat == "POOR" or (vi_val is not None and vi_val <= 33):
        return "#C62828"   # red
    if cat == "FAIR" or (vi_val is not None and vi_val <= 54):
        return "#E65100"   # orange
    return "#2E7D32"       # green


# ══════════════════════════════════════════════════════════════════════════════
#  Data fetchers
# ══════════════════════════════════════════════════════════════════════════════

def fetch_sounding(target_hour: int) -> dict:
    """
    Fetch GFS pressure-level sounding from Open-Meteo for Golden at
    today's target_hour (local).  Returns dict with numpy arrays.
    """
    # Open-Meteo pressure-level variable names follow the pattern used for
    # surface variables: dewpoint (no underscore), windspeed, winddirection.
    # Two requests keep the URL short and avoid any query-string length limits:
    #   request A — temperature + dewpoint (for Skew-T profile)
    #   request B — windspeed + winddirection + geopotential_height (for barbs/heights)
    vars_A = []
    vars_B = []
    for p in PRESSURE_LEVELS_HPA:
        vars_A += [f"temperature_{p}hPa", f"dewpoint_{p}hPa"]
        vars_B += [f"windspeed_{p}hPa", f"winddirection_{p}hPa",
                   f"geopotential_height_{p}hPa"]

    common_params = {
        "latitude": GOLDEN_LAT,
        "longitude": GOLDEN_LON,
        "timezone": TIMEZONE,
        "forecast_days": 3,
        "models": "gfs_seamless",
    }

    rA = requests.get("https://api.open-meteo.com/v1/forecast",
                      params={**common_params, "hourly": ",".join(vars_A)},
                      timeout=30)
    rA.raise_for_status()

    rB = requests.get("https://api.open-meteo.com/v1/forecast",
                      params={**common_params, "hourly": ",".join(vars_B)},
                      timeout=30)
    rB.raise_for_status()

    # Merge both hourly dicts
    hA = rA.json()["hourly"]
    hB = rB.json()["hourly"]
    h = {**hA, **hB}

    times = pd.to_datetime(h["time"])
    target = pd.Timestamp(datetime.date.today()) + pd.Timedelta(hours=target_hour)
    idx = int(np.argmin(np.abs([(t - target).total_seconds() for t in times])))
    valid_time = times[idx]

    def _val(key, i):
        lst = h.get(key)
        if lst and i < len(lst):
            return lst[i]
        return None

    pressures, temps, dews, wspds, wdirs, heights = [], [], [], [], [], []
    for p in PRESSURE_LEVELS_HPA:
        t = _val(f"temperature_{p}hPa", idx)
        if t is None:
            continue
        d = _val(f"dewpoint_{p}hPa", idx)
        ws = _val(f"windspeed_{p}hPa", idx)
        wd = _val(f"winddirection_{p}hPa", idx)
        hgt = _val(f"geopotential_height_{p}hPa", idx)
        pressures.append(float(p))
        temps.append(float(t))
        dews.append(float(d) if d is not None else float(t) - 25.0)
        wspds.append(float(ws) if ws is not None else 0.0)
        wdirs.append(float(wd) if wd is not None else 0.0)
        # Crude hypsometric fallback if geopotential_height not available
        heights.append(float(hgt) if hgt is not None else (1 - (p / 1013.25) ** (1 / 5.255)) / 2.25577e-5)

    return dict(
        valid_time=valid_time,
        pressure=np.array(pressures, dtype=float),
        temperature=np.array(temps, dtype=float),
        dewpoint=np.array(dews, dtype=float),
        windspeed=np.array(wspds, dtype=float),
        winddirection=np.array(wdirs, dtype=float),
        height_m=np.array(heights, dtype=float),
    )


def fetch_valley_temp() -> Optional[float]:
    """
    Current temperature at Golden Airport (CYGE) from the METAR —
    an actual observed valley-floor reading, not a model forecast.
    Falls back to Open-Meteo GFS if the METAR fetch fails.
    """
    try:
        r = requests.get(
            "https://aviationweather.gov/api/data/metar",
            params={"ids": "CYGE", "format": "json"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data and data[0].get("temp") is not None:
            return round(float(data[0]["temp"]), 1)
    except Exception as exc:
        log.warning("CYGE METAR fetch failed (%s) — falling back to Open-Meteo", exc)

    # Fallback: Open-Meteo GFS grid point for Golden
    r = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": GOLDEN_LAT, "longitude": GOLDEN_LON,
        "current": "temperature_2m", "timezone": TIMEZONE,
    }, timeout=15)
    r.raise_for_status()
    return round(float(r.json()["current"]["temperature_2m"]), 1)

def fetch_valley_temp_dummy() -> Optional[float]:
    """Current 2 m temperature at Golden from Open-Meteo (°C).

    Swap this function for a PurpleAir average if you have sensor API keys:
        return mean([sensor.temperature for sensor in nearby_sensors])
    """
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": GOLDEN_LAT,
            "longitude": GOLDEN_LON,
            "current": "temperature_2m",
            "timezone": TIMEZONE,
        }, timeout=15)
        r.raise_for_status()
        return float(r.json()["current"]["temperature_2m"])
    except Exception:
        return None


def fetch_mountain_temp(url: str) -> Optional[float]:
    """
    Parse the most recent air temperature (°C) from a mountainweather.ca
    fixed-width data table.  Both TOP_FTP.HTM and DOGSNOWSAFETY.HTM use
    the same leading column format: Month  Day  Time  AirTemp  ...
    """
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        temps = []
        for line in r.text.splitlines():
            parts = line.split()
            # Need at least 4 numeric tokens: month, day, time, temperature
            if len(parts) < 4:
                continue
            try:
                int(parts[0])   # month
                int(parts[1])   # day
                int(parts[2])   # HHMM time (may be 0, 100, 200 … 2300)
                temps.append(float(parts[3]))
            except (ValueError, IndexError):
                pass
        return temps[-1] if temps else None
    except Exception:
        return None


def fetch_venting_index() -> dict:
    """
    Scrape the BC Venting Index bulletin for Golden.

    Bulletin format (after the header):
        GOLDEN  NA/NA  NA  NA   54/FAIR  NA  NA   78/GOOD  NA  NA
    Columns:  [7 AM VI/CAT  WND  MXGHT] [4 PM VI/CAT  WND  MXGHT] [TOMORROW VI/CAT  WND  MXGHT]

    Returns:
        {'today_am': ViData, 'today_pm': ViData, 'tomorrow': ViData,
         'raw_line': str, 'bulletin_date': str}
    """
    result: dict = {
        "today_am": None, "today_pm": None, "tomorrow": None,
        "raw_line": None, "bulletin_date": None,
    }
    try:
        r = requests.get(VENTING_URL, timeout=15)
        r.raise_for_status()
        text = r.text

        # Capture bulletin issue date (e.g. "22-APRIL-2026")
        date_m = re.search(r'(\d{1,2}-[A-Z]+-\d{4})', text, re.IGNORECASE)
        if date_m:
            result["bulletin_date"] = date_m.group(1).upper()

        # Capture the full GOLDEN line verbatim
        line_m = re.search(r'(GOLDEN\s+.*)', text, re.IGNORECASE)
        if not line_m:
            return result
        raw = line_m.group(1).rstrip()
        result["raw_line"] = raw

        pairs = re.findall(r'(\d+|NA)/(POOR|FAIR|GOOD|NA)', raw, re.IGNORECASE)
        for key, (vi_str, cat) in zip(["today_am", "today_pm", "tomorrow"], pairs[:3]):
            if vi_str.upper() != "NA":
                result[key] = (int(vi_str), cat.upper())
    except Exception:
        pass
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  Inversion risk scoring  (0 – 6 points)
# ══════════════════════════════════════════════════════════════════════════════

def score_inversion(
    sounding: dict,
    vi: dict,
    valley_t: Optional[float],
    dog_t: Optional[float],
    ww_t: Optional[float],
) -> tuple:
    """
    Compute inversion risk score and plain-English hints.

    Components
    ----------
    A) Venting Index                   0–2 pts
    B) Valley–mountain ΔT vs. normal   0–2 pts
    C) Skew-T low-level lapse rate     0–2 pts

    Returns (score: int, hints: list[str])
    """
    score = 0
    hints = []

    # ── A) Venting Index ──────────────────────────────────────────────────────
    best_vi: ViData = vi.get("today_pm") or vi.get("today_am") or vi.get("tomorrow")
    if best_vi:
        vi_val, vi_cat = best_vi
        if vi_val <= 33:
            score += 2
            hints.append(f"Venting Index POOR ({vi_val}) — smoke traps easily in valleys")
        elif vi_val <= 54:
            score += 1
            hints.append(f"Venting Index FAIR ({vi_val}) — marginal smoke dispersal")
        else:
            hints.append(f"Venting Index GOOD ({vi_val}) — air should disperse well")
    else:
        hints.append("Venting Index unavailable — check https://envistaweb.env.gov.bc.ca")

    # ── B) Valley–mountain ΔT anomaly ────────────────────────────────────────
    # Pick the lower station (Dogtooth preferred) so we capture shallow inversions
    if valley_t is not None:
        if dog_t is not None:
            obs_delta = valley_t - dog_t          # positive = valley warmer than mountain
            exp_delta = NORMAL_LAPSE_RATE_C_PER_KM * (DOGTOOTH_ELEV_M - GOLDEN_ELEV_M) / 1000.0
            label = f"Dogtooth ({DOGTOOTH_ELEV_M} m)"
        elif ww_t is not None:
            obs_delta = valley_t - ww_t
            exp_delta = NORMAL_LAPSE_RATE_C_PER_KM * (WHITE_WALL_ELEV_M - GOLDEN_ELEV_M) / 1000.0
            label = f"White Wall ({WHITE_WALL_ELEV_M} m)"
        else:
            obs_delta = exp_delta = None
            label = ""

        if obs_delta is not None:
            anomaly = exp_delta - obs_delta  # > 0 means valley is warmer than normal lapse predicts
            if anomaly >= 4.0:
                score += 2
                hints.append(
                    f"Valley→{label} ΔT only {obs_delta:.1f}°C "
                    f"(normal ≈ {exp_delta:.1f}°C) — strong inversion signal"
                )
            elif anomaly >= 2.0:
                score += 1
                hints.append(
                    f"Valley→{label} ΔT {obs_delta:.1f}°C "
                    f"(normal ≈ {exp_delta:.1f}°C) — slight inversion possible"
                )
            else:
                hints.append(
                    f"Valley→{label} ΔT {obs_delta:.1f}°C ≈ normal ({exp_delta:.1f}°C)"
                )
    else:
        hints.append("No valley temperature — cannot check ΔT vs. mountain")

    # ── C) Skew-T: low-level lapse rate (1000–850 hPa) ───────────────────────
    p = sounding["pressure"]
    t = sounding["temperature"]
    mask = (p >= 850) & (p <= 1000)
    p_low = p[mask]
    t_low = t[mask]
    if len(p_low) >= 2:
        # Sort descending pressure = ascending altitude
        order = np.argsort(p_low)[::-1]
        t_bot = t_low[order[0]]   # lowest altitude (highest pressure)
        t_top = t_low[order[-1]]  # highest altitude (lowest pressure)
        if t_top > t_bot:
            score += 2
            hints.append(
                f"Sounding shows temperature INVERSION below 850 hPa "
                f"({t_bot:.1f}°C near surface → {t_top:.1f}°C aloft)"
            )
        elif (t_bot - t_top) < 2.0:
            score += 1
            hints.append(
                f"Sounding shows near-neutral lapse below 850 hPa "
                f"({t_bot:.1f}°C → {t_top:.1f}°C) — weak mixing"
            )
        else:
            hints.append(
                f"Sounding shows normal lapse below 850 hPa "
                f"({t_bot:.1f}°C → {t_top:.1f}°C)"
            )
    else:
        hints.append("Not enough sounding levels for Skew-T inversion check")

    return score, hints


def risk_label(score: int) -> tuple:
    """(label, hex_colour, emoji) for a given score."""
    if score >= 4:
        return "HIGH", "#B71C1C", "🔴"
    if score >= 2:
        return "MODERATE", "#E65100", "🟠"
    return "LOW", "#1B5E20", "🟢"


# ══════════════════════════════════════════════════════════════════════════════
#  Drawing helpers
# ══════════════════════════════════════════════════════════════════════════════

def draw_skewt(fig: plt.Figure, sounding: dict, rect: tuple) -> None:
    """Embed a MetPy Skew-T log-P diagram in *fig* at the given rect."""
    from matplotlib.transforms import blended_transform_factory

    skewt = SkewT(fig, rotation=45, rect=rect)
    ax = skewt.ax

    p = sounding["pressure"] * munits.hPa
    T = sounding["temperature"] * munits.degC
    Td = sounding["dewpoint"] * munits.degC
    wspd = sounding["windspeed"] * munits("km/h")
    wdir = sounding["winddirection"] * munits.degrees
    u, v = mpcalc.wind_components(wspd, wdir)

    # ── Profiles ──
    skewt.plot(p, T, "#D32F2F", linewidth=2.2, label="Temperature")
    skewt.plot(p, Td, "#1565C0", linewidth=1.8, linestyle="--", label="Dewpoint")

    # Wind barbs every other level (avoids clutter)
    skewt.plot_barbs(p[::2], u[::2].to("knots"), v[::2].to("knots"),
                     color="#333333", length=6)

    # ── Reference curves ──
    skewt.plot_dry_adiabats(alpha=0.20, colors="#6D4C41", linewidths=0.9)
    skewt.plot_moist_adiabats(alpha=0.18, colors="#388E3C", linewidths=0.8)
    skewt.plot_mixing_lines(alpha=0.15, colors="#0277BD", linewidths=0.7)

    # ── Station elevation reference lines ──
    trans = blended_transform_factory(ax.transAxes, ax.transData)
    stations_ref = [
        (GOLDEN_ELEV_M,    "Golden valley (785 m)", "#BF6000"),
        (DOGTOOTH_ELEV_M,  "Dogtooth (2060 m)",     "#1565C0"),
        (WHITE_WALL_ELEV_M,"White Wall (2450 m)",    "#6A1A9A"),
    ]
    for elev_m, label, colour in stations_ref:
        p_ref = elev_to_pressure(elev_m)
        ax.axhline(p_ref, color=colour, lw=1.2, ls=":", alpha=0.80, zorder=2)
        ax.text(0.01, p_ref, label, transform=trans, fontsize=7,
                color=colour, va="center", style="italic",
                bbox=dict(facecolor="white", alpha=0.65, edgecolor="none", pad=1))

    # ── Detect low-level inversion in the sounding ───────────────────────────
    p_arr = sounding["pressure"]
    t_arr = sounding["temperature"]
    mask  = (p_arr >= 750) & (p_arr <= 1000)
    p_low = p_arr[mask]
    t_low = t_arr[mask]
    inv_top_hpa = inv_bot_hpa = None
    if len(p_low) >= 2:
        order = np.argsort(p_low)[::-1]   # ascending altitude
        p_s, t_s = p_low[order], t_low[order]
        for i in range(len(t_s) - 1):
            if t_s[i + 1] > t_s[i]:       # T increases with altitude = inversion
                inv_bot_hpa = float(p_s[i])
                inv_top_hpa = float(p_s[i + 1])
                break                      # flag the lowest (shallowest) inversion

    if inv_bot_hpa is not None:
        # Shade the inversion layer
        ax.axhspan(inv_bot_hpa, inv_top_hpa,
                   color="#FF8F00", alpha=0.18, zorder=1)
        ax.text(0.99, (inv_bot_hpa + inv_top_hpa) / 2,
                "inversion layer", transform=trans,
                fontsize=7, color="#E65100", va="center", ha="right",
                fontweight="bold",
                bbox=dict(facecolor="white", alpha=0.75, edgecolor="#E65100",
                          pad=1.5, linewidth=0.8))

    # ── How-to-read annotation ───────────────────────────────────────────────
    guide = (
        "HOW TO READ THIS CHART\n"
        "Red line = temperature at each altitude\n"
        "Blue dashed = dewpoint (moisture)\n"
        "\n"
        "NORMAL: red line leans LEFT going up\n"
        "  (air gets colder with altitude)\n"
        "INVERSION: red line leans RIGHT going up\n"
        "  (air gets warmer — smoke gets trapped)"
    )
    ax.text(0.98, 0.02, guide,
            transform=ax.transAxes,
            fontsize=6.8, va="bottom", ha="right",
            color="#333",
            linespacing=1.5,
            bbox=dict(facecolor="white", alpha=0.88,
                      edgecolor="#BBBBBB", boxstyle="round,pad=0.5",
                      linewidth=0.8))

    # ── Axis limits & formatting ──
    ax.set_ylim(1050, 300)
    ax.set_xlim(-40, 40)
    valid_str = sounding["valid_time"].strftime("%a %b %-d  %H:%M") + " local"
    ax.set_title(
        f"GFS weather forecast — {valid_str}\n"
        f"Atmospheric sounding (temperature vs. altitude)",
        fontsize=8, fontweight="bold", linespacing=1.5)
    ax.set_xlabel("Temperature (°C)", fontsize=8)
    ax.set_ylabel("Pressure (hPa)", fontsize=8)
    ax.legend(fontsize=7.5, loc="upper right", framealpha=0.85)


def draw_venting_panel(ax: plt.Axes, vi: dict) -> None:
    """
    Compact venting panel: raw bulletin line in monospace at top,
    then one tight coloured label per time slot with a plain-language note.
    """
    VI_MEANING = {
        "POOR": "Smoke trapped in valley — avoid burning",
        "FAIR": "Some dispersal — burn with caution",
        "GOOD": "Air mixing well — normal conditions",
    }

    ax.set_facecolor("#2A2A3E")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # ── Source label ──────────────────────────────────────────────────────────
    ax.text(0.02, 1.0, "BC Ministry of Environment — official venting forecast",
            fontsize=6.5, color="#666", va="bottom", style="italic",
            transform=ax.transAxes)

    # ── Raw bulletin block ────────────────────────────────────────────────────
    date_str = vi.get("bulletin_date") or ""
    raw_line = vi.get("raw_line") or "GOLDEN  (unavailable)"
    header   = (
        f"           7 AM    4 PM  TOMORROW\n"
        f"{'─'*36}\n"
        f"{raw_line}"
    )
    ax.text(0.02, 0.99, header,
            fontfamily="monospace", fontsize=6.8, va="top", color="#222",
            linespacing=1.55,
            bbox=dict(facecolor="#EBEBEB", edgecolor="#BBBBBB",
                      boxstyle="round,pad=0.35", linewidth=0.7))

    ax.axhline(0.48, color="#555577", lw=0.8)

    # ── TONIGHT / TOMORROW buttons ────────────────────────────────────────────
    rows = [
        ("TONIGHT:",  vi.get("today_pm") or vi.get("today_am")),
        ("TOMORROW:", vi.get("tomorrow")),
    ]
    y_positions = [0.38, 0.16]
    for (row_label, data), y in zip(rows, y_positions):
        ax.text(0.03, y + 0.03, row_label,
                fontsize=9, color="white", va="center", fontweight="bold")
        if data:
            vi_val, cat = data
            col     = vi_colour(vi_val, cat)
            meaning = VI_MEANING.get(cat, "")
            ax.text(0.36, y + 0.03, f"  {vi_val}  {cat}  ",
                    fontsize=10, color="white", va="center", fontweight="bold",
                    bbox=dict(facecolor=col, edgecolor="none",
                              boxstyle="round,pad=0.3", alpha=0.92))
            ax.text(0.36, y - 0.09, meaning,
                    fontsize=7, color="#CCCCCC", va="top")
        else:
            ax.text(0.36, y + 0.03, "  N/A  ",
                    fontsize=10, color="#888", va="center",
                    bbox=dict(facecolor="#DDDDDD", edgecolor="none",
                              boxstyle="round,pad=0.3"))

    ax.text(0.5, 0.01, "0–33 POOR  •  34–54 FAIR  •  55–100 GOOD",
            fontsize=6, color="#9999BB", ha="center", va="bottom")


def draw_temp_profile(
    ax: plt.Axes,
    valley_t: Optional[float],
    dog_t: Optional[float],
    ww_t: Optional[float],
) -> None:
    """
    Horizontal thermometer layout — elevation on Y, temperature on X.

    For each non-valley station:
      • A thin bar from the EXPECTED temperature (open diamond on lapse line)
        to the OBSERVED temperature (filled circle) makes the gap unmissable.
      • The bar is orange when observed > expected (inversion) and blue when
        observed < expected (normal or super-adiabatic).
      • The gap is labelled in °C with a plain-English sign.

    The valley station anchors the lapse line; its bar runs from 0 °C to the
    observed temperature so it reads like a traditional thermometer.
    """
    ax.set_facecolor("#EFF4FB")
    ax.set_title("Valley vs. Mountain Temps", fontsize=9.5, fontweight="bold", pad=5)
    ax.set_ylabel("Elevation (m)", fontsize=8.5)
    ax.tick_params(labelsize=7.5)

    stations = []
    if valley_t is not None:
        stations.append((valley_t, GOLDEN_ELEV_M,    "Golden ~785 m",    "#BF6000"))
    if dog_t is not None:
        stations.append((dog_t,    DOGTOOTH_ELEV_M,  "Dogtooth 2060 m",  "#1565C0"))
    if ww_t is not None:
        stations.append((ww_t,     WHITE_WALL_ELEV_M,"White Wall 2450 m","#6A1A9A"))

    if not stations:
        ax.text(0.5, 0.5, "No station data available",
                ha="center", va="center", transform=ax.transAxes, color="#999")
        return

    all_obs   = [s[0] for s in stations]
    all_elevs = [s[1] for s in stations]

    # Compute expected temps at every station from valley anchor
    if valley_t is not None:
        all_expected = [expected_temp(valley_t, GOLDEN_ELEV_M, e) for e in all_elevs]
    else:
        all_expected = all_obs[:]   # no reference — nothing to compare

    # X-axis spans observed AND expected, with a little breathing room
    x_all = all_obs + all_expected
    x_lo  = min(x_all) - 3
    x_hi  = max(x_all) + 6          # extra room for labels on the right
    ax.set_xlim(x_lo, x_hi)

    y_lo  = min(all_elevs) - 250
    y_hi  = max(all_elevs) + 350
    ax.set_ylim(y_lo, y_hi)

    # ── Normal lapse rate reference line (continuous) ─────────────────────────
    if valley_t is not None:
        lapse_elevs = np.linspace(GOLDEN_ELEV_M, max(all_elevs) + 150, 80)
        lapse_temps = [expected_temp(valley_t, GOLDEN_ELEV_M, e) for e in lapse_elevs]
        ax.plot(lapse_temps, lapse_elevs, "--", color="#9E9E9E", lw=1.4, zorder=3,
                label="Normal decrease in temp with altitude")

    # ── Per-station thermometer bars and labels ───────────────────────────────
    bar_height = (y_hi - y_lo) * 0.04   # half-height of each horizontal bar

    for i, (obs, elev, name, colour) in enumerate(stations):
        exp = all_expected[i]
        is_valley = (elev == GOLDEN_ELEV_M)

        if is_valley:
            # Valley is the lapse-line anchor — no comparison bar, just dot + label
            ax.plot(obs, elev, "o", ms=10, color=colour, zorder=6,
                    markeredgecolor="white", markeredgewidth=1.2)
            ax.text(obs + 0.6, elev, f"{obs:.1f}°C  {name}  (anchor)",
                    fontsize=8, va="center", color=colour, fontweight="bold")
        else:
            gap = obs - exp   # positive = warmer than expected = inversion signal
            inv = gap > 0

            # Bar strictly from lapse-line (expected) to observed dot
            # Red = inversion (too warm aloft); grey = normal or super-adiabatic
            true_inv  = (valley_t is not None) and (obs > valley_t)
            if true_inv:
                bar_col, bar_alpha = "#C62828", 0.85   # red   — actual inversion
            elif inv:
                bar_col, bar_alpha = "#F48FB1", 0.80   # pink  — warmer than lapse but below valley
            else:
                bar_col, bar_alpha = "#78909C", 0.45   # grey  — normal
            ax.barh(elev, abs(gap), left=min(obs, exp),
                    height=bar_height, color=bar_col, alpha=bar_alpha,
                    zorder=4, edgecolor="none")

            # Filled circle at observed temperature
            ax.plot(obs, elev, "o", ms=10, color=colour, zorder=6,
                    markeredgecolor="white", markeredgewidth=1.2)

            # Gap label right of the rightmost point
            label_x      = max(obs, exp) + 0.4
            sign         = "▲ warmer than expected" if inv else "▼ cooler than expected"
            colour_label = "#C62828" if true_inv else ("#C2185B" if inv else "#546E7A")
            ax.text(label_x, elev,
                    f"{obs:.1f}°C  {name}\n{sign} by {abs(gap):.1f}°C",
                    fontsize=7.5, va="center", color=colour_label, fontweight="bold",
                    linespacing=1.4)

    # ── Valley temperature vertical line ─────────────────────────────────────
    # The KEY reference: any mountain dot to the RIGHT of this = true inversion
    if valley_t is not None:
        ax.axvline(valley_t, color="#BF6000", lw=1.8, ls="-.", alpha=0.85, zorder=3)
        ax.text(valley_t + 0.2, y_hi - (y_hi - y_lo) * 0.04,
                "← valley temp\n   (stations RIGHT of here = inversion)",
                fontsize=6.5, color="#BF6000", va="top", style="italic")

    # ── Plain-English verdict ─────────────────────────────────────────────────
    mountain_temps = [(obs, elev) for obs, elev, _, _ in stations
                      if elev != GOLDEN_ELEV_M]
    if valley_t is not None and mountain_temps:
        any_true_inversion  = any(obs > valley_t for obs, _ in mountain_temps)
        all_lapse_anomalous = all(
            (valley_t - obs) < NORMAL_LAPSE_RATE_C_PER_KM * (elev - GOLDEN_ELEV_M) / 1000.0 * 0.6
            for obs, elev in mountain_temps
        )

        if any_true_inversion:
            verdict      = "🚨 Dammit — there's an inversion.\n    Don't light that wood stove."
            verdict_col  = "#B71C1C"
            verdict_bg   = "#FFEBEE"
        elif all_lapse_anomalous:
            verdict      = "⚠️  No inversion yet, but lapse rate is\n    weak — smoke won't mix well."
            verdict_col  = "#E65100"
            verdict_bg   = "#FFF3E0"
        else:
            verdict      = "✅  No inversion. Normal lapse rate.\n    Air is mixing."
            verdict_col  = "#1B5E20"
            verdict_bg   = "#E8F5E9"

        ax.text(0.02, 0.97, verdict,
                transform=ax.transAxes, fontsize=8.5, fontweight="bold",
                color=verdict_col, va="top", linespacing=1.5,
                bbox=dict(facecolor=verdict_bg, edgecolor=verdict_col,
                          alpha=0.92, boxstyle="round,pad=0.4", linewidth=1.2))

    # ── Direction hint along the bottom ──────────────────────────────────────
    ax.text(0.01, 0.02,
            "Red bar = warmer than normal lapse rate (stability signal)",
            transform=ax.transAxes, fontsize=6.5, color="#777",
            va="bottom", style="italic")

    # ── Legend ────────────────────────────────────────────────────────────────
    inv_patch   = mpatches.Patch(color="#C62828", alpha=0.85,
                                 label="Warmer than valley = true inversion")
    pink_patch  = mpatches.Patch(color="#F48FB1", alpha=0.80,
                                 label="Warmer than lapse, cooler than valley = unstable")
    norm_patch  = mpatches.Patch(color="#78909C", alpha=0.45,
                                 label="Cooler than lapse = normal")
    lapse_line  = plt.Line2D([0], [0], ls="--", color="#9E9E9E", lw=1.4,
                             label="Normal decrease in temp with altitude")
    ax.legend(handles=[lapse_line, norm_patch, pink_patch, inv_patch],
              fontsize=6.5, loc="lower left", framealpha=0.85)


def draw_risk_banner(ax: plt.Axes, score: int, hints: list) -> None:
    """Full-width inversion risk assessment banner with stacked hint bullets."""
    label, colour, emoji = risk_label(score)
    ax.set_facecolor(colour)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    # Headline left
    ax.text(0.01, 0.92,
            f"EVENING INVERSION RISK:  {label}   ({score}/6)",
            fontsize=12, fontweight="bold", color="white", va="top")

    # Hint bullets — one per line, two columns if more than 2 hints
    bullet_lines = [f"• {h}" for h in hints]
    mid = (len(bullet_lines) + 1) // 2
    col1 = "\n".join(bullet_lines[:mid])
    col2 = "\n".join(bullet_lines[mid:])

    ax.text(0.01, 0.52, col1,
            fontsize=7.8, color="white", va="top", linespacing=1.6,
            path_effects=[pe.withStroke(linewidth=1.5, foreground=colour)])
    if col2:
        ax.text(0.51, 0.52, col2,
                fontsize=7.8, color="white", va="top", linespacing=1.6,
                path_effects=[pe.withStroke(linewidth=1.5, foreground=colour)])


# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Golden BC evening inversion infographic")
    parser.add_argument("--hour", type=int, default=21,
                        help="Local hour for Skew-T sounding (0–23, default 21)")
    parser.add_argument("--output", default="golden_inversion.png",
                        help="Output PNG path (default: golden_inversion.png)")
    args = parser.parse_args()

    # ── Fetch all data concurrently via threads ───────────────────────────────
    from concurrent.futures import ThreadPoolExecutor, as_completed

    tasks = {
        "sounding":   lambda: fetch_sounding(args.hour),
        "valley_t":   fetch_valley_temp,
        "dog_t":      lambda: fetch_mountain_temp(DOGTOOTH_URL),
        "ww_t":       lambda: fetch_mountain_temp(WHITE_WALL_URL),
        "vi":         fetch_venting_index,
    }

    results: dict = {}
    print("\nFetching data …")
    with ThreadPoolExecutor(max_workers=5) as ex:
        future_map = {ex.submit(fn): name for name, fn in tasks.items()}
        for fut in as_completed(future_map):
            name = future_map[fut]
            try:
                results[name] = fut.result()
                if name == "sounding":
                    vt = results["sounding"]["valid_time"].strftime("%H:%M")
                    print(f"  ✓ {name:<12} (valid {vt} local)")
                else:
                    val = results[name]
                    print(f"  ✓ {name:<12} → {val}")
            except Exception as exc:
                results[name] = None
                print(f"  ✗ {name:<12} failed: {exc}")
                if name == "sounding":
                    import traceback; traceback.print_exc()

    sounding = results["sounding"]
    valley_t = results["valley_t"]
    dog_t = results["dog_t"]
    ww_t = results["ww_t"]
    vi = results["vi"] or {"today_am": None, "today_pm": None, "tomorrow": None}

    if sounding is None:
        print("\n✗ Sounding fetch failed — cannot draw Skew-T. Check network / API.")
        return

    # ── Score ──────────────────────────────────────────────────────────────────
    score, hints = score_inversion(sounding, vi, valley_t, dog_t, ww_t)
    label, colour, emoji = risk_label(score)
    print(f"\nInversion risk: {emoji} {label}  (score {score}/6)")
    for h in hints:
        print(f"   • {h}")

    # ── Figure layout ──────────────────────────────────────────────────────────
    # Left column  (FORECASTS, narrow):   x 0.02 → 0.32
    # Right column (CURRENT DATA, wide):  x 0.36 → 0.97
    fig = plt.figure(figsize=(15, 10), facecolor="#1C1C2E")

    today_str = datetime.date.today().strftime("%A, %B %-d, %Y")
    fig.text(0.5, 0.980,
             f"Golden, BC — Temperature Inversion Outlook — {today_str}",
             ha="center", va="top", fontsize=14, fontweight="bold", color="white")

    fig.text(0.02, 0.945, "FORECASTS",
             fontsize=11, fontweight="bold", color="#90CAF9", va="top")
    fig.text(0.36, 0.945, "CURRENT CONDITIONS",
             fontsize=11, fontweight="bold", color="#A5D6A7", va="top")

    line = plt.Line2D([0.335, 0.335], [0.02, 0.94],
                      transform=fig.transFigure,
                      color="#444466", linewidth=1.0, linestyle="--")
    fig.add_artist(line)

    # ── LEFT: Venting panel ───────────────────────────────────────────────────
    ax_vi = fig.add_axes([0.02, 0.68, 0.30, 0.23])
    draw_venting_panel(ax_vi, vi)

    # ── LEFT: Skew-T — portrait (tall and narrow) ─────────────────────────────
    draw_skewt(fig, sounding, rect=(0.02, 0.14, 0.30, 0.52))

    # ── RIGHT: Temperature profile ────────────────────────────────────────────
    ax_tp = fig.add_axes([0.36, 0.14, 0.61, 0.77])
    draw_temp_profile(ax_tp, valley_t, dog_t, ww_t)

    # ── BOTTOM: Risk banner ────────────────────────────────────────────────────
    ax_risk = fig.add_axes([0.02, 0.02, 0.96, 0.11])
    draw_risk_banner(ax_risk, score, hints)

    # ── Save ───────────────────────────────────────────────────────────────────
    plt.savefig(args.output, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    print(f"\n✓  Saved → {args.output}")
    plt.show()


if __name__ == "__main__":
    main()
