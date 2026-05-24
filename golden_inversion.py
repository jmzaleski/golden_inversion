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
WHITE_WALL_ELEV_M = 2450     # White Wall Remote Weather Station

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
        {'today_am': ViData, 'today_pm': ViData, 'tomorrow': ViData}
    """
    result: dict = {"today_am": None, "today_pm": None, "tomorrow": None}
    try:
        r = requests.get(VENTING_URL, timeout=15)
        r.raise_for_status()
        m = re.search(r'GOLDEN\s+(.*)', r.text, re.IGNORECASE)
        if not m:
            return result
        line = m.group(1)
        # Each slot is  number/WORD  or  NA/NA
        pairs = re.findall(r'(\d+|NA)/(POOR|FAIR|GOOD|NA)', line, re.IGNORECASE)
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
    # Uses blended transform: x in axes coords, y in data (pressure) coords
    trans = blended_transform_factory(ax.transAxes, ax.transData)
    stations_ref = [
        (GOLDEN_ELEV_M, "Golden valley (785 m)", "#BF6000"),
        (DOGTOOTH_ELEV_M, "Dogtooth (2060 m)", "#1565C0"),
        (WHITE_WALL_ELEV_M, "White Wall (2450 m)", "#6A1A9A"),
    ]
    for elev_m, label, colour in stations_ref:
        p_ref = elev_to_pressure(elev_m)
        ax.axhline(p_ref, color=colour, lw=1.2, ls=":", alpha=0.80, zorder=2)
        ax.text(0.01, p_ref, label, transform=trans, fontsize=7,
                color=colour, va="center", style="italic",
                bbox=dict(facecolor="white", alpha=0.65, edgecolor="none", pad=1))

    # ── Axis limits & formatting ──
    ax.set_ylim(1050, 300)
    ax.set_xlim(-40, 40)
    valid_str = sounding["valid_time"].strftime("%a %b %-d  %H:%M") + " local"
    ax.set_title(f"GFS Sounding — {valid_str}", fontsize=9.5, fontweight="bold")
    ax.set_xlabel("Temperature (°C)", fontsize=8.5)
    ax.set_ylabel("Pressure (hPa)", fontsize=8.5)
    ax.legend(fontsize=8, loc="upper right", framealpha=0.85)


def draw_venting_panel(ax: plt.Axes, vi: dict) -> None:
    """Compact Venting Index display for today (AM, PM) and tomorrow."""
    ax.set_facecolor("#F8F8F8")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_title("BC Venting Index — Golden", fontsize=9.5, fontweight="bold", pad=5)

    slots = [
        ("Today  7 AM", vi["today_am"]),
        ("Today  4 PM", vi["today_pm"]),
        ("Tomorrow 4 PM", vi["tomorrow"]),
    ]
    y_centres = [0.80, 0.50, 0.20]

    for yc, (slot_label, data) in zip(y_centres, slots):
        ax.text(0.05, yc + 0.14, slot_label, fontsize=8,
                color="#555", va="bottom", fontweight="bold")
        box = mpatches.FancyBboxPatch(
            (0.05, yc - 0.08), 0.90, 0.21,
            boxstyle="round,pad=0.01",
            facecolor=(vi_colour(data[0], data[1]) if data else "#AAAAAA"),
            edgecolor="white", alpha=0.88,
            transform=ax.transAxes, clip_on=False,
        )
        ax.add_patch(box)
        if data:
            vi_val, cat = data
            ax.text(0.50, yc + 0.025, f"{vi_val}  {cat}",
                    fontsize=14, color="white", ha="center", va="center",
                    fontweight="bold",
                    path_effects=[pe.withStroke(linewidth=2.5, foreground="#333")])
        else:
            ax.text(0.50, yc + 0.025, "N/A", fontsize=12,
                    color="#EEEEEE", ha="center", va="center")

    ax.text(0.5, 0.01, "0–33 POOR  •  34–54 FAIR  •  55–100 GOOD",
            fontsize=6.5, color="#777", ha="center", va="bottom")


def draw_temp_profile(
    ax: plt.Axes,
    valley_t: Optional[float],
    dog_t: Optional[float],
    ww_t: Optional[float],
) -> None:
    """
    Temperature vs. elevation profile.
    Observed dots connected by a solid line; normal-lapse reference dashed.
    Orange shading marks the region where observed air is warmer than
    the normal lapse rate predicts (inversion signal).
    """
    ax.set_facecolor("#EFF4FB")
    ax.set_title("Temp Profile vs. Normal Lapse", fontsize=9.5, fontweight="bold", pad=5)
    ax.set_ylabel("Elevation (m)", fontsize=8.5)
    ax.set_xlabel("Temperature (°C)", fontsize=8.5)
    ax.tick_params(labelsize=7.5)

    stations = []
    if valley_t is not None:
        stations.append((valley_t, GOLDEN_ELEV_M, "Golden\n~785 m", "#BF6000"))
    if dog_t is not None:
        stations.append((dog_t, DOGTOOTH_ELEV_M, "Dogtooth\n2060 m", "#1565C0"))
    if ww_t is not None:
        stations.append((ww_t, WHITE_WALL_ELEV_M, "White Wall\n2450 m", "#6A1A9A"))

    if not stations:
        ax.text(0.5, 0.5, "No mountain data available",
                ha="center", va="center", transform=ax.transAxes, color="#999")
        return

    all_temps = [s[0] for s in stations]
    t_min = min(all_temps) - 4
    t_max = max(all_temps) + 4
    ax.set_xlim(t_min, t_max)
    ax.set_ylim(550, 2700)

    # ── Normal lapse line from valley ────────────────────────────────────────
    if valley_t is not None:
        elev_line = np.linspace(GOLDEN_ELEV_M, WHITE_WALL_ELEV_M + 100, 80)
        temp_line = [expected_temp(valley_t, GOLDEN_ELEV_M, e) for e in elev_line]
        ax.plot(temp_line, elev_line, "--", color="#9E9E9E", lw=1.5,
                label=f"Normal lapse ({NORMAL_LAPSE_RATE_C_PER_KM}°C/km)", zorder=3)

        # Shade inversion zone: between normal-lapse line and observed profile
        # Build a common elevation grid and interpolate observed temps
        obs_elevs = [s[1] for s in stations]
        obs_temps = [s[0] for s in stations]
        if len(obs_elevs) >= 2:
            obs_interp = np.interp(elev_line, sorted(obs_elevs),
                                   [t for _, t in sorted(zip(obs_elevs, obs_temps))])
            normal_interp = np.array(temp_line)
            # Inversion where observed > normal (valley is warmer than it should be)
            inv_mask = obs_interp > normal_interp
            if inv_mask.any():
                ax.fill_betweenx(elev_line, normal_interp, obs_interp,
                                 where=inv_mask, color="#FF8F00", alpha=0.30,
                                 label="Inversion zone", zorder=2)

    # ── Observed profile ─────────────────────────────────────────────────────
    obs_temps = [s[0] for s in stations]
    obs_elevs = [s[1] for s in stations]
    obs_colours = [s[3] for s in stations]

    ax.plot(obs_temps, obs_elevs, "-", color="#212121", lw=2.2, zorder=5)
    for temp, elev, label, colour in stations:
        ax.plot(temp, elev, "o", ms=11, color=colour, zorder=6,
                markeredgecolor="white", markeredgewidth=1.2)
        ha = "left" if temp <= np.mean(all_temps) else "right"
        xoff = 0.4 if ha == "left" else -0.4
        ax.text(temp + xoff, elev, f"{temp:.1f}°C  {label}",
                fontsize=7.5, va="center", ha=ha, color=colour, fontweight="bold")

    ax.axvline(0, color="#BDBDBD", lw=0.8, ls="--", alpha=0.6)
    ax.grid(axis="x", alpha=0.2)
    ax.legend(fontsize=7.5, loc="upper right", framealpha=0.85)


def draw_risk_banner(ax: plt.Axes, score: int, hints: list) -> None:
    """Full-width inversion risk assessment banner."""
    label, colour, emoji = risk_label(score)
    ax.set_facecolor(colour)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    ax.text(0.01, 0.78,
            f"{emoji}  EVENING INVERSION RISK:  {label}   ({score}/6 points)",
            fontsize=13, fontweight="bold", color="white", va="top",
            path_effects=[pe.withStroke(linewidth=3, foreground=colour)])

    summary = "   •   ".join(hints)
    # Wrap long summaries manually
    max_chars = 140
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "…"
    ax.text(0.01, 0.32, summary, fontsize=8, color="#FFECB3", va="top")


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
    # All positions in figure-fraction coordinates [left, bottom, width, height]
    fig = plt.figure(figsize=(15, 10), facecolor="#1C1C2E")

    today_str = datetime.date.today().strftime("%A, %B %-d, %Y")
    fig.text(0.5, 0.975,
             f"Golden, BC — Evening Temperature-Inversion Outlook — {today_str}",
             ha="center", va="top", fontsize=15, fontweight="bold", color="white")

    # Sub-heading with current readings
    def _fmt(v, unit="°C"):
        return f"{v:.1f}{unit}" if v is not None else "N/A"

    vi_now = vi.get("today_pm") or vi.get("today_am")
    vi_str = f"{vi_now[0]} ({vi_now[1]})" if vi_now else "N/A"
    fig.text(
        0.5, 0.940,
        f"Valley: {_fmt(valley_t)}   •   Dogtooth {DOGTOOTH_ELEV_M} m: {_fmt(dog_t)}"
        f"   •   White Wall {WHITE_WALL_ELEV_M} m: {_fmt(ww_t)}"
        f"   •   Venting Index: {vi_str}",
        ha="center", va="top", fontsize=9.5, color="#CCCCDD",
    )

    # Skew-T — left 60 %, most of vertical space
    draw_skewt(fig, sounding, rect=(0.03, 0.14, 0.56, 0.77))

    # Venting Index — top-right quadrant
    ax_vi = fig.add_axes([0.63, 0.55, 0.35, 0.36])
    draw_venting_panel(ax_vi, vi)

    # Temp profile — bottom-right quadrant
    ax_tp = fig.add_axes([0.63, 0.14, 0.35, 0.37])
    draw_temp_profile(ax_tp, valley_t, dog_t, ww_t)

    # Risk banner — full-width bottom strip
    ax_risk = fig.add_axes([0.03, 0.03, 0.95, 0.09])
    draw_risk_banner(ax_risk, score, hints)

    # ── Save ───────────────────────────────────────────────────────────────────
    plt.savefig(args.output, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    print(f"\n✓  Saved → {args.output}")
    plt.show()


if __name__ == "__main__":
    main()
