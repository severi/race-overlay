#!/usr/bin/env python3
"""
race_overlay.py — elevation-profile "you are here" overlay PNGs for race videos.

Parses a .fit or .gpx activity file, plots the course elevation profile with
the event's checkpoints marked, stamps a "you are here" marker for each
requested position, and writes one transparent-background PNG per position —
ready to composite onto action-cam footage in DaVinci Resolve or any NLE.

Event specifics (checkpoints, course length, timezone) live in a TOML config;
see event.toml for a documented example.

Usage examples:
    # positions as race-km values
    python race_overlay.py my_run.fit --km 34 57.5 88

    # positions as clock times (race-local time)
    python race_overlay.py my_run.fit --ts 09:30:00 --ts 14:02:30

    # batch file: one position per line ("34", "km 34", "09:30:00",
    # optionally "| My label" after it), '#' starts a comment
    python race_overlay.py my_run.fit --pos-file positions.txt

    # explicit event config, custom size, no text label, plain linear rescale
    python race_overlay.py my_run.fit --event events/my-race.toml --km 50 \
        --width 1600 --height 500 --no-label --align linear

Requires: matplotlib, and fitparse for .fit input (Python 3.11+).
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import patheffects

# ============================================================================
# EVENT CONFIG — loaded from a TOML file (see event.toml for an example).
# The values below are fallback defaults; load_event() overrides them.
# ============================================================================

DEFAULT_EVENT_FILE = "event.toml"

# [[checkpoints]] — aid stations / pit stops. Each: label (drawn on the
# chart), km (official course distance), and optionally highlight = true
# (bigger star marker, e.g. for a halfway basecamp) and name (longer name
# used in the "Next ..." status line; defaults to label).
CHECKPOINTS: list = []
OFFICIAL_TOTAL_KM = 100.0   # official course length; the x-axis ends here
FINISH_LABEL = "FINISH"

# How to align the GPS track's distance to the official course-km scale:
#   "checkpoints" — piecewise-linear through rest stops auto-detected from
#                   the FIT laps (Garmin Ultra Run rest = manual lap at
#                   ~standstill), matched to the checkpoint kms. Most
#                   accurate. Falls back to "linear" if nothing is detected
#                   (e.g. GPX input).
#   "linear"      — scale the whole track so its total = OFFICIAL_TOTAL_KM.
#   "none"        — use raw recorded distance, no rescaling.
DISTANCE_ALIGN = "checkpoints"

# Rest-stop auto-detection (FIT only): a manual-trigger lap counts as a rest
# if its average speed is below REST_MAX_SPEED_MS and it lasted at least
# REST_MIN_DURATION_S. Detected rests within MATCH_TOLERANCE_KM (on the
# linearly pre-scaled track) of a checkpoint become alignment anchors.
REST_MAX_SPEED_MS = 0.5
REST_MIN_DURATION_S = 60
MATCH_TOLERANCE_KM = 2.5

# Timestamps inside FIT files are UTC; --ts inputs and the burned clock are
# race-local time, offset by this many hours from UTC.
LOCAL_UTC_OFFSET_HOURS = 0

# Camera clock offset from UTC, used by gopro_batch.py for files without
# timezone-aware metadata (GoPros; iPhones carry their own timezone).
CAMERA_UTC_OFFSET_HOURS = 0


def load_event(path=None):
    """Load an event TOML file into the module-level config globals."""
    import tomllib
    global CHECKPOINTS, OFFICIAL_TOTAL_KM, FINISH_LABEL, DISTANCE_ALIGN, \
        REST_MAX_SPEED_MS, REST_MIN_DURATION_S, MATCH_TOLERANCE_KM, \
        LOCAL_UTC_OFFSET_HOURS, CAMERA_UTC_OFFSET_HOURS

    path = path or DEFAULT_EVENT_FILE
    if not os.path.exists(path):
        sys.exit(f"Event config not found: {path}\n"
                 f"Copy/edit event.toml (see repository) and pass it with "
                 f"--event, or place it at ./{DEFAULT_EVENT_FILE}")
    with open(path, "rb") as f:
        cfg = tomllib.load(f)

    ev = cfg.get("event", {})
    OFFICIAL_TOTAL_KM = float(ev.get("total_km", OFFICIAL_TOTAL_KM))
    FINISH_LABEL = ev.get("finish_label", FINISH_LABEL)
    LOCAL_UTC_OFFSET_HOURS = float(ev.get("utc_offset_hours",
                                          LOCAL_UTC_OFFSET_HOURS))

    CHECKPOINTS = []
    for cp in cfg.get("checkpoints", []):
        CHECKPOINTS.append({
            "label": cp["label"],
            "name": cp.get("name", cp["label"]),
            "km": float(cp["km"]),
            "highlight": bool(cp.get("highlight", False)),
        })
    CHECKPOINTS.sort(key=lambda c: c["km"])

    al = cfg.get("alignment", {})
    DISTANCE_ALIGN = al.get("mode", DISTANCE_ALIGN)
    if DISTANCE_ALIGN not in ("checkpoints", "linear", "none"):
        sys.exit(f"{path}: invalid alignment.mode {DISTANCE_ALIGN!r} "
                 f"(expected checkpoints, linear or none)")
    REST_MAX_SPEED_MS = float(al.get("rest_max_speed_ms", REST_MAX_SPEED_MS))
    REST_MIN_DURATION_S = float(al.get("rest_min_duration_s",
                                       REST_MIN_DURATION_S))
    MATCH_TOLERANCE_KM = float(al.get("match_tolerance_km",
                                      MATCH_TOLERANCE_KM))

    CAMERA_UTC_OFFSET_HOURS = float(
        cfg.get("camera", {}).get("utc_offset_hours", CAMERA_UTC_OFFSET_HOURS))
    return cfg

# Output image geometry. 1280 px is 1/3 of a 4K frame width (and scales down
# crisply to 1/3 of 1080p). Override per-run with --width/--height/--dpi.
IMG_WIDTH_PX = 1280
IMG_HEIGHT_PX = 400
IMG_DPI = 100

OUTPUT_DIR = "overlays"
FILENAME_PREFIX = "overlay"

# Text label burned onto the image (toggle with --no-label / BURN_LABEL).
BURN_LABEL = True
# Also show watch clock time + elapsed & moving race time in the label
# (toggle with --no-time). Hours and minutes only, no seconds.
SHOW_TIME = True
# Moving time counts record-to-record gaps where speed >= this (m/s) ...
MOVING_SPEED_MS = 0.5
# ... skipping recording gaps longer than this (e.g. the watch reboot).
MOVING_MAX_GAP_S = 15

# Elevation smoothing window in meters of distance (GPS elevation is noisy).
SMOOTH_WINDOW_M = 150
# Separate (lighter) smoothing for the cumulative-gain stat: 100 m makes the
# full-course total land on the watch/official ~1350 m figure.
GAIN_SMOOTH_WINDOW_M = 100
# Plot roughly one profile vertex per this many meters (keeps files small).
PLOT_STEP_M = 40

# --- Style ------------------------------------------------------------------
# Design language (based on broadcast/telemetry-overlay best practice):
# monochrome white graphic + ONE accent color reserved for "you are here";
# done vs remaining course differ by opacity, not hue; soft drop shadows
# (not hard outlines) for legibility; optional dark gradient scrim behind
# the chart so it reads over any footage without hiding it.
MONO = "#ffffff"                    # the single graphic color
ACCENT = "#ff3b30"                  # position dot + progress bar accent
ALPHA_FILL_DONE = 0.55              # completed course fill
ALPHA_FILL_TODO = 0.14              # remaining course fill
ALPHA_LINE = 0.95                   # profile outline
LINE_WIDTH = 2.0
HERE_DOT_SIZE = 170                 # scatter size (pt^2)

# Scrim: bottom-weighted black gradient behind the chart (0 disables).
# Keeps text WCAG-legible over bright/busy footage; barely dims the video.
SCRIM_MAX_ALPHA = 0.35
SCRIM_CORNER_RADIUS = 0.055         # rounded corners, fraction of height

# First available family wins. All are neutral screen-first sans faces.
FONT_STACK = ["Inter", "SF Pro Display", "Avenir Next", "Helvetica Neue",
              "DejaVu Sans"]
FONT_SIZE_PIT = 12
FONT_SIZE_AXIS = 11.5
FONT_SIZE_LABEL_MAIN = 34           # the big km value
FONT_SIZE_LABEL_UNIT = 17           # the small "KM" next to it
FONT_SIZE_LABEL_SUB = 14            # "NEXT PS3 · 2.0 KM"
TRACKING = "\u2009"                # thin-space letter tracking for
                                    # uppercase micro-labels
ELEV_FLOOR_PAD_M = 15               # visual padding under the lowest point

# ============================================================================
# Track parsing
# ============================================================================


@dataclass
class Track:
    ts: list            # datetime (UTC, naive) per point
    dist_km: list       # raw recorded/derived cumulative distance, km
    ele: list           # elevation, m
    rests: list         # [(track_km, duration_s), ...] detected standstills


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_fit(path):
    try:
        import fitparse
    except ImportError:
        sys.exit("fitparse is required for .fit files: pip install fitparse")

    fit = fitparse.FitFile(path)
    ts, dist, ele, lat, lon = [], [], [], [], []
    for m in fit.get_messages("record"):
        v = {f.name: f.value for f in m}
        t = v.get("timestamp")
        e = v.get("enhanced_altitude")
        if e is None:
            e = v.get("altitude")
        if t is None or e is None:
            continue
        ts.append(t)
        dist.append(v.get("distance"))
        ele.append(e)
        lat.append(v.get("position_lat"))
        lon.append(v.get("position_long"))

    if any(d is None for d in dist):
        dist = _cumulative_from_coords(
            [x * (180 / 2**31) if x is not None else None for x in lat],
            [x * (180 / 2**31) if x is not None else None for x in lon],
        )
    dist_km = [d / 1000.0 for d in dist]

    # Rest stops: Garmin Ultra Run "rest" = manual lap at near-standstill.
    rests = []
    for m in fit.get_messages("lap"):
        v = {f.name: f.value for f in m}
        dur = v.get("total_timer_time") or 0
        d = v.get("total_distance") or 0
        if (
            v.get("lap_trigger") == "manual"
            and dur >= REST_MIN_DURATION_S
            and (d / dur if dur else 99) < REST_MAX_SPEED_MS
        ):
            i = min(bisect_left(ts, v["start_time"]), len(dist_km) - 1)
            rests.append((dist_km[i], dur))
    # merge back-to-back rests (< 500 m apart)
    merged = []
    for km, dur in sorted(rests):
        if merged and km - merged[-1][0] < 0.5:
            merged[-1] = (merged[-1][0], merged[-1][1] + dur)
        else:
            merged.append((km, dur))
    return Track(ts, dist_km, ele, merged)


def parse_gpx(path):
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    root = ET.parse(path).getroot()
    if root.tag.startswith("{"):  # tolerate GPX 1.0 namespace too
        ns["g"] = root.tag[1:].split("}")[0]
    ts, ele, lat, lon = [], [], [], []
    for pt in root.iter(f"{{{ns['g']}}}trkpt"):
        e = pt.find("g:ele", ns)
        t = pt.find("g:time", ns)
        if e is None:
            continue
        lat.append(float(pt.get("lat")))
        lon.append(float(pt.get("lon")))
        ele.append(float(e.text))
        if t is not None:
            ts.append(datetime.fromisoformat(t.text.replace("Z", "+00:00"))
                      .astimezone(timezone.utc).replace(tzinfo=None))
        else:
            ts.append(None)
    if any(t is None for t in ts):
        # partial/missing timestamps (e.g. an exported course file): disable
        # all time-based features rather than guessing
        ts = [None] * len(ts)
    dist = _cumulative_from_coords(lat, lon)
    return Track(ts, [d / 1000.0 for d in dist], ele, [])


def _cumulative_from_coords(lat, lon):
    dist, total, prev = [], 0.0, None
    for la, lo in zip(lat, lon):
        if la is not None and prev is not None:
            total += _haversine_m(prev[0], prev[1], la, lo)
        if la is not None:
            prev = (la, lo)
        dist.append(total)
    return dist


def load_track(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fit":
        track = parse_fit(path)
    elif ext == ".gpx":
        track = parse_gpx(path)
    else:
        sys.exit(f"Unsupported file type: {ext} (expected .fit or .gpx)")
    if len(track.dist_km) < 2 or track.dist_km[-1] <= 0:
        sys.exit(f"{path}: no usable distance data in the activity file")
    return track


def has_timestamps(track):
    return bool(track.ts) and track.ts[0] is not None


# ============================================================================
# Distance alignment: raw track km -> official course km
# ============================================================================


def build_km_mapper(track, mode):
    """Return (map_fn, anchors_report). map_fn: raw track km -> official km."""
    total = track.dist_km[-1]

    if mode == "none":
        return (lambda km: km), "raw recorded distance (no rescale)"

    linear = lambda km: km * OFFICIAL_TOTAL_KM / total  # noqa: E731
    if mode == "linear":
        return linear, (f"linear rescale x{OFFICIAL_TOTAL_KM / total:.4f} "
                        f"({total:.2f} -> {OFFICIAL_TOTAL_KM} km)")

    # mode == "checkpoints": anchor detected rests to the checkpoint kms
    anchors = [(0.0, 0.0)]
    matched = []
    used = set()
    for cp in CHECKPOINTS:
        off_km = cp["km"]
        best = None
        for km, dur in track.rests:
            err = abs(linear(km) - off_km)
            if km not in used and err < MATCH_TOLERANCE_KM and (
                    best is None or err < abs(linear(best) - off_km)):
                best = km
        if best is not None:
            used.add(best)
            anchors.append((best, off_km))
            matched.append(f"{cp['label']} {off_km}km <- track {best:.2f}km")
    anchors.append((total, OFFICIAL_TOTAL_KM))
    anchors.sort()

    if len(anchors) < 3:  # no usable rests detected -> plain linear
        return linear, "linear rescale (no rest stops detected to anchor on)"

    # the mapping must be strictly increasing in both track km and course km;
    # a rest matched to the wrong checkpoint would silently corrupt every
    # lookup, so refuse and fall back to linear instead
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x1 <= x0 or y1 <= y0:
            return linear, (
                "linear rescale (detected rests match checkpoints out of "
                "order — check the checkpoint kms / match_tolerance_km)")

    xs = [a[0] for a in anchors]
    ys = [a[1] for a in anchors]

    def piecewise(km):
        i = min(max(bisect_left(xs, km), 1), len(xs) - 1)
        x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
        return y0 + (km - x0) * (y1 - y0) / (x1 - x0)

    report = "piecewise via detected rest stops:\n    " + "\n    ".join(matched)
    return piecewise, report


# ============================================================================
# Profile preparation
# ============================================================================


def _smooth_elevation(kms, ele, window_m=None):
    """Distance-windowed running mean over the elevation samples."""
    half = (window_m or SMOOTH_WINDOW_M) / 2000.0  # km
    smooth, j0, j1, acc = [], 0, 0, 0.0
    n = len(kms)
    for i in range(n):
        while j1 < n and kms[j1] <= kms[i] + half:
            acc += ele[j1]
            j1 += 1
        while kms[j0] < kms[i] - half:
            acc -= ele[j0]
            j0 += 1
        smooth.append(acc / (j1 - j0))
    return smooth


def prepare_profile(track, map_fn):
    """Smoothed, downsampled (official_km, elevation) polyline."""
    kms = [map_fn(d) for d in track.dist_km]
    smooth = _smooth_elevation(kms, track.ele)

    step = PLOT_STEP_M / 1000.0
    px, py, nxt = [], [], 0.0
    for k, e in zip(kms, smooth):
        if k >= nxt:
            px.append(k)
            py.append(e)
            nxt = k + step
    px.append(kms[-1])
    py.append(smooth[-1])
    return px, py


def interp(px, py, x):
    i = min(max(bisect_left(px, x), 1), len(px) - 1)
    x0, x1, y0, y1 = px[i - 1], px[i], py[i - 1], py[i]
    if x1 == x0:
        return y0
    return y0 + (x - x0) * (y1 - y0) / (x1 - x0)


# ============================================================================
# Position inputs
# ============================================================================

TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def resolve_position(spec, track, map_fn):
    """'34', 'km 34', '09:30:00' or ISO datetime -> (official_km, source_desc)."""
    s = spec.strip().lower().removeprefix("km").strip()
    if TIME_RE.match(s) or "t" in s or "-" in s:
        if not has_timestamps(track):
            sys.exit(f"Cannot resolve '{spec}': the activity file has no "
                     f"timestamps — use --km positions instead")
        t = _parse_time_input(s, track)
        i = min(bisect_left(track.ts, t), len(track.ts) - 1)
        if i > 0 and abs((track.ts[i - 1] - t).total_seconds()) < abs(
                (track.ts[i] - t).total_seconds()):
            i -= 1
        gap = abs((track.ts[i] - t).total_seconds())
        km = map_fn(track.dist_km[i])
        note = f" (nearest point {gap:.0f}s away!)" if gap > 30 else ""
        return km, f"time {spec.strip()} -> km {km:.2f}{note}"
    km = float(s)
    return km, f"km {km:g}"


def _parse_time_input(s, track):
    off = timedelta(hours=LOCAL_UTC_OFFSET_HOURS)
    if TIME_RE.match(s):  # clock time on race day
        h, m, *rest = [int(x) for x in s.split(":")]
        sec = rest[0] if rest else 0
        day = (track.ts[0] + off).date()
        t = datetime(day.year, day.month, day.day, h, m, sec) - off
        # races can cross midnight: a clock time before the start means the
        # following day (e.g. 00:15 on a race that started at 07:00)
        if t < track.ts[0] and t + timedelta(days=1) <= track.ts[-1] \
                + timedelta(hours=1):
            t += timedelta(days=1)
        return t
    return datetime.fromisoformat(s) - off  # full local datetime


# ============================================================================
# Rendering
# ============================================================================


def _resolve_font():
    from matplotlib import font_manager
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for fam in FONT_STACK:
        if fam in installed:
            return fam
    return "DejaVu Sans"


FONT_FAMILY = _resolve_font()


def _tracked(s):
    """Letter-spaced uppercase micro-label ('NEXT' -> 'N E X T', thin)."""
    return TRACKING.join(s.upper())


def _text_fx(drop=2.0, alpha=0.55):
    """Soft drop shadow — legible over footage without a hard outline."""
    return [patheffects.SimplePatchShadow(offset=(0, -drop),
                                          shadow_rgbFace="black",
                                          alpha=alpha),
            patheffects.Normal()]


def _line_fx(drop=2.0, alpha=0.40):
    return [patheffects.SimpleLineShadow(offset=(0, -drop),
                                         shadow_color="black", alpha=alpha),
            patheffects.Normal()]


def _draw_scrim(fig, w, h):
    """Bottom-weighted black gradient behind the chart, rounded corners."""
    if SCRIM_MAX_ALPHA <= 0:
        return
    import numpy as np
    from matplotlib.patches import FancyBboxPatch
    bg = fig.add_axes([0, 0, 1, 1])
    bg.axis("off")
    rgba = np.zeros((256, 1, 4))
    rgba[:, 0, 3] = np.linspace(0, 1, 256) ** 1.4 * SCRIM_MAX_ALPHA
    im = bg.imshow(rgba, extent=(0, 1, 0, 1), aspect="auto", origin="upper",
                   interpolation="bilinear")
    r = SCRIM_CORNER_RADIUS * h / w  # circular corners despite aspect
    clip = FancyBboxPatch((0.002, 0.006), 0.996, 0.988,
                          boxstyle=f"round,pad=0,rounding_size={r}",
                          mutation_aspect=w / h, transform=bg.transAxes)
    im.set_clip_path(clip)


def render(px, py, here_km, out_path, label_text, args):
    w, h, dpi = args.width, args.height, args.dpi
    fig = plt.figure(figsize=(w / dpi, h / dpi), dpi=dpi)
    fig.patch.set_alpha(0)
    _draw_scrim(fig, w, h)
    label_room = 0.20 if label_text else 0.05
    ax = fig.add_axes([0.035, 0.155, 0.93, 0.80 - label_room])
    ax.set_facecolor("none")

    floor = min(py) - ELEV_FLOOR_PAD_M
    here_km = min(max(here_km, 0.0), px[-1])

    # split polyline at the current position
    i = bisect_left(px, here_km)
    here_ele = interp(px, py, here_km)
    dx, dy = px[:i] + [here_km], py[:i] + [here_ele]
    tx, ty = [here_km] + px[i:], [here_ele] + py[i:]

    # profile: one color, progress carried by opacity
    ax.fill_between(dx, floor, dy, color=MONO, alpha=ALPHA_FILL_DONE,
                    lw=0, zorder=2)
    ax.fill_between(tx, floor, ty, color=MONO, alpha=ALPHA_FILL_TODO,
                    lw=0, zorder=2)
    ax.plot(px, py, color=MONO, lw=LINE_WIDTH, alpha=ALPHA_LINE, zorder=3,
            solid_capstyle="round", path_effects=_line_fx())

    # baseline + accent progress bar along it
    ax.plot([0, OFFICIAL_TOTAL_KM], [floor, floor], color=MONO, lw=1.0,
            alpha=0.45, zorder=4, solid_capstyle="round")
    if here_km > 0.3:
        ax.plot([0, here_km], [floor, floor], color=ACCENT, lw=3.0,
                alpha=0.95, zorder=5, solid_capstyle="round")

    # pit-stop markers: hairline + dot + tracked micro-label
    marks = [(c["label"], c["km"], c["highlight"]) for c in CHECKPOINTS]
    marks.append((FINISH_LABEL, OFFICIAL_TOTAL_KM, False))
    for label, km, highlight in marks:
        e = interp(px, py, km)
        ax.plot([km, km], [floor, e], color=MONO, lw=0.8, alpha=0.22,
                zorder=4)
        ax.scatter([km], [e], s=110 if highlight else 26,
                   marker="*" if highlight else "o", color=MONO, alpha=0.95,
                   lw=0, zorder=6)
        # anchor the label above the highest terrain within +-3 km so it
        # never sits inside the profile
        lo, hi = bisect_left(px, km - 3), bisect_left(px, km + 3)
        local_top = max(py[lo:hi] or [e])
        txt = _tracked(label)
        ha = "right" if km > OFFICIAL_TOTAL_KM - 3 else "center"
        ax.annotate(txt, (km, local_top), xytext=(5 if ha == "right" else 0, 9),
                    textcoords="offset points", ha=ha, color=MONO, alpha=1.0,
                    fontsize=FONT_SIZE_PIT, fontweight="bold",
                    family=FONT_FAMILY, path_effects=_text_fx(2.0, 0.65),
                    zorder=7, annotation_clip=False)

    # you-are-here: accent glow + dot with white ring
    ax.scatter([here_km], [here_ele], s=HERE_DOT_SIZE * 2.8, color=ACCENT,
               alpha=0.28, lw=0, zorder=7)
    ax.scatter([here_km], [here_ele], s=HERE_DOT_SIZE, color=ACCENT,
               edgecolors=MONO, linewidths=1.8, zorder=8)

    # bottom row: end ticks + centered status strip
    ax.annotate("0", (0, floor), xytext=(0, -15), textcoords="offset points",
                ha="center", color=MONO, alpha=0.95, fontsize=FONT_SIZE_AXIS,
                family=FONT_FAMILY, path_effects=_text_fx(2.0, 0.65),
                annotation_clip=False)
    ax.annotate(f"{OFFICIAL_TOTAL_KM:.0f} KM", (OFFICIAL_TOTAL_KM, floor),
                xytext=(12, -15), textcoords="offset points", ha="right",
                color=MONO, alpha=0.95, fontsize=FONT_SIZE_AXIS,
                family=FONT_FAMILY, path_effects=_text_fx(2.0, 0.65),
                annotation_clip=False)
    if label_text and label_text.get("bottom"):
        ax.annotate(_tracked(label_text["bottom"]),
                    (OFFICIAL_TOTAL_KM / 2, floor), xytext=(0, -15),
                    textcoords="offset points", ha="center", color=MONO,
                    alpha=1.0, fontsize=FONT_SIZE_LABEL_SUB,
                    fontweight="bold", family=FONT_FAMILY,
                    path_effects=_text_fx(2.0, 0.65), annotation_clip=False)

    if label_text:
        vt = fig.text(0.035, 0.955, label_text["value"], ha="left", va="top",
                      color=MONO, fontsize=FONT_SIZE_LABEL_MAIN,
                      fontweight="heavy", family=FONT_FAMILY,
                      path_effects=_text_fx(2.5, 0.6))
        fig.canvas.draw()
        bb = vt.get_window_extent(fig.canvas.get_renderer())
        if label_text.get("unit"):
            fig.text(bb.x1 / w + 0.008, bb.y0 / h, label_text["unit"],
                     ha="left", va="bottom", color=MONO, alpha=0.95,
                     fontsize=FONT_SIZE_LABEL_UNIT, fontweight="bold",
                     family=FONT_FAMILY, path_effects=_text_fx(2.0, 0.65))
        if label_text.get("gain"):
            fig.text(0.037, bb.y0 / h - 0.025, _tracked(label_text["gain"]),
                     ha="left", va="top", color=MONO, alpha=1.0,
                     fontsize=FONT_SIZE_LABEL_SUB, fontweight="bold",
                     family=FONT_FAMILY, path_effects=_text_fx(2.0, 0.65))
        for row, line in enumerate(label_text.get("right") or []):
            fig.text(0.965, 0.955 - row * 0.062, _tracked(line), ha="right",
                     va="top", color=MONO, alpha=1.0,
                     fontsize=FONT_SIZE_LABEL_SUB, fontweight="bold",
                     family=FONT_FAMILY, path_effects=_text_fx(2.0, 0.65))

    ax.set_xlim(-0.5, OFFICIAL_TOTAL_KM + 0.5)
    ax.set_ylim(floor - 5, max(py) + 30)
    ax.axis("off")

    fig.savefig(out_path, transparent=True, dpi=dpi)
    plt.close(fig)


def _hm(seconds):
    mins = max(int(seconds // 60), 0)
    return f"{mins // 60}h {mins % 60:02d}m"


def make_label(here_km, when_utc=None, start_utc=None, moving_s=None,
               gain_m=None, custom=None):
    """Label layout dict for render():

      value/unit — big top-left ('34' / 'KM'; custom text replaces both)
      gain       — small top-left, under the value ('+812 m')
      right      — stacked top-right lines (elapsed / moving time)
      bottom     — status strip under the graph ('11:36 · Next PS3 · 2.0 km')
    """
    if custom:
        value, unit = custom, None
    elif abs(here_km - round(here_km)) < 0.05:
        value, unit = f"{here_km:.0f}", "KM"
    else:
        value, unit = f"{here_km:.1f}", "KM"

    gain = f"+{gain_m:.0f} m" if gain_m is not None else None

    right = []
    bottom = []
    if when_utc is not None and start_utc is not None:
        right.append(f"elapsed {_hm((when_utc - start_utc).total_seconds())}")
        if moving_s is not None:
            right.append(f"moving {_hm(moving_s)}")
        clock = when_utc + timedelta(hours=LOCAL_UTC_OFFSET_HOURS)
        bottom.append(f"{clock:%H:%M}")

    ahead = [c for c in CHECKPOINTS if c["km"] > here_km + 0.05]
    remaining = OFFICIAL_TOTAL_KM - here_km
    if ahead:
        nxt = ahead[0]
        bottom.append(f"Next {nxt['name']} · {nxt['km'] - here_km:.1f} km")
    elif remaining > 0.05:
        bottom.append(f"Next {FINISH_LABEL} · {remaining:.1f} km")
    else:
        bottom.append(FINISH_LABEL)

    return {"value": value, "unit": unit, "gain": gain,
            "right": right, "bottom": " · ".join(bottom)}


def make_time_lookup(track, map_fn):
    """official km -> timestamp (UTC) of the nearest track point."""
    mkms = [map_fn(d) for d in track.dist_km]

    def at(km):
        i = min(bisect_left(mkms, km), len(mkms) - 1)
        return track.ts[i]

    return at


def make_moving_lookup(track):
    """timestamp (UTC) -> cumulative moving seconds up to that moment."""
    cum, c = [0.0], 0.0
    for i in range(1, len(track.ts)):
        dt = (track.ts[i] - track.ts[i - 1]).total_seconds()
        if 0 < dt <= MOVING_MAX_GAP_S:
            speed = (track.dist_km[i] - track.dist_km[i - 1]) * 1000 / dt
            if speed >= MOVING_SPEED_MS:
                c += dt
        cum.append(c)

    def at(t):
        i = min(bisect_left(track.ts, t), len(track.ts) - 1)
        return cum[i]

    return at


def make_gain_lookup(track, map_fn):
    """official km -> cumulative elevation gain (m) up to that point.

    Uses the smoothed elevation so GPS jitter doesn't inflate the total.
    """
    kms = [map_fn(d) for d in track.dist_km]
    smooth = _smooth_elevation(kms, track.ele, GAIN_SMOOTH_WINDOW_M)
    cum, c = [0.0], 0.0
    for i in range(1, len(smooth)):
        c += max(smooth[i] - smooth[i - 1], 0.0)
        cum.append(c)

    def at(km):
        i = min(bisect_left(kms, km), len(kms) - 1)
        return cum[i]

    return at


# ============================================================================
# CLI
# ============================================================================


def read_pos_file(path):
    specs = []
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if "|" in line:
                spec, custom = [s.strip() for s in line.split("|", 1)]
                specs.append((spec, custom))
            else:
                specs.append((line, None))
    return specs


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("activity", help=".fit or .gpx activity file")
    ap.add_argument("--km", nargs="+", default=[], metavar="KM",
                    help="position(s) as official-course km, e.g. 34 57.5")
    ap.add_argument("--ts", nargs="+", default=[], metavar="TIME",
                    help="position(s) as local clock time HH:MM[:SS] "
                         "or full ISO datetime")
    ap.add_argument("--pos-file", help="file with one position per line")
    ap.add_argument("--out-dir", default=OUTPUT_DIR)
    ap.add_argument("--width", type=int, default=IMG_WIDTH_PX)
    ap.add_argument("--height", type=int, default=IMG_HEIGHT_PX)
    ap.add_argument("--dpi", type=int, default=IMG_DPI)
    ap.add_argument("--event", default=None, metavar="TOML",
                    help=f"event config file (default: {DEFAULT_EVENT_FILE})")
    ap.add_argument("--align", choices=["checkpoints", "linear", "none"],
                    default=None,
                    help="distance alignment mode (default: from event config)")
    ap.add_argument("--no-label", action="store_true",
                    help="don't burn the text label onto the images")
    ap.add_argument("--no-time", action="store_true",
                    help="omit the clock/elapsed-time line from the label")
    args = ap.parse_args()
    load_event(args.event)
    if args.align is None:
        args.align = DISTANCE_ALIGN

    specs = [(s, None) for s in args.km] + [(s, None) for s in args.ts]
    if args.pos_file:
        specs += read_pos_file(args.pos_file)
    if not specs:
        ap.error("no positions given (use --km, --ts and/or --pos-file)")

    track = load_track(args.activity)
    span = (f", {track.ts[0]} -> {track.ts[-1]} UTC"
            if has_timestamps(track) else ", no timestamps")
    print(f"Track: {len(track.dist_km)} points, "
          f"{track.dist_km[-1]:.2f} km recorded{span}")
    if track.rests:
        print("Detected standstills (rest laps):")
        for km, dur in track.rests:
            print(f"  track km {km:7.2f}  {dur/60:5.1f} min")

    map_fn, report = build_km_mapper(track, args.align)
    print(f"Distance alignment: {report}")

    px, py = prepare_profile(track, map_fn)
    os.makedirs(args.out_dir, exist_ok=True)

    burn = BURN_LABEL and not args.no_label
    show_time = SHOW_TIME and not args.no_time and has_timestamps(track)
    time_at = make_time_lookup(track, map_fn) if show_time else None
    moving_at = make_moving_lookup(track) if show_time else None
    gain_at = make_gain_lookup(track, map_fn)
    for spec, custom in specs:
        km, desc = resolve_position(spec, track, map_fn)
        label = None
        if burn:
            when = time_at(km) if show_time else None
            start = track.ts[0] if show_time else None
            moving = moving_at(when) if show_time else None
            label = make_label(km, when, start, moving, gain_at(km), custom)
        name = f"{FILENAME_PREFIX}_km{km:05.1f}"
        if custom:
            name += "_" + re.sub(r"[^A-Za-z0-9]+", "-", custom).strip("-").lower()
        out = os.path.join(args.out_dir, name + ".png")
        render(px, py, km, out, label, args)
        print(f"  {desc:45s} -> {out}")


if __name__ == "__main__":
    main()
