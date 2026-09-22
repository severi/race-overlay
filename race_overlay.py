#!/usr/bin/env python3
"""
race_overlay.py — "you are here" overlay PNGs for race videos.

Parses a .fit or .gpx activity file, draws the course as an elevation
profile, as a 2-D route map or — for lap races — as the profile of one loop
with a lap counter (event.view / --view) with the event's checkpoints marked, stamps a "you are here" marker for each requested
position, and writes one transparent-background PNG per position — ready to
composite onto action-cam footage in DaVinci Resolve or any NLE.

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

    # 2-D route map instead of the elevation profile
    python race_overlay.py my_run.fit --view map --km 20

    # lap race (laps found from the GPS track; see the [laps] config table)
    python race_overlay.py my_run.fit --event events/my-lap-race.toml \
        --ts 12:48

Requires: matplotlib, and fitparse for .fit input (Python 3.11+).
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from bisect import bisect_left, bisect_right
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
VIEWS = ("profile", "map", "laps")

# Compass point -> (dx, dy) label offset in points, and text alignment.
LABEL_OFFSETS = {
    "n": ((0, 9), "center", "bottom"), "s": ((0, -9), "center", "top"),
    "e": ((9, 0), "left", "center"), "w": ((-9, 0), "right", "center"),
    "ne": ((7, 7), "left", "bottom"), "nw": ((-7, 7), "right", "bottom"),
    "se": ((7, -7), "left", "top"), "sw": ((-7, -7), "right", "top"),
}

# [[checkpoints]] — aid stations / pit stops. Each: label (drawn on the
# chart), km (official course distance), and optionally highlight = true
# (bigger star marker, e.g. for a halfway basecamp) and name (longer name
# used in the "Next ..." status line; defaults to label).
CHECKPOINTS: list = []
OFFICIAL_TOTAL_KM = 100.0   # official course length; the x-axis ends here
FINISH_LABEL = "FINISH"
START_LABEL = "START"       # map view only, drawn when start != finish
FINISH_LABEL_POS = "n"      # map view: compass side of the finish label

# What to draw: "profile" (elevation profile, distance on the x-axis),
# "map" (2-D route seen from above) or "laps" (lap race: the profile of ONE
# loop with the marker going round it, lap counter, countdown). Override
# per-run with --view.
VIEW = "profile"

# [laps] — lap races (as many loops as you can in a time limit, or a fixed
# number of loops). Laps are counted from the GPS track: every return to
# the start point (the first fix) is a lap boundary; the watch's lap button
# is ignored (a missed press would merge two loops).
LAP_TIME_LIMIT_H = None     # race time limit, hours (None: no countdown)
LAP_KM = None               # official loop length (None: median recorded)
LAP_START_RADIUS_M = 30.0   # a pass this close to the start point = boundary
LAP_MIN_S = 120.0           # ... unless the previous one was under this ago
# The loop graphic: "profile" (flat elevation profile of one loop) or "3d"
# (the loop as a ribbon in space over its ground outline — track AND
# elevation; needs GPS). The 3-D camera: azimuth None = side-on to the
# loop's long axis with the climb running left to right.
LAP_STYLE = "profile"
LAP_VIEW_AZIMUTH_DEG = None
LAP_VIEW_ROTATE_DEG = 20.0  # turned this far from side-on (auto azimuth only)
LAP_VIEW_TILT_DEG = 50.0    # 0 = from the side, 90 = from straight above
LAP_Z_EXAGGERATION = 2.0
# Does the lap in progress when the time runs out still count? If not, it
# is drawn as "not counted" from the moment the limit passes.
LATE_LAP_COUNTS = False

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
# Seconds the camera clock is BEHIND the watch (added to camera times).
# Cameras synced from a phone drift; find it from a clip where you visibly
# stop or start and compare with the track (see README).
CAMERA_CLOCK_BEHIND_S = 0.0


def load_event(path=None):
    """Load an event TOML file into the module-level config globals."""
    import tomllib
    global CHECKPOINTS, OFFICIAL_TOTAL_KM, FINISH_LABEL, START_LABEL, \
        FINISH_LABEL_POS, VIEW, LAP_TIME_LIMIT_H, LAP_KM, \
        LAP_START_RADIUS_M, LAP_MIN_S, LATE_LAP_COUNTS, LAP_STYLE, \
        LAP_VIEW_AZIMUTH_DEG, LAP_VIEW_ROTATE_DEG, LAP_VIEW_TILT_DEG, \
        LAP_Z_EXAGGERATION, \
        GAIN_SMOOTH_WINDOW_M, DISTANCE_ALIGN, REST_MAX_SPEED_MS, \
        REST_MIN_DURATION_S, MATCH_TOLERANCE_KM, LOCAL_UTC_OFFSET_HOURS, CAMERA_UTC_OFFSET_HOURS, \
        CAMERA_CLOCK_BEHIND_S

    path = path or DEFAULT_EVENT_FILE
    if not os.path.exists(path):
        sys.exit(f"Event config not found: {path}\n"
                 f"Copy/edit event.toml (see repository) and pass it with "
                 f"--event, or place it at ./{DEFAULT_EVENT_FILE}")
    with open(path, "rb") as f:
        cfg = tomllib.load(f)

    ev = cfg.get("event", {})
    VIEW = ev.get("view", VIEW)
    if VIEW not in VIEWS:
        sys.exit(f"{path}: invalid event.view {VIEW!r} "
                 f"(expected {', '.join(VIEWS)})")
    # a timed lap race has no course length: distance stays as recorded
    open_ended = VIEW == "laps" and "total_km" not in ev
    OFFICIAL_TOTAL_KM = (math.inf if open_ended else
                         float(ev.get("total_km", OFFICIAL_TOTAL_KM)))
    GAIN_SMOOTH_WINDOW_M = float(ev.get("gain_smooth_window_m",
                                        GAIN_SMOOTH_WINDOW_M))
    FINISH_LABEL = ev.get("finish_label", FINISH_LABEL)
    START_LABEL = ev.get("start_label", START_LABEL)
    FINISH_LABEL_POS = str(ev.get("finish_label_pos", FINISH_LABEL_POS)).lower()
    if FINISH_LABEL_POS not in LABEL_OFFSETS:
        sys.exit(f"{path}: event.finish_label_pos must be one of "
                 f"{', '.join(LABEL_OFFSETS)}")
    LOCAL_UTC_OFFSET_HOURS = float(ev.get("utc_offset_hours",
                                          LOCAL_UTC_OFFSET_HOURS))

    CHECKPOINTS = []
    for cp in cfg.get("checkpoints", []):
        CHECKPOINTS.append({
            "label": cp["label"],
            "name": cp.get("name", cp["label"]),
            "km": float(cp["km"]),
            "highlight": bool(cp.get("highlight", False)),
            # map view: which side of the marker the label goes (compass
            # point n/ne/e/se/s/sw/w/nw), to dodge the route line
            "label_pos": str(cp.get("label_pos", "n")).lower(),
        })
        if CHECKPOINTS[-1]["label_pos"] not in LABEL_OFFSETS:
            sys.exit(f"{path}: checkpoint {cp['label']!r}: label_pos must be "
                     f"one of {', '.join(LABEL_OFFSETS)}")
    CHECKPOINTS.sort(key=lambda c: c["km"])

    lp = cfg.get("laps", {})
    limit = lp.get("time_limit_h", LAP_TIME_LIMIT_H)
    LAP_TIME_LIMIT_H = float(limit) if limit is not None else None
    lap_km = lp.get("lap_km", LAP_KM)
    LAP_KM = float(lap_km) if lap_km is not None else None
    LAP_START_RADIUS_M = float(lp.get("start_radius_m", LAP_START_RADIUS_M))
    LAP_MIN_S = float(lp.get("min_lap_s", LAP_MIN_S))
    LATE_LAP_COUNTS = bool(lp.get("late_lap_counts", LATE_LAP_COUNTS))
    LAP_STYLE = str(lp.get("style", LAP_STYLE)).lower()
    if LAP_STYLE not in ("profile", "3d"):
        sys.exit(f"{path}: laps.style must be \"profile\" or \"3d\"")
    az = lp.get("view_azimuth_deg", LAP_VIEW_AZIMUTH_DEG)
    LAP_VIEW_AZIMUTH_DEG = float(az) if az is not None else None
    LAP_VIEW_ROTATE_DEG = float(lp.get("view_rotate_deg",
                                       LAP_VIEW_ROTATE_DEG))
    LAP_VIEW_TILT_DEG = float(lp.get("view_tilt_deg", LAP_VIEW_TILT_DEG))
    LAP_Z_EXAGGERATION = float(lp.get("z_exaggeration", LAP_Z_EXAGGERATION))

    al = cfg.get("alignment", {})
    DISTANCE_ALIGN = al.get("mode", "none" if open_ended else DISTANCE_ALIGN)
    if DISTANCE_ALIGN not in ("checkpoints", "linear", "none"):
        sys.exit(f"{path}: invalid alignment.mode {DISTANCE_ALIGN!r} "
                 f"(expected checkpoints, linear or none)")
    REST_MAX_SPEED_MS = float(al.get("rest_max_speed_ms", REST_MAX_SPEED_MS))
    REST_MIN_DURATION_S = float(al.get("rest_min_duration_s",
                                       REST_MIN_DURATION_S))
    MATCH_TOLERANCE_KM = float(al.get("match_tolerance_km",
                                      MATCH_TOLERANCE_KM))

    cam = cfg.get("camera", {})
    CAMERA_UTC_OFFSET_HOURS = float(cam.get("utc_offset_hours",
                                            CAMERA_UTC_OFFSET_HOURS))
    CAMERA_CLOCK_BEHIND_S = float(cam.get("clock_behind_s",
                                          CAMERA_CLOCK_BEHIND_S))
    return cfg

# Output image geometry. 1280 px is 1/3 of a 4K frame width (and scales down
# crisply to 1/3 of 1080p). Override per-run with --width/--height/--dpi.
IMG_WIDTH_PX = 1280
IMG_HEIGHT_PX = 400
IMG_DPI = 100
# The map view is squarer: stats column on the left, route on the right.
LAPS_3D_IMG_WIDTH_PX = 800          # laps view, 3-D style: info column on
LAPS_3D_IMG_HEIGHT_PX = 430         # the left, the loop on the right
LAPS_3D_COL_PX = 345                # width of that column (fixed, so the
                                    # loop does not jump between frames)
LAP_PIP_PITCH_PX = 17               # laps view: spacing / radius of the lap
LAP_PIP_RADIUS_PX = 5               # pips (shrunk to fit when there are many)
LAP_URGENT_S = 600                  # countdown turns accent under this
MAP_IMG_WIDTH_PX = 960
MAP_IMG_HEIGHT_PX = 480
MAP_TEXT_COL_PX = 330               # width of the stats column (map view)
MAP_PAD = 0.07                      # margin around the route, fraction
ROUTE_STEP_M = 20                   # one route vertex per this many meters
ROUTE_LINE_WIDTH = 3.2
LOOP_MAX_GAP_M = 300                # start within this of finish = loop

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
FONT_SIZE_LIVE = 24                 # live heart rate / pace values
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
    lat: list           # degrees per point (None where the fix was missing)
    lon: list
    hr: list            # heart rate bpm per point (None where missing)
    speed: list         # device speed m/s per point (None where missing)
    cadence: list       # running cadence per point (None where missing)


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
    ts, dist, ele, lat, lon, hr, speed, cad = [], [], [], [], [], [], [], []
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
        hr.append(v.get("heart_rate"))
        sp = v.get("enhanced_speed")
        speed.append(sp if sp is not None else v.get("speed"))
        cad.append(v.get("cadence"))

    lat = [x * (180 / 2**31) if x is not None else None for x in lat]
    lon = [x * (180 / 2**31) if x is not None else None for x in lon]
    if any(d is None for d in dist):
        dist = _cumulative_from_coords(lat, lon)
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
    if not rests:  # no rest laps (plain run mode / auto laps): use the track
        rests = _detect_standstills(ts, dist_km)
    return Track(ts, dist_km, ele, _merge_rests(rests), lat, lon, hr, speed,
                 cad)


def _detect_standstills(ts, dist_km):
    """Standstills straight from the samples: stretches where the 30 s
    running-average speed stays below REST_MAX_SPEED_MS for at least
    REST_MIN_DURATION_S. Fallback when the watch recorded no rest laps."""
    n = len(ts)
    if n < 2:
        return []
    window = 30.0
    slow = [False] * n
    j = 0
    for i in range(n):
        while j < n and (ts[j] - ts[i]).total_seconds() < window:
            j += 1
        if j >= n:
            break
        dt = (ts[j] - ts[i]).total_seconds()
        if (dist_km[j] - dist_km[i]) * 1000 / dt < REST_MAX_SPEED_MS:
            for k in range(i, j):
                slow[k] = True
    rests, start = [], None
    for i in range(n):
        if slow[i] and start is None:
            start = i
        elif not slow[i] and start is not None:
            dur = (ts[i] - ts[start]).total_seconds()
            if dur >= REST_MIN_DURATION_S:
                rests.append((dist_km[start], dur))
            start = None
    return rests


def _merge_rests(rests):
    """Merge back-to-back rests (< 500 m apart) into one."""
    merged = []
    for km, dur in sorted(rests):
        if merged and km - merged[-1][0] < 0.5:
            merged[-1] = (merged[-1][0], merged[-1][1] + dur)
        else:
            merged.append((km, dur))
    return merged


def parse_gpx(path):
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    root = ET.parse(path).getroot()
    if root.tag.startswith("{"):  # tolerate GPX 1.0 namespace too
        ns["g"] = root.tag[1:].split("}")[0]
    ts, ele, lat, lon, hr = [], [], [], [], []
    for pt in root.iter(f"{{{ns['g']}}}trkpt"):
        e = pt.find("g:ele", ns)
        t = pt.find("g:time", ns)
        if e is None:
            continue
        lat.append(float(pt.get("lat")))
        lon.append(float(pt.get("lon")))
        ele.append(float(e.text))
        # heart rate lives in vendor extensions (gpxtpx:hr, ns3:hr, ...)
        h = next((el for el in pt.iter() if el.tag.rsplit("}", 1)[-1] == "hr"),
                 None)
        hr.append(int(float(h.text)) if h is not None and h.text else None)
        if t is not None:
            ts.append(datetime.fromisoformat(t.text.replace("Z", "+00:00"))
                      .astimezone(timezone.utc).replace(tzinfo=None))
        else:
            ts.append(None)
    if any(t is None for t in ts):
        # partial/missing timestamps (e.g. an exported course file): disable
        # all time-based features rather than guessing
        ts = [None] * len(ts)
    dist_km = [d / 1000.0 for d in _cumulative_from_coords(lat, lon)]
    rests = _detect_standstills(ts, dist_km) if ts and ts[0] is not None else []
    return Track(ts, dist_km, ele, _merge_rests(rests), lat, lon, hr,
                 [None] * len(ts), [None] * len(ts))


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
# Course preparation (profile polyline + route polyline)
# ============================================================================


@dataclass
class Course:
    px: list            # profile: official km per vertex
    py: list            # profile: smoothed elevation per vertex
    rx: list            # route: local east metres per vertex (map view)
    ry: list            # route: local north metres per vertex
    rkm: list           # route: official km per vertex
    laps: "Laps | None" = None  # laps view only


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


def prepare_course(track, map_fn, laps=False):
    """Downsampled profile and route polylines on the official-km scale;
    with laps=True (laps view) also the detected laps + loop profile."""
    kms = [map_fn(d) for d in track.dist_km]
    px, py = _prepare_profile(kms, track.ele)
    rx, ry, rkm = _prepare_route(kms, track.lat, track.lon)
    return Course(px, py, rx, ry, rkm,
                  prepare_laps(track, kms) if laps else None)


def _prepare_profile(kms, ele):
    """Smoothed, downsampled (official_km, elevation) polyline."""
    smooth = _smooth_elevation(kms, ele)
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


def _prepare_route(kms, lat, lon):
    """Downsampled route as local metres (equirectangular around the track
    centre — plenty for a race-sized area), with official km per vertex."""
    pts = [(k, la, lo) for k, la, lo in zip(kms, lat, lon)
           if la is not None and lo is not None]
    if len(pts) < 2:
        return [], [], []
    lat0 = sum(p[1] for p in pts) / len(pts)
    lon0 = sum(p[2] for p in pts) / len(pts)
    mx = 111320.0 * math.cos(math.radians(lat0))
    my = 110574.0
    step = ROUTE_STEP_M / 1000.0
    rx, ry, rkm, nxt = [], [], [], 0.0
    for k, la, lo in pts:
        if k >= nxt:
            rx.append((lo - lon0) * mx)
            ry.append((la - lat0) * my)
            rkm.append(k)
            nxt = k + step
    k, la, lo = pts[-1]
    rx.append((lo - lon0) * mx)
    ry.append((la - lat0) * my)
    rkm.append(k)
    return rx, ry, rkm


# ============================================================================
# Laps (laps view): boundaries from the GPS track, one averaged loop profile
# ============================================================================

# 3-D style: deliberately light — it is a HUD, the footage is the picture.
LOOP3D_WALL_ALPHA = 0.07    # the not-yet-done wall (overlapping walls add up)
LOOP3D_DONE_ALPHA = 0.22    # ... and the completed wall
LOOP3D_GROUND_SHADOW = 0.12  # black fill of the loop's footprint
LOOP3D_RIBBON_LW = 1.5      # the track line
LOOP3D_TRAIL_LW = 2.2       # accent trail over the completed part
LOOP3D_DOT_SCALE = 0.6      # marker size relative to HERE_DOT_SIZE
LOOP3D_SMOOTH_M = 90        # ribbon smoothing (plan and elevation), round
                            # the ring: a diagram, not a survey
LOOP_PROFILE_POINTS = 240   # vertices of the loop profile
LOOP_SMOOTH_WINDOW_M = 40   # light: averaging the laps already kills noise,
                            # and a wide window would shave the summit
LAP_TAIL_MIN_FRACTION = 0.25  # track left after the last crossing is a lap
                              # in progress if at least this much of a loop
                              # (less = milling about after the finish)


@dataclass
class Laps:
    ts: list            # boundary times: start, then every start-line pass
    kms: list           # course km at those boundaries
    tail: bool          # the track ends mid-lap (last boundary is synthetic)
    lap_km: float       # loop length the profile is drawn over
    lx: list            # loop profile: km within the loop
    ly: list            # loop profile: elevation
    track_ts: list      # per-sample time / km, to find the time of a km
    track_kms: list
    gx: list = None     # loop in plan: local east / north metres per profile
    gy: list = None     # vertex (None without GPS) — the 3-D style

    @property
    def count(self):
        return len(self.ts) - 1

    def _counts(self, j, limit_t):
        """Does lap j (0-based) count towards the result?"""
        if self.tail and j == self.count - 1:
            return False
        if limit_t is None or self.ts[j + 1] <= limit_t:
            return True
        return LATE_LAP_COUNTS and self.ts[j] < limit_t

    def state(self, km, when=None):
        """Everything the HUD shows about laps at course km / time `when`
        (UTC; defaults to the first moment the track reached that km)."""
        if when is None:
            i = min(bisect_left(self.track_kms, km), len(self.track_ts) - 1)
            when = self.track_ts[i]
        when = min(max(when, self.ts[0]), self.track_ts[-1])
        limit_t = (self.ts[0] + timedelta(hours=LAP_TIME_LIMIT_H)
                   if LAP_TIME_LIMIT_H else None)
        n = bisect_right(self.ts, when) - 1          # lap in progress
        finished = n >= self.count
        past_limit = limit_t is not None and when >= limit_t

        pips = []
        for j in range(self.count):
            counts = self._counts(j, limit_t)
            if not counts and past_limit and (j <= n or finished):
                pips.append("void")
            elif j < n:
                pips.append("done" if counts else "void")
            else:
                pips.append("current" if j == n else "todo")
        completed = sum(1 for j in range(min(n, self.count))
                        if self._counts(j, limit_t))

        # the last lap that did not count (for the final read-out)
        void = [j for j in range(min(n, self.count))
                if not self._counts(j, limit_t)
                and not (self.tail and j == self.count - 1)]
        late = None
        if void and limit_t is not None:
            late = (void[-1] + 1,
                    (self.ts[void[-1] + 1] - limit_t).total_seconds())

        if finished:
            frac, lap_s = 1.0, None
            counted_now = True
        else:
            k0, k1 = self.kms[n], self.kms[n + 1]
            frac = min(max((km - k0) / (k1 - k0), 0.0), 1.0) if k1 > k0 else 0
            lap_s = (when - self.ts[n]).total_seconds()
            counted_now = self._counts(n, limit_t) or not past_limit
        last_s = ((self.ts[min(n, self.count)] -
                   self.ts[min(n, self.count) - 1]).total_seconds()
                  if n >= 1 else None)
        return {
            "lap": None if finished else n + 1,
            "completed": completed,
            "frac": frac,
            "lap_s": lap_s,
            "last_s": last_s,
            "elapsed_s": (when - self.ts[0]).total_seconds(),
            "remaining_s": ((limit_t - when).total_seconds()
                            if limit_t is not None else None),
            # True from the moment the limit passes on a lap that won't count
            "void": not counted_now,
            "finished": finished,
            "late": late,       # (lap number, seconds over the limit)
            "pips": pips,
        }


def _detect_lap_bounds(track):
    """Sample indices where a lap starts: 0, then the closest sample of
    every later visit to the start point (first GPS fix)."""
    fixes = [i for i, la in enumerate(track.lat)
             if la is not None and track.lon[i] is not None]
    if not fixes:
        sys.exit("The laps view needs GPS positions to find the laps")
    i0 = fixes[0]
    lat0, lon0 = track.lat[i0], track.lon[i0]
    bounds, visit = [0], []

    def close_visit():
        if not visit:
            return
        d, i = min(visit)
        if (track.ts[i] - track.ts[bounds[-1]]).total_seconds() >= LAP_MIN_S:
            bounds.append(i)
        visit.clear()

    for i in fixes:
        d = _haversine_m(lat0, lon0, track.lat[i], track.lon[i])
        if d <= LAP_START_RADIUS_M:
            visit.append((d, i))
        else:
            close_visit()
    close_visit()
    return bounds


def prepare_laps(track, kms):
    if not has_timestamps(track):
        sys.exit("The laps view needs an activity file with timestamps")
    bounds = _detect_lap_bounds(track)
    if len(bounds) < 2:
        sys.exit("No laps found: the track never returns to within "
                 f"{LAP_START_RADIUS_M:g} m of its start "
                 "(laps.start_radius_m)")
    lens = sorted(kms[b] - kms[a] for a, b in zip(bounds, bounds[1:]))
    median_km = lens[len(lens) // 2]

    # one loop profile: every full lap resampled by fraction of its length,
    # median across laps (barometer drift and GPS noise drop out)
    n = LOOP_PROFILE_POINTS
    columns = [[] for _ in range(n + 1)]
    for a, b in zip(bounds, bounds[1:]):
        lap_k, lap_e = kms[a:b + 1], track.ele[a:b + 1]
        span = lap_k[-1] - lap_k[0]
        if span <= 0:
            continue
        for c in range(n + 1):
            columns[c].append(interp(lap_k, lap_e, lap_k[0] + span * c / n))
    lap_km = LAP_KM or median_km
    lx = [lap_km * c / n for c in range(n + 1)]
    ly = [sorted(col)[len(col) // 2] for col in columns]
    ly = _smooth_elevation(lx, ly, LOOP_SMOOTH_WINDOW_M)

    gx, gy = _loop_plan(track, kms, bounds, n)

    ts = [track.ts[b] for b in bounds]
    bkms = [kms[b] for b in bounds]
    tail = kms[-1] - bkms[-1] >= LAP_TAIL_MIN_FRACTION * median_km
    if tail:    # lap in progress when the recording ends: never completed
        ts.append(datetime.max)
        bkms.append(bkms[-1] + median_km)
    return Laps(ts, bkms, tail, lap_km, lx, ly, track.ts, kms, gx, gy)


def _loop_plan(track, kms, bounds, n):
    """The loop seen from above, one (east, north) metre pair per profile
    vertex: per-fraction median of all laps like the elevation, lightly
    smoothed, and closed (the median's two ends miss each other by some
    metres of GPS scatter; the gap is spread along the loop)."""
    fixes = [(la, lo) for la, lo in zip(track.lat, track.lon)
             if la is not None and lo is not None]
    lat0 = sum(f[0] for f in fixes) / len(fixes)
    lon0 = sum(f[1] for f in fixes) / len(fixes)
    mx, my = 111320.0 * math.cos(math.radians(lat0)), 110574.0
    cols_x = [[] for _ in range(n + 1)]
    cols_y = [[] for _ in range(n + 1)]
    for a, b in zip(bounds, bounds[1:]):
        pts = [(kms[i], (track.lon[i] - lon0) * mx, (track.lat[i] - lat0) * my)
               for i in range(a, b + 1) if track.lat[i] is not None
               and track.lon[i] is not None]
        if len(pts) < 2 or pts[-1][0] <= pts[0][0]:
            continue
        pk, px, py = zip(*pts)
        span = pk[-1] - pk[0]
        for c in range(n + 1):
            k = pk[0] + span * c / n
            cols_x[c].append(interp(pk, px, k))
            cols_y[c].append(interp(pk, py, k))
    if not cols_x[0]:
        return None, None
    out = []
    for cols in (cols_x, cols_y):
        med = [sorted(col)[len(col) // 2] for col in cols]
        gap = med[-1] - med[0]
        ring = [v - gap * c / n for c, v in enumerate(med)][:n]
        half = 4    # smoothed round the ring, so the ends still meet
        smooth = [sum(ring[(c + d) % n] for d in range(-half, half + 1)) /
                  (2 * half + 1) for c in range(n)]
        out.append(smooth + smooth[:1])
    return out[0], out[1]


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


def render(course, here_km, out_path, label_text, args):
    """One overlay PNG; args.view picks the profile or the map drawing.
    With args.canvas the graphic is placed on a transparent full-frame
    canvas (e.g. 3840x2160) so it drops onto a timeline at zoom 1."""
    if args.view == "map":
        _render_map(course, here_km, out_path, label_text, args)
    elif args.view == "laps":
        _render_laps(course, here_km, out_path, label_text, args)
    else:
        _render_profile(course.px, course.py, here_km, out_path, label_text,
                        args)
    if getattr(args, "canvas", None):
        _place_on_canvas(out_path, args.canvas, args.anchor, args.margin)


def canvas_offset(size, canvas, anchor, margin):
    """Top-left (x, y) of a size = (w, h) graphic on the canvas."""
    (w, h), (cw, ch), margin = size, canvas, margin or 0
    x = margin if "left" in anchor else cw - margin - w
    y = margin if "top" in anchor else ch - margin - h
    if anchor in ("top", "bottom"):
        x = (cw - w) // 2
    return x, y


def _place_on_canvas(path, canvas, anchor, margin):
    from PIL import Image
    cw, ch = canvas
    img = Image.open(path).convert("RGBA")
    x, y = canvas_offset(img.size, canvas, anchor, margin)
    out = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    out.alpha_composite(img, (x, y))
    out.save(path)


def _render_profile(px, py, here_km, out_path, label_text, args):
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
        strip = " · ".join([label_text["bottom"]] + [
            f"{cap} {val}" for cap, val in label_text.get("live") or []])
        ax.annotate(_tracked(strip),
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


def _render_laps(course, here_km, out_path, label_text, args):
    """Lap race: one loop — as a flat profile or as a 3-D ribbon
    (laps.style) — with the marker going round it every lap; lap counter,
    lap pips and the countdown around it."""
    laps = course.laps
    st = (label_text or {}).get("laps") or laps.state(here_km)
    w, h, dpi = args.width, args.height, args.dpi
    fig = plt.figure(figsize=(w / dpi, h / dpi), dpi=dpi)
    fig.patch.set_alpha(0)
    _draw_scrim(fig, w, h)
    strip = None
    if label_text:
        strip = " · ".join(label_text["bottom_lines"] + [
            f"{cap} {val}" for cap, val in label_text.get("live") or []])
    # a lap that will not count banks nothing: no bright "done" part
    banked = not (st["void"] or (st["finished"] and st["late"]))
    if LAP_STYLE == "3d":
        k = dpi / IMG_DPI   # pixel constants are tuned for IMG_DPI
        x0 = (LAPS_3D_COL_PX * k if label_text else 0.035 * w) / w
        _draw_loop_3d(fig, laps, st, banked, [x0, 0.06, 0.965 - x0, 0.88])
        if label_text:
            _draw_laps_column(fig, label_text, st, w, h, k)
    else:
        _draw_loop_profile(fig, laps, st, banked, strip, bool(label_text))
        if label_text:
            _draw_laps_header(fig, label_text, st, w, h)
    fig.savefig(out_path, transparent=True, dpi=dpi)
    plt.close(fig)


def _draw_loop_profile(fig, laps, st, banked, strip, labelled):
    label_room = 0.27 if labelled else 0.05
    ax = fig.add_axes([0.035, 0.155, 0.93, 0.80 - label_room])
    ax.set_facecolor("none")

    px, py, lap_km = laps.lx, laps.ly, laps.lap_km
    floor = min(py) - ELEV_FLOOR_PAD_M
    here = st["frac"] * lap_km
    i = bisect_left(px, here)
    here_ele = interp(px, py, here)
    dx, dy = px[:i] + [here], py[:i] + [here_ele]
    tx, ty = [here] + px[i:], [here_ele] + py[i:]

    ax.fill_between(dx, floor, dy, color=MONO, lw=0, zorder=2,
                    alpha=ALPHA_FILL_DONE if banked else ALPHA_FILL_TODO)
    ax.fill_between(tx, floor, ty, color=MONO, alpha=ALPHA_FILL_TODO,
                    lw=0, zorder=2)
    ax.plot(px, py, color=MONO, lw=LINE_WIDTH, alpha=ALPHA_LINE, zorder=3,
            solid_capstyle="round", path_effects=_line_fx())
    ax.plot([0, lap_km], [floor, floor], color=MONO, lw=1.0, alpha=0.45,
            zorder=4, solid_capstyle="round")
    if here > 0.01 * lap_km and banked:
        ax.plot([0, here], [floor, floor], color=ACCENT, lw=3.0, alpha=0.95,
                zorder=5, solid_capstyle="round")

    # summit and low point, with their elevations
    top = max(range(len(py)), key=py.__getitem__)
    low = min(range(len(py)), key=py.__getitem__)
    for idx, name in ((top, "TOP"), (low, "LOW")):
        ax.plot([px[idx], px[idx]], [floor, py[idx]], color=MONO, lw=0.8,
                alpha=0.22, zorder=4)
        ax.scatter([px[idx]], [py[idx]], s=26, color=MONO, alpha=0.95, lw=0,
                   zorder=6)
        ax.annotate(_tracked(f"{name} {py[idx]:.0f} m"), (px[idx], py[idx]),
                    xytext=(0, 9), textcoords="offset points",
                    ha="center", va="bottom", color=MONO,
                    fontsize=FONT_SIZE_PIT, fontweight="bold",
                    family=FONT_FAMILY, path_effects=_text_fx(2.0, 0.65),
                    zorder=7, annotation_clip=False)

    ax.scatter([here], [here_ele], s=HERE_DOT_SIZE * 2.8, color=ACCENT,
               alpha=0.28, lw=0, zorder=7, clip_on=False)
    ax.scatter([here], [here_ele], s=HERE_DOT_SIZE, color=ACCENT,
               edgecolors=MONO, linewidths=1.8, zorder=8, clip_on=False)

    # bottom row: end ticks + centered status strip
    for txt, x, ha, off in (("0", 0, "center", 0),
                            (f"{lap_km:g} KM", lap_km, "right", 12)):
        ax.annotate(txt, (x, floor), xytext=(off, -15),
                    textcoords="offset points", ha=ha, color=MONO,
                    alpha=0.95, fontsize=FONT_SIZE_AXIS, family=FONT_FAMILY,
                    path_effects=_text_fx(2.0, 0.65), annotation_clip=False)
    if strip:
        ax.annotate(_tracked(strip), (lap_km / 2, floor), xytext=(0, -15),
                    textcoords="offset points", ha="center", color=MONO,
                    fontsize=FONT_SIZE_LABEL_SUB, fontweight="bold",
                    family=FONT_FAMILY, path_effects=_text_fx(2.0, 0.65),
                    annotation_clip=False)

    ax.set_xlim(-0.006 * lap_km, 1.006 * lap_km)
    ax.set_ylim(floor - 5, max(py) + 30)
    ax.axis("off")


def _loop_camera(laps):
    """(azimuth, tilt, z scale) for the 3-D loop. Default azimuth: side-on
    to the loop's long axis, from the side that has the climb running left
    to right."""
    if LAP_VIEW_AZIMUTH_DEG is not None:
        az = LAP_VIEW_AZIMUTH_DEG
    else:
        n = len(laps.gx)
        cx, cy = sum(laps.gx) / n, sum(laps.gy) / n
        sxx = sum((x - cx) ** 2 for x in laps.gx)
        syy = sum((y - cy) ** 2 for y in laps.gy)
        sxy = sum((x - cx) * (y - cy) for x, y in zip(laps.gx, laps.gy))
        az = -math.degrees(0.5 * math.atan2(2 * sxy, sxx - syy))
        top = max(range(n), key=laps.ly.__getitem__)
        low = min(range(n), key=laps.ly.__getitem__)
        a = math.radians(az)
        screen_x = [x * math.cos(a) - y * math.sin(a)
                    for x, y in ((laps.gx[i], laps.gy[i]) for i in (top, low))]
        if screen_x[0] < screen_x[1]:
            az += 180
        az += LAP_VIEW_ROTATE_DEG
    return az, LAP_VIEW_TILT_DEG, LAP_Z_EXAGGERATION


def _fill_wall_panel(ax, panel, done_alpha):
    """One wall panel: [(top, ground, done), ...] in track order."""
    if len(panel) < 2:
        return
    tops = [p[0] for p in panel]
    grounds = [p[1] for p in panel]
    ax.fill(*zip(*(tops + grounds[::-1])), color=MONO, lw=0, zorder=2,
            alpha=done_alpha if panel[0][2] else LOOP3D_WALL_ALPHA)


def _ring_smooth(values, half):
    """Moving average round a closed ring (values[0] == values[-1])."""
    ring = values[:-1]
    n = len(ring)
    out = [sum(ring[(c + d) % n] for d in range(-half, half + 1)) /
           (2 * half + 1) for c in range(n)]
    return out + out[:1]


def _draw_loop_3d(fig, laps, st, banked, rect):
    """The loop as a ribbon in space: a translucent wall from the track
    down to the ground plane, the ground outline (= the track in plan)
    under it. Oblique parallel projection, no perspective — it stays a
    readable diagram."""
    if not laps.gx:
        sys.exit("laps.style = \"3d\" needs GPS positions in the activity "
                 "file (use style = \"profile\")")
    az, tilt, zex = _loop_camera(laps)
    a, tl = math.radians(az), math.radians(tilt)
    n = len(laps.gx)
    cx, cy = sum(laps.gx) / n, sum(laps.gy) / n
    base = min(laps.ly) - ELEV_FLOOR_PAD_M
    # close the ribbon (the median profile's ends differ by a metre or two)
    # and smooth plan + elevation round the ring: raw, the line looks rough,
    # the more so with the elevation exaggerated
    z_gap = laps.ly[-1] - laps.ly[0]
    zs = [z - z_gap * i / (n - 1) for i, z in enumerate(laps.ly)]
    half = max(int(LOOP3D_SMOOTH_M / 2 / (laps.lap_km * 1000 / (n - 1))), 1)
    zs, gx, gy = (_ring_smooth(v, half) for v in (zs, laps.gx, laps.gy))

    def proj(i, z=None):
        x, y = gx[i] - cx, gy[i] - cy
        depth = x * math.sin(a) + y * math.cos(a)
        z = zs[i] if z is None else z
        return (x * math.cos(a) - y * math.sin(a),
                depth * math.sin(tl) + (z - base) * zex * math.cos(tl))

    top = [proj(i) for i in range(n)]
    ground = [proj(i, base) for i in range(n)]

    ax = fig.add_axes(rect)
    ax.set_facecolor("none")
    ax.set_aspect("equal")
    ax.axis("off")

    # "here", interpolated between vertices so the marker moves smoothly
    pos = st["frac"] * (n - 1)
    i0 = min(int(pos), n - 2)
    f = pos - i0
    mix = lambda p, q: (p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f)  # noqa: E731
    here, here_ground = mix(top[i0], top[i0 + 1]), mix(ground[i0], ground[i0 + 1])

    # depth cue that survives bright footage: a shadow on the ground
    ax.fill(*zip(*ground), color="black", alpha=LOOP3D_GROUND_SHADOW, lw=0,
            zorder=1)
    # Walls. NOT one polygon per wall: the descent runs right-to-left on
    # screen and the climb left-to-right, so where the two walls overlap
    # their windings cancel and the fill leaves a hole. Instead a panel ends
    # wherever the screen direction reverses (and at "here"): between two
    # reversals ribbon and ground line are both x-monotone, so the panel is
    # a simple polygon, and at a reversal the wall folds back on itself, so
    # no seam shows. Where walls overlap the alphas add up — two walls
    # behind each other. Same-colour translucent layers need no depth sort.
    done_top = top[:i0 + 1] + [here]
    wall = ([(top[i], ground[i], True) for i in range(i0 + 1)] +
            [(here, here_ground, False)] +
            [(top[i], ground[i], False) for i in range(i0 + 1, n)])
    done_alpha = LOOP3D_DONE_ALPHA if banked else LOOP3D_WALL_ALPHA
    panel, direction = [wall[0]], 0
    for k, (prev, cur) in enumerate(zip(wall, wall[1:])):
        dx = cur[0][0] - prev[0][0]
        step = (dx > 0) - (dx < 0)
        if step and direction and step != direction:
            _fill_wall_panel(ax, panel, done_alpha)   # prev = turning point
            panel = [prev]
        direction = step or direction
        panel.append(cur)
        if k == i0:     # cur is "here": the done wall ends
            _fill_wall_panel(ax, panel, done_alpha)
            panel = [cur]
    _fill_wall_panel(ax, panel, done_alpha)
    ax.plot(*zip(*ground), color=MONO, lw=1.0, alpha=0.45, zorder=3,
            solid_capstyle="round", solid_joinstyle="round")
    ax.plot(*zip(*top), color=MONO, lw=LOOP3D_RIBBON_LW, alpha=ALPHA_LINE,
            zorder=4, solid_capstyle="round", solid_joinstyle="round",
            path_effects=_line_fx(1.5, 0.35))
    if banked and len(done_top) > 1:
        ax.plot(*zip(*done_top), color=ACCENT, lw=LOOP3D_TRAIL_LW, alpha=0.95,
                zorder=5, solid_capstyle="round", solid_joinstyle="round")

    # labels keep out of the loop's way and inside the panel: the leftmost
    # mark is labelled under the ground line, the rightmost above, text
    # running inwards; any other above
    i_top = max(range(n), key=zs.__getitem__)
    i_low = min(range(n), key=zs.__getitem__)
    marks = [(i_top, f"TOP {max(laps.ly):.0f} m"),
             (i_low, f"LOW {min(laps.ly):.0f} m"), (0, START_LABEL)]
    left = min(marks, key=lambda m: top[m[0]][0])[0]
    right = max(marks, key=lambda m: top[m[0]][0])[0]
    for idx, text in marks:
        ax.plot(*zip(top[idx], ground[idx]), color=MONO, lw=0.8, alpha=0.30,
                zorder=3)
        ax.scatter(*top[idx], s=18, color=MONO, alpha=0.95, lw=0, zorder=6)
        if idx == left:
            anchor, offset, ha, va = ground[idx], (-2, -7), "left", "top"
        elif idx == right:
            anchor, offset, ha, va = top[idx], (4, 8), "right", "bottom"
        else:
            anchor, offset, ha, va = top[idx], (0, 8), "center", "bottom"
        ax.annotate(_tracked(text), anchor, xytext=offset,
                    textcoords="offset points", ha=ha, va=va, color=MONO,
                    fontsize=FONT_SIZE_AXIS - 1.5, fontweight="bold",
                    family=FONT_FAMILY, path_effects=_text_fx(2.0, 0.65),
                    zorder=8, annotation_clip=False)

    ax.plot(*zip(here, here_ground), color=MONO, lw=0.9, alpha=0.55, zorder=6)
    dot = HERE_DOT_SIZE * LOOP3D_DOT_SCALE
    ax.scatter(*here, s=dot * 2.8, color=ACCENT, alpha=0.28, lw=0,
               zorder=7, clip_on=False)
    ax.scatter(*here, s=dot, color=ACCENT, edgecolors=MONO,
               linewidths=1.5, zorder=8, clip_on=False)

    # room for the labels above / below the shape
    xs, ys = zip(*(top + ground))
    padx = 0.04 * (max(xs) - min(xs))
    pady = 0.16 * (max(ys) - min(ys))
    ax.set_xlim(min(xs) - padx, max(xs) + padx)
    ax.set_ylim(min(ys) - pady, max(ys) + pady)


def _draw_laps_column(fig, label_text, st, w, h, k):
    """Info column of the 3-D laps HUD, by importance: the lap, the laps
    that count, the time left; then a small caption/value grid; clock,
    ascent and distance at the bottom."""
    x = 0.045
    fx_big, fx_small = _text_fx(2.5, 0.6), _text_fx(2.0, 0.65)
    dim = st["void"]    # lap in progress no longer counts

    def text(y, s, size, weight="bold", color=MONO, alpha=1.0, va="top",
             dx=0.0, fx=fx_small):
        return fig.text(x + dx, y, s, ha="left", va=va, color=color,
                        alpha=alpha, fontsize=size, fontweight=weight,
                        family=FONT_FAMILY, path_effects=fx)

    px = lambda v: v * k / h    # noqa: E731  (px at IMG_DPI -> figure fraction)
    y = 0.93
    text(y, label_text["value"], FONT_SIZE_LABEL_MAIN, "heavy",
         alpha=0.45 if dim else 1.0,
         fx=_text_fx(2.5, 0.25 if dim else 0.6))
    y -= px(57)
    if label_text.get("unit"):      # "NOT COUNTED"
        text(y, _tracked(label_text["unit"]), FONT_SIZE_PIT, color=ACCENT)
        y -= px(21)
    f = label_text["fields"]
    text(y, _tracked(f["completed"]), FONT_SIZE_LABEL_SUB)
    y -= px(44)

    if label_text.get("countdown"):
        value, caption, urgent = label_text["countdown"]
        colour = ACCENT if urgent else MONO
        vt = text(y, value, FONT_SIZE_LIVE, "heavy", color=colour, fx=fx_big)
        fig.canvas.draw()
        bb = vt.get_window_extent(fig.canvas.get_renderer())
        if caption:
            fig.text(bb.x1 / w + 0.012, bb.y0 / h, _tracked(caption),
                     ha="left", va="bottom", color=colour,
                     fontsize=FONT_SIZE_PIT, fontweight="bold",
                     family=FONT_FAMILY, path_effects=fx_small)
        y -= px(54)

    # caption/value grid, two per row
    pairs = list(f["pairs"]) + [(cap, val.replace(" bpm", "").replace(" /km", ""))
                                for cap, val in label_text.get("live") or []]
    col_w = (LAPS_3D_COL_PX * k / w - x) / 2
    for row in range(0, len(pairs), 2):
        for c, (cap, val) in enumerate(pairs[row:row + 2]):
            text(y, _tracked(cap), FONT_SIZE_PIT - 2, alpha=0.8,
                 dx=c * col_w)
            text(y - px(16), val, FONT_SIZE_LABEL_UNIT, "heavy",
                 dx=c * col_w, fx=fx_big)
        y -= px(53)

    for row, line in enumerate(reversed(f["footer"])):
        text(0.075 + row * px(22), _tracked(line), FONT_SIZE_PIT,
             va="bottom", alpha=0.95)


def _draw_laps_header(fig, label_text, st, w, h):
    """'LAP 7' + completed/gain line on the left, the countdown on the
    right, one pip per lap under the countdown."""
    dim = st["void"]    # lap in progress no longer counts
    vt = fig.text(0.035, 0.955, label_text["value"], ha="left", va="top",
                  color=MONO, alpha=0.45 if dim else 1.0,
                  fontsize=FONT_SIZE_LABEL_MAIN, fontweight="heavy",
                  family=FONT_FAMILY,
                  path_effects=_text_fx(2.5, 0.25 if dim else 0.6))
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    bb = vt.get_window_extent(renderer)
    if label_text.get("unit"):
        fig.text(bb.x1 / w + 0.010, bb.y0 / h + 0.012,
                 _tracked(label_text["unit"]), ha="left", va="bottom",
                 color=ACCENT if dim else MONO, fontsize=FONT_SIZE_LABEL_SUB,
                 fontweight="bold", family=FONT_FAMILY,
                 path_effects=_text_fx(2.0, 0.65))
    stats_right = 0.035 * w
    if label_text.get("gain"):
        gt = fig.text(0.037, bb.y0 / h - 0.025, _tracked(label_text["gain"]),
                      ha="left", va="top", color=MONO,
                      fontsize=FONT_SIZE_LABEL_SUB, fontweight="bold",
                      family=FONT_FAMILY, path_effects=_text_fx(2.0, 0.65))
        stats_right = gt.get_window_extent(renderer).x1

    clock = label_text.get("countdown")
    if clock:
        value, caption, urgent = clock
        # small caption right of the value, on its baseline ("34 KM" style)
        ct = fig.text(0.965, 0.955, _tracked(caption), ha="right", va="top",
                      color=ACCENT if urgent else MONO,
                      fontsize=FONT_SIZE_LABEL_SUB, fontweight="bold",
                      family=FONT_FAMILY, path_effects=_text_fx(2.0, 0.65))
        x1 = (ct.get_window_extent(renderer).x0 / w - 0.010 if caption
              else 0.965)
        cv = fig.text(x1, 0.955, value, ha="right", va="top",
                      color=ACCENT if urgent else MONO,
                      fontsize=FONT_SIZE_LIVE, fontweight="heavy",
                      family=FONT_FAMILY, path_effects=_text_fx(2.5, 0.6))
        ct.set_va("bottom")
        ct.set_y(cv.get_window_extent(renderer).y0 / h)

    # lap pips, right-aligned on the line of the completed/gain text
    pips = st["pips"]
    ov = fig.add_axes([0, 0, 1, 1])
    ov.set_xlim(0, w)
    ov.set_ylim(0, h)
    ov.axis("off")
    ov.set_facecolor("none")
    k = fig.dpi / IMG_DPI
    room = 0.965 * w - stats_right - 2 * LAP_PIP_PITCH_PX * k
    pitch = min(LAP_PIP_PITCH_PX * k, room / max(len(pips), 1))
    r = min(LAP_PIP_RADIUS_PX * k, pitch * 0.36)
    size = (2 * r * 72 / fig.dpi) ** 2          # scatter size, pt^2
    y = bb.y0 - 0.025 * h - 0.5 * FONT_SIZE_LABEL_SUB * fig.dpi / 72
    x0 = 0.965 * w - r - pitch * (len(pips) - 1)
    fx = _line_fx(1.5, 0.45)
    for j, pip in enumerate(pips):
        x = x0 + j * pitch
        if pip == "done":
            ov.scatter([x], [y], s=size, color=MONO, alpha=0.95, lw=0,
                       path_effects=_text_fx(1.5, 0.45))
        elif pip == "current":
            ov.scatter([x], [y], s=size * 1.5, color=ACCENT, edgecolors=MONO,
                       linewidths=1.4 * k)
        elif pip == "void":     # a cross (drawn: the font has no glyph)
            for sx in (1, -1):
                ov.plot([x - r, x + r], [y - sx * r, y + sx * r],
                        color=ACCENT, lw=2.2 * k, solid_capstyle="round",
                        path_effects=fx)
        else:
            ov.scatter([x], [y], s=size, facecolors="none", edgecolors=MONO,
                       alpha=0.45, linewidths=1.4 * k)


def _route_xy(course, km):
    """Route point (local metres) at an official km, interpolated."""
    rx, ry, rkm = course.rx, course.ry, course.rkm
    km = min(max(km, rkm[0]), rkm[-1])
    return interp(rkm, rx, km), interp(rkm, ry, km)


def _nice_scale_m(extent_m):
    """Scale-bar length: the largest round number under a quarter of the
    map width."""
    best = 100
    for cand in (100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000):
        if cand <= extent_m / 4:
            best = cand
    return best


def _render_map(course, here_km, out_path, label_text, args):
    if not course.rx:
        sys.exit("The activity file has no GPS positions — the map view "
                 "needs lat/lon (use --view profile)")
    w, h, dpi = args.width, args.height, args.dpi
    fig = plt.figure(figsize=(w / dpi, h / dpi), dpi=dpi)
    fig.patch.set_alpha(0)
    _draw_scrim(fig, w, h)

    # stats column (left) first, so the map can take whatever width is
    # left. The column is sized for the widest status line the event can
    # produce, not the current one, so the map does not jump between
    # frames of an animated overlay when the text changes length.
    px_scale = dpi / IMG_DPI  # pixel constants are tuned for IMG_DPI
    if label_text:
        _draw_map_stats(fig, label_text, w, h, px_scale)
        col_px = max(_map_column_px(fig, w), MAP_TEXT_COL_PX * px_scale)
    else:
        col_px = 0
    x0 = max(col_px, 0.03 * w) / w
    rect = [x0, 0.06, 0.97 - x0, 0.88]
    ax = fig.add_axes(rect)
    ax.set_facecolor("none")
    ax.set_aspect("equal")
    rx, ry, rkm = course.rx, course.ry, course.rkm
    ew, eh = max(rx) - min(rx), max(ry) - min(ry)
    cx, cy = (max(rx) + min(rx)) / 2, (max(ry) + min(ry)) / 2
    aw, ah = rect[2] * w, rect[3] * h  # axes size in px
    scale = max(ew / aw, eh / ah) * (1 + 2 * MAP_PAD)  # metres per px
    ax.set_xlim(cx - scale * aw / 2, cx + scale * aw / 2)
    ax.set_ylim(cy - scale * ah / 2, cy + scale * ah / 2)
    ax.axis("off")

    here_km = min(max(here_km, 0.0), OFFICIAL_TOTAL_KM)
    hx, hy = _route_xy(course, here_km)
    i = bisect_left(rkm, here_km)
    dx, dy = rx[:i] + [hx], ry[:i] + [hy]
    tx, ty = [hx] + rx[i:], [hy] + ry[i:]

    # route: one color, progress carried by opacity; remaining part drawn
    # first so the completed line sits on top where the course overlaps
    ax.plot(tx, ty, color=MONO, lw=ROUTE_LINE_WIDTH, alpha=0.32, zorder=2,
            solid_capstyle="round", solid_joinstyle="round",
            path_effects=_line_fx(1.5, 0.25))
    if len(dx) > 1:
        ax.plot(dx, dy, color=MONO, lw=ROUTE_LINE_WIDTH, alpha=ALPHA_LINE,
                zorder=3, solid_capstyle="round", solid_joinstyle="round",
                path_effects=_line_fx())

    # start / finish, checkpoints
    sx, sy = rx[0], ry[0]
    fx, fy = _route_xy(course, OFFICIAL_TOTAL_KM)
    loop = math.hypot(fx - sx, fy - sy) < LOOP_MAX_GAP_M
    marks = [(c["label"], c["km"], c["highlight"], c["label_pos"])
             for c in CHECKPOINTS]
    marks.append((FINISH_LABEL, OFFICIAL_TOTAL_KM, False, FINISH_LABEL_POS))
    if not loop:
        marks.append((START_LABEL, 0.0, False, "n"))
    for label, km, highlight, pos in marks:
        mx, my = _route_xy(course, km)
        ax.scatter([mx], [my], s=130 if highlight else 34,
                   marker="*" if highlight else "o", color=MONO, alpha=0.95,
                   lw=0, zorder=6)
        (ox, oy), ha, va = LABEL_OFFSETS[pos]
        ax.annotate(_tracked(label), (mx, my), xytext=(ox, oy),
                    textcoords="offset points", ha=ha, va=va, color=MONO,
                    fontsize=FONT_SIZE_PIT, fontweight="bold",
                    family=FONT_FAMILY, path_effects=_text_fx(2.0, 0.65),
                    zorder=7, annotation_clip=False)

    # you-are-here: accent glow + dot with white ring
    ax.scatter([hx], [hy], s=HERE_DOT_SIZE * 2.8, color=ACCENT, alpha=0.28,
               lw=0, zorder=7)
    ax.scatter([hx], [hy], s=HERE_DOT_SIZE, color=ACCENT, edgecolors=MONO,
               linewidths=1.8, zorder=8)

    # scale bar, bottom-right corner of the map
    bar = _nice_scale_m(scale * aw)
    bx1 = ax.get_xlim()[1] - scale * aw * 0.03
    by = ax.get_ylim()[0] + scale * ah * 0.05
    ax.plot([bx1 - bar, bx1], [by, by], color=MONO, lw=2.0, alpha=0.85,
            solid_capstyle="butt", zorder=5, path_effects=_line_fx(1.5, 0.4))
    for x in (bx1 - bar, bx1):
        ax.plot([x, x], [by, by + scale * 5], color=MONO, lw=2.0, alpha=0.85,
                zorder=5)
    ax.annotate(f"{bar / 1000:g} KM" if bar >= 1000 else f"{bar} M",
                (bx1 - bar / 2, by), xytext=(0, 6), textcoords="offset points",
                ha="center", va="bottom", color=MONO, alpha=0.95,
                fontsize=FONT_SIZE_AXIS, family=FONT_FAMILY,
                path_effects=_text_fx(2.0, 0.65))

    fig.savefig(out_path, transparent=True, dpi=dpi)
    plt.close(fig)


_COLUMN_CACHE = {}


def _map_column_px(fig, w):
    """Right edge (px) of the widest stats line this event can show."""
    key = (w, fig.dpi, OFFICIAL_TOTAL_KM, tuple(c["name"] for c in CHECKPOINTS))
    if key in _COLUMN_CACHE:
        return _COLUMN_CACHE[key]
    names = [c["name"] for c in CHECKPOINTS] + [FINISH_LABEL]
    lines = [f"Next {n} · 88.8 km" for n in names]
    lines += ["elapsed 88h 88m", "moving 88h 88m", "+8888 m", "88:88",
              "888 bpm", "88:88 /km"]
    renderer = fig.canvas.get_renderer()
    right = 0
    for line in lines:
        t = fig.text(0.037, 0.5, _tracked(line), ha="left", va="top",
                     fontsize=FONT_SIZE_LABEL_SUB, fontweight="bold",
                     family=FONT_FAMILY, alpha=0)
        right = max(right, t.get_window_extent(renderer).x1)
        t.remove()
    _COLUMN_CACHE[key] = right + 0.03 * w
    return _COLUMN_CACHE[key]


def _draw_map_stats(fig, label_text, w, h, px_scale=1.0):
    """Stats column for the map view: value / gain / times at the top,
    status lines at the bottom. Returns the column's right edge in px."""
    texts = []
    vt = fig.text(0.035, 0.93, label_text["value"], ha="left", va="top",
                  color=MONO, fontsize=FONT_SIZE_LABEL_MAIN,
                  fontweight="heavy", family=FONT_FAMILY,
                  path_effects=_text_fx(2.5, 0.6))
    texts.append(vt)
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    bb = vt.get_window_extent(renderer)
    if label_text.get("unit"):
        texts.append(fig.text(
            bb.x1 / w + 0.008, bb.y0 / h, label_text["unit"], ha="left",
            va="bottom", color=MONO, alpha=0.95, fontsize=FONT_SIZE_LABEL_UNIT,
            fontweight="bold", family=FONT_FAMILY,
            path_effects=_text_fx(2.0, 0.65)))
    y = bb.y0 / h - 0.03
    row_h = 26 * px_scale / h  # line pitch in px
    lines = []
    if label_text.get("gain"):
        lines.append(label_text["gain"])
    lines += label_text.get("right") or []
    for line in lines:
        texts.append(fig.text(
            0.037, y, _tracked(line), ha="left", va="top", color=MONO,
            fontsize=FONT_SIZE_LABEL_SUB, fontweight="bold",
            family=FONT_FAMILY, path_effects=_text_fx(2.0, 0.65)))
        y -= row_h
    bottom = label_text.get("bottom_lines") or []
    for row, line in enumerate(reversed(bottom)):
        texts.append(fig.text(
            0.037, 0.07 + row * row_h, _tracked(line), ha="left",
            va="bottom", color=MONO, fontsize=FONT_SIZE_LABEL_SUB,
            fontweight="bold", family=FONT_FAMILY,
            path_effects=_text_fx(2.0, 0.65)))

    # live block (heart rate / pace): caption + value pairs, centred in
    # the gap between the top stats and the status lines
    live = label_text.get("live") or []
    if live:
        cap_h = 15 * px_scale / h
        val_h = 36 * px_scale / h
        top = y + row_h - 0.01          # bottom of the last top line
        bot = 0.07 + len(bottom) * row_h + 0.02
        block = len(live) * (cap_h + val_h) - 0.4 * cap_h
        yy = (top + bot) / 2 + block / 2
        for cap, val in live:
            texts.append(fig.text(
                0.037, yy, _tracked(cap), ha="left", va="top", color=MONO,
                alpha=0.85, fontsize=FONT_SIZE_PIT, fontweight="bold",
                family=FONT_FAMILY, path_effects=_text_fx(2.0, 0.65)))
            yy -= cap_h
            texts.append(fig.text(
                0.035, yy, val, ha="left", va="top", color=MONO,
                fontsize=FONT_SIZE_LIVE, fontweight="heavy",
                family=FONT_FAMILY, path_effects=_text_fx(2.5, 0.6)))
            yy -= val_h
    right = max(t.get_window_extent(renderer).x1 for t in texts)
    return right + 0.03 * w


def _hm(seconds):
    mins = max(int(seconds // 60), 0)
    return f"{mins // 60}h {mins % 60:02d}m"


def _ms(seconds):
    seconds = max(int(seconds), 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


def make_label(here_km, when_utc=None, start_utc=None, moving_s=None,
               gain_m=None, custom=None, hr=None, pace_s_km=None,
               show_hr=False, show_pace=False, laps=None):
    """Label layout dict for render():

      value/unit — big top-left ('34' / 'KM'; custom text replaces both)
      gain       — small top-left, under the value ('+812 m')
      right      — stacked top-right lines (elapsed / moving time)
      bottom     — status strip under the graph ('11:36 · Next PS3 · 2.0 km')
      bottom_lines — the same, as separate lines (map view stacks them)
      live       — [(caption, value), ...] current heart rate / pace

    With laps (a Laps.state() dict, laps view) the big value is the lap in
    progress, the line under it the laps that count so far, and the label
    also carries 'countdown' (value, caption, urgent) and the state itself.
    """
    live = []
    if show_hr:
        live.append(("HR", f"{hr:.0f} bpm" if hr else "-- bpm"))
    if show_pace:
        live.append(("pace", _pace_str(pace_s_km)))
    if laps is not None:
        return _make_laps_label(laps, here_km, when_utc, gain_m, custom, live)

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

    return {"value": value, "unit": unit, "gain": gain, "right": right,
            "bottom": " · ".join(bottom), "bottom_lines": bottom,
            "live": live}


def _make_laps_label(st, here_km, when_utc, gain_m, custom, live):
    n = st["completed"]
    if st["finished"]:
        value, unit = f"{n} LAP" + ("S" if n != 1 else ""), None
        stats = ["final"]
    else:
        value = f"LAP {st['lap']}"
        unit = "not counted" if st["void"] else None
        stats = [f"{n} completed"]
    if custom:
        value, unit = custom, None
    if gain_m is not None:
        stats.append(f"+{gain_m:.0f} m")
    stats.append(f"{here_km:.1f} km")

    bottom, countdown = [], None
    if when_utc is not None:
        clock = when_utc + timedelta(hours=LOCAL_UTC_OFFSET_HOURS)
        bottom.append(f"{clock:%H:%M}")
        left = st["remaining_s"]
        if left is None:
            countdown = (_hm(st["elapsed_s"]), "elapsed", False)
        elif left > LAP_URGENT_S:
            countdown = (_hm(left + 59), "left", False)
        elif left > 0:
            countdown = (_ms(left), "left", True)
        elif st["void"]:
            countdown = ("+" + _ms(-left), "over", True)
        elif not st["finished"]:
            countdown = ("FINAL LAP", "", True)
        else:
            countdown = ("TIME", "", False)
        if st["finished"]:
            if st["late"]:
                lap, over_s = st["late"]
                bottom.append(f"lap {lap} finished {_ms(over_s)} over")
            elif st["last_s"] is not None:
                bottom.append(f"last lap {_ms(st['last_s'])}")
        else:
            bottom.append(f"this lap {_ms(st['lap_s'])}")
            if st["last_s"] is not None:
                bottom.append(f"last {_ms(st['last_s'])}")
    # the same facts in pieces, for the 3-D style's info column
    pairs, footer = [], []
    if st["finished"]:
        if st["last_s"] is not None:
            pairs.append(("last lap", _ms(st["last_s"])))
        if st["late"]:
            footer.append(f"lap {st['late'][0]} · {_ms(st['late'][1])} over")
    else:
        pairs.append(("this lap", _ms(st["lap_s"])))
        pairs.append(("last lap", _ms(st["last_s"])
                      if st["last_s"] is not None else "-:--"))
    footer.append(" · ".join(([bottom[0]] if when_utc is not None else [])
                             + stats[1:]))
    fields = {"completed": stats[0], "pairs": pairs, "footer": footer}
    return {"value": value, "unit": unit, "gain": " · ".join(stats),
            "right": [], "bottom": " · ".join(bottom),
            "bottom_lines": bottom, "live": live, "countdown": countdown,
            "laps": st, "fields": fields}


def _pace_str(s_per_km):
    if not s_per_km or s_per_km > 59 * 60:
        return "--:-- /km"
    m, sec = divmod(int(round(s_per_km / PACE_ROUND_S)) * PACE_ROUND_S, 60)
    return f"{m}:{sec:02d} /km"


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


# Current pace, from first principles about the watch's channels:
#   distance  GPS-derived, reacts within a second or two but noisy (a step
#             at the aid-station table = a metre; walking swings 1.0-1.7 m/s
#             second to second)                     -> decides IF you move
#   speed     the watch's fused estimate: smooth, what its pace field
#             shows, but ramps up for ~10 s after a start -> the VALUE once
#             it has caught up
#   cadence   wrist accelerometer, instant, but 0 when the arm does not
#             swing (eating)                        -> corroborates moving
PACE_WINDOW_S = 6       # distance averaged over this (moving seconds only)
PACE_SMOOTH_S = 5       # the displayed value: speed channel averaged over
                        # this (DC Rainmaker's sweet spot; Stryd offers 3/10/30)
PACE_LEAD_S = 2         # channels trail reality by ~2 s; read ahead
PACE_STILL_MS = 0.3     # per-second distance delta below this = not moving
PACE_WALK_MS = 1.4      # the distance channel alone can claim at most a
                        # brisk walk; faster must come from the speed
                        # channel (GPS distance jumps when you set off)
PACE_ROUND_S = 5        # display granularity, s/km (steadier read-out)


def make_hr_lookup(track):
    """timestamp (UTC) -> heart rate (bpm) of the nearest sample within
    10 s, or None."""
    if not track.hr or all(h is None for h in track.hr):
        return lambda t: None

    def at(t):
        i = min(bisect_left(track.ts, t), len(track.ts) - 1)
        for j in (i, i - 1, i + 1, i - 2, i + 2, i - 5, i + 5, i - 10, i + 10):
            if 0 <= j < len(track.ts) and track.hr[j] is not None and \
                    abs((track.ts[j] - t).total_seconds()) <= 10:
                return track.hr[j]
        return None

    return at


def make_pace_lookup(track):
    """timestamp (UTC) -> current pace in s/km, None when standing still.

    Moving = the distance channel averaged over PACE_WINDOW_S (counting
    only seconds with movement) is above walking-slow, AND either the speed
    channel or the cadence channel agrees — shuffling at a table moves
    metres by GPS but registers on neither. The value is the smooth speed
    channel; while that is still ramping up after a start, the distance
    channel may lift it, but no higher than a brisk walk (PACE_WALK_MS) —
    GPS distance jumps as you set off and would claim a run.
    """
    half = timedelta(seconds=PACE_WINDOW_S / 2)
    smooth_half = timedelta(seconds=PACE_SMOOTH_S / 2)
    lead = timedelta(seconds=PACE_LEAD_S)
    have_speed = any(v is not None for v in track.speed)
    have_cad = any(v is not None for v in track.cadence)

    def at(t):
        t = t + lead
        if t < track.ts[0] or t > track.ts[-1]:
            return None
        i0 = max(bisect_left(track.ts, t - half), 1)
        i1 = min(bisect_left(track.ts, t + half), len(track.ts) - 1)
        if i1 < i0:
            return None
        moved_m, moving_s, span_s = 0.0, 0.0, 0.0
        for i in range(i0, i1 + 1):
            dt = (track.ts[i] - track.ts[i - 1]).total_seconds()
            if not 0 < dt <= MOVING_MAX_GAP_S:
                continue
            span_s += dt
            dm = (track.dist_km[i] - track.dist_km[i - 1]) * 1000
            if dm / dt >= PACE_STILL_MS:
                moved_m += dm
                moving_s += dt
        if span_s <= 0 or moving_s < span_s / 2 or moved_m <= 0:
            return None
        from_dist = moved_m / moving_s
        if from_dist < MOVING_SPEED_MS:
            return None
        # corroboration at the moment itself (+-1 s), not across the window
        ic = min(bisect_left(track.ts, t), len(track.ts) - 1)
        near = range(max(ic - 1, 0), min(ic + 2, len(track.ts)))
        corroborated = (any((track.speed[i] or 0) > 0 for i in near) or
                        any((track.cadence[i] or 0) > 0 for i in near))
        if (have_speed or have_cad) and not corroborated:
            return None
        j0 = max(bisect_left(track.ts, t - smooth_half), 0)
        j1 = min(bisect_left(track.ts, t + smooth_half), len(track.ts) - 1)
        speeds = [v for v in track.speed[j0:j1 + 1] if v is not None]
        if speeds:
            from_speed = sum(speeds) / len(speeds)
            ms = max(from_speed, min(from_dist, PACE_WALK_MS))
        else:
            ms = from_dist
        return 1000.0 / ms

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


def add_render_args(ap):
    """--view/--width/--height/--dpi/--align, shared with gopro_batch.py."""
    ap.add_argument("--view", choices=VIEWS, default=None,
                    help="elevation profile, 2-D route map or lap race "
                         "(default: from event config)")
    ap.add_argument("--width", type=int, default=None,
                    help=f"image width px (default {IMG_WIDTH_PX} profile / "
                         f"{MAP_IMG_WIDTH_PX} map)")
    ap.add_argument("--height", type=int, default=None,
                    help=f"image height px (default {IMG_HEIGHT_PX} profile "
                         f"/ {MAP_IMG_HEIGHT_PX} map)")
    ap.add_argument("--dpi", type=int, default=IMG_DPI)
    ap.add_argument("--canvas", metavar="WxH", default=None,
                    help="also pad each PNG to a transparent full frame of "
                         "this size (e.g. 3840x2160) with the graphic in a "
                         "corner, so it needs no scaling in the editor")
    ap.add_argument("--anchor", default="bottom-right",
                    choices=["top-left", "top", "top-right", "bottom-left",
                             "bottom", "bottom-right"],
                    help="corner of the canvas for the graphic "
                         "(default bottom-right)")
    ap.add_argument("--margin", type=int, default=0,
                    help="gap between the graphic and the canvas edge, px "
                         "(default 0: flush in the corner)")
    ap.add_argument("--hr", action="store_true",
                    help="show the current heart rate (from the activity "
                         "file; needs timestamps)")
    ap.add_argument("--pace", action="store_true",
                    help=f"show the current pace, min/km over a "
                         f"{PACE_WINDOW_S} s window")
    ap.add_argument("--align", choices=["checkpoints", "linear", "none"],
                    default=None,
                    help="distance alignment mode (default: from event config)")


def resolve_render_args(args):
    """Fill view/size/align defaults from the loaded event config."""
    if args.canvas:
        m = re.fullmatch(r"(\d+)x(\d+)", args.canvas.lower())
        if not m:
            sys.exit(f"--canvas expects WxH, e.g. 3840x2160 (got {args.canvas})")
        args.canvas = (int(m.group(1)), int(m.group(2)))
    if args.view is None:
        args.view = VIEW
    default_w, default_h = IMG_WIDTH_PX, IMG_HEIGHT_PX
    if args.view == "map":
        default_w, default_h = MAP_IMG_WIDTH_PX, MAP_IMG_HEIGHT_PX
    elif args.view == "laps" and LAP_STYLE == "3d":
        default_w, default_h = LAPS_3D_IMG_WIDTH_PX, LAPS_3D_IMG_HEIGHT_PX
    if args.width is None:
        args.width = default_w
    if args.height is None:
        args.height = default_h
    if args.align is None:
        args.align = DISTANCE_ALIGN
    if math.isinf(OFFICIAL_TOTAL_KM) and (args.view != "laps"
                                          or args.align != "none"):
        sys.exit("The event config has no event.total_km — needed for the "
                 "profile/map views and for distance alignment")


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
    add_render_args(ap)
    ap.add_argument("--event", default=None, metavar="TOML",
                    help=f"event config file (default: {DEFAULT_EVENT_FILE})")
    ap.add_argument("--no-label", action="store_true",
                    help="don't burn the text label onto the images")
    ap.add_argument("--no-time", action="store_true",
                    help="omit the clock/elapsed-time line from the label")
    args = ap.parse_args()
    load_event(args.event)
    resolve_render_args(args)

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

    course = prepare_course(track, map_fn, laps=args.view == "laps")
    if course.laps:
        print(f"Laps: {course.laps.count} found"
              + (" (the last one unfinished)" if course.laps.tail else ""))
    os.makedirs(args.out_dir, exist_ok=True)

    burn = BURN_LABEL and not args.no_label
    show_time = SHOW_TIME and not args.no_time and has_timestamps(track)
    time_at = make_time_lookup(track, map_fn) if show_time else None
    moving_at = make_moving_lookup(track) if show_time else None
    gain_at = make_gain_lookup(track, map_fn)
    live = (args.hr or args.pace) and has_timestamps(track)
    hr_at = make_hr_lookup(track) if live else None
    pace_at = make_pace_lookup(track) if live else None
    if (args.hr or args.pace) and not live:
        print("--hr/--pace need timestamps in the activity file, skipped")
    for spec, custom in specs:
        km, desc = resolve_position(spec, track, map_fn)
        label = None
        if burn:
            when = time_at(km) if show_time else None
            start = track.ts[0] if show_time else None
            moving = moving_at(when) if show_time else None
            at = time_at(km) if live else None
            label = make_label(km, when, start, moving, gain_at(km), custom,
                               hr=hr_at(at) if live else None,
                               pace_s_km=pace_at(at) if live else None,
                               show_hr=bool(live and args.hr),
                               show_pace=bool(live and args.pace),
                               laps=(course.laps.state(km, when)
                                     if course.laps else None))
        name = f"{FILENAME_PREFIX}_km{km:05.1f}"
        if custom:
            name += "_" + re.sub(r"[^A-Za-z0-9]+", "-", custom).strip("-").lower()
        out = os.path.join(args.out_dir, name + ".png")
        render(course, km, out, label, args)
        print(f"  {desc:45s} -> {out}")


if __name__ == "__main__":
    main()
