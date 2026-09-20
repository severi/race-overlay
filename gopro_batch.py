#!/usr/bin/env python3
"""
gopro_batch.py — generate one race_overlay PNG per video clip.

Reads each clip's recording start time from its QuickTime/MP4 metadata (no
ffprobe needed), converts camera clock -> UTC, maps it to a course km via the
activity track, and renders one transparent overlay per clip, named after
the clip. iPhone .MOV files carry a timezone-aware creation date which is
used directly; for other cameras set camera.utc_offset_hours in the event
TOML to whatever the MP4 creation time is relative to UTC: a GoPro stamps its
local clock (so use that clock's offset), a DJI Osmo stamps UTC (use 0; the
local time is only in the file name).

With --mov each overlay is also encoded (ffmpeg) as a QuickTime Animation
video with alpha, as long as its clip plus a margin, and ANIMATED: the
position, distance, clock and next-checkpoint line are re-rendered every
--mov-step seconds of clip time, so a long clip stays right as you move.
Editors trim a video to any length, whereas a still image dropped in by
script keeps its default duration — so the videos are what
resolve_add_overlays.py places.

Usage:
    python gopro_batch.py my_run.fit /path/to/clips extra_clip.MOV \
        --out-dir overlays_clips --canvas 3840x2160 --mov
"""

import argparse
import datetime
import multiprocessing
import os
import re
import shutil
import struct
import subprocess
import tempfile
from bisect import bisect_left

import race_overlay as ro
from race_overlay import (
    build_km_mapper,
    load_track,
    make_gain_lookup,
    make_hr_lookup,
    make_label,
    make_moving_lookup,
    make_pace_lookup,
    prepare_course,
    render,
)

EPOCH_1904 = datetime.datetime(1904, 1, 1)
MOV_MARGIN_S = 2.0      # overlay video runs this much longer than its clip
MOV_KEYFRAME_EVERY = 250  # frames; RLE delta frames of a static image are
                          # tiny, keyframes are not — but seeking needs them


def clip_fps(path, default=25.0):
    """Frame rate of a clip via ffprobe, or the default if unavailable."""
    if not shutil.which("ffprobe"):
        return default
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30).stdout.strip()
        num, _, den = out.splitlines()[0].partition("/")
        return float(num) / float(den or 1)
    except (subprocess.SubprocessError, ValueError, IndexError, OSError):
        return default


def encode_overlay_video(frames, mov, fps):
    """Alpha video from [(png, seconds), ...] held frames (QuickTime
    Animation/RLE: an unchanged frame costs almost nothing)."""
    lst = mov + ".txt"
    with open(lst, "w") as f:
        for png, secs in frames:
            f.write(f"file '{os.path.abspath(png)}'\nduration {secs:.3f}\n")
        f.write(f"file '{os.path.abspath(frames[-1][0])}'\n")  # concat quirk
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
           "-i", lst, "-fps_mode", "cfr", "-r", f"{fps:g}", "-c:v", "qtrle",
           "-g", str(MOV_KEYFRAME_EVERY), "-pix_fmt", "argb", mov]
    subprocess.run(cmd, check=True)
    os.remove(lst)


# Shared state for the per-clip workers (set once in main, inherited via
# fork so nothing big is pickled).
_W = {}


def _label_key(label):
    if label is None:
        return None
    return (label["value"], label["gain"], tuple(label["right"]),
            label["bottom"], tuple(label.get("live") or []),
            repr(label.get("laps")), label.get("countdown"))


def _render_clip_video(job):
    """Worker: render the animated overlay for one clip -> (mov, frames)."""
    path, utc, seconds, out_mov = job
    track, course, lookup, args = (_W["track"], _W["course"], _W["lookup"],
                                   _W["args"])
    step = args.mov_step
    fps = clip_fps(path)
    with tempfile.TemporaryDirectory(prefix="overlay_frames_") as tmp:
        frames, prev, t, n = [], None, 0.0, 0
        while t < seconds:
            km, label = lookup(utc + datetime.timedelta(seconds=t))
            key = (round(km, 3), _label_key(label))
            if key == prev:
                frames[-1][1] += step
            else:
                png = os.path.join(tmp, f"f{n:05d}.png")
                n += 1
                render(course, km, png, label, args)
                frames.append([png, step])
                prev = key
            t += step
        encode_overlay_video(frames, out_mov, fps)
    return out_mov, n


def mvhd_info(path):
    """(recording start datetime — camera clock, duration seconds) from an MP4."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        def scan(start, end):
            pos = start
            while pos < end - 8:
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    return None
                alen, atype = struct.unpack(">I4s", hdr)
                off = 8
                if alen == 1:
                    alen = struct.unpack(">Q", f.read(8))[0]
                    off = 16
                if alen == 0:
                    alen = end - pos
                if atype == b"moov":
                    r = scan(pos + off, pos + alen)
                    if r:
                        return r
                elif atype == b"mvhd":
                    data = f.read(32)
                    if data[0] == 0:
                        ctime, _, tscale, dur = struct.unpack(">IIII", data[4:20])
                    else:  # version 1: 64-bit creation time and duration
                        ctime = struct.unpack(">Q", data[4:12])[0]
                        tscale = struct.unpack(">I", data[20:24])[0]
                        dur = struct.unpack(">Q", data[24:32])[0]
                    return (EPOCH_1904 + datetime.timedelta(seconds=ctime),
                            dur / tscale)
                pos += alen
            return None
        return scan(0, size)


def apple_creationdate(path):
    """Timezone-aware recording start from the QuickTime metadata iPhones
    embed (com.apple.quicktime.creationdate), or None if absent."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        chunks = [f.read(2_000_000)]
        f.seek(max(size - 2_000_000, 0))
        chunks.append(f.read())
    for data in chunks:
        i = data.find(b"com.apple.quicktime.creationdate")
        if i >= 0:
            m = re.search(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:?\d{2}",
                          data[i:i + 400])
            if m:
                return datetime.datetime.fromisoformat(m.group().decode())
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("activity", help=".fit or .gpx activity file")
    ap.add_argument("clips", nargs="+",
                    help="folder(s) of clips and/or individual .MP4/.MOV files")
    ap.add_argument("--out-dir", default="overlays_clips")
    ro.add_render_args(ap)
    ap.add_argument("--event", default=None, metavar="TOML",
                    help=f"event config file (default: {ro.DEFAULT_EVENT_FILE})")
    ap.add_argument("--camera-offset", type=float, default=None,
                    help="camera clock UTC offset in hours "
                         "(default: from event config)")
    ap.add_argument("--clock-behind", type=float, default=None, metavar="SEC",
                    help="seconds the camera clock is behind the watch "
                         "(default: camera.clock_behind_s from event config)")
    ap.add_argument("--no-label", action="store_true")
    ap.add_argument("--no-time", action="store_true",
                    help="omit the clock/elapsed-time line from the label")
    ap.add_argument("--mov", action="store_true",
                    help="also write each overlay as an animated alpha .mov "
                         "as long as its clip (needs ffmpeg); see "
                         "resolve_add_overlays.py")
    ap.add_argument("--mov-step", type=float, default=2.0, metavar="SEC",
                    help="re-render the overlay every SEC seconds of clip "
                         "time in the .mov (default 2)")
    ap.add_argument("--jobs", type=int, default=None,
                    help="parallel .mov renders (default: CPU count)")
    args = ap.parse_args()
    ro.load_event(args.event)
    ro.resolve_render_args(args)
    if args.mov and not shutil.which("ffmpeg"):
        raise SystemExit("--mov needs ffmpeg on the PATH")
    cam_offset = (args.camera_offset if args.camera_offset is not None
                  else ro.CAMERA_UTC_OFFSET_HOURS)
    clock_behind = (args.clock_behind if args.clock_behind is not None
                    else ro.CAMERA_CLOCK_BEHIND_S)

    track = load_track(args.activity)
    if not ro.has_timestamps(track):
        raise SystemExit("Mapping clips to course positions needs an "
                         "activity file with timestamps")
    moving_at = make_moving_lookup(track)
    hr_at = make_hr_lookup(track)
    pace_at = make_pace_lookup(track)
    map_fn, report = build_km_mapper(track, args.align)
    gain_at = make_gain_lookup(track, map_fn)
    print(f"Distance alignment: {report}")
    course = prepare_course(track, map_fn, laps=args.view == "laps")
    os.makedirs(args.out_dir, exist_ok=True)

    def lookup(utc_t):
        """(official km, label) for a moment in UTC."""
        i = min(bisect_left(track.ts, utc_t), len(track.ts) - 1)
        km = min(map_fn(track.dist_km[i]), ro.OFFICIAL_TOTAL_KM)
        label = None
        if not args.no_label:
            when = None if args.no_time else min(max(utc_t, track.ts[0]),
                                                 track.ts[-1])
            label = make_label(km, when,
                               None if args.no_time else track.ts[0],
                               None if args.no_time else moving_at(when),
                               gain_at(km),
                               hr=hr_at(utc_t) if args.hr else None,
                               pace_s_km=pace_at(utc_t) if args.pace else None,
                               show_hr=args.hr, show_pace=args.pace,
                               laps=(course.laps.state(km, utc_t)
                                     if course.laps else None))
        return km, label

    _W.update(track=track, course=course, lookup=lookup, args=args)

    paths = []
    for p in args.clips:
        if os.path.isdir(p):
            paths += sorted(os.path.join(p, f) for f in os.listdir(p)
                            if f.upper().endswith((".MP4", ".MOV")))
        else:
            paths.append(p)
    print(f"{len(paths)} clips\n")
    off = datetime.timedelta(hours=cam_offset)
    behind = datetime.timedelta(seconds=clock_behind)
    if clock_behind:
        print(f"camera clock correction: +{clock_behind:g} s")

    mov_jobs = []
    for path in paths:
        clip = os.path.basename(path)
        info = mvhd_info(path)
        if info is None:
            print(f"  {clip}: no mvhd atom found, SKIPPED")
            continue
        cam_time, dur = info
        # prefer the tz-aware Apple metadata (exact) over the camera-clock
        # offset assumption
        meta = apple_creationdate(path)
        if meta is not None:
            utc = meta.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            cam_time = meta.replace(tzinfo=None)
        else:
            utc = cam_time - off + behind

        note = ""
        if utc < track.ts[0]:
            note = f" (clip starts {(track.ts[0]-utc).total_seconds():.0f}s pre-start)"
        elif utc > track.ts[-1]:
            note = f" (clip starts {(utc-track.ts[-1]).total_seconds():.0f}s post-finish)"
        km, label = lookup(utc)
        stem = os.path.splitext(clip)[0]
        out = os.path.join(args.out_dir, f"overlay_{stem}_km{km:05.1f}.png")
        render(course, km, out, label, args)
        print(f"  {clip}  cam {cam_time:%H:%M:%S}  utc {utc:%H:%M:%S}  "
              f"dur {dur:5.1f}s  -> km {km:6.2f}  {os.path.basename(out)}{note}")
        if args.mov:
            mov_jobs.append((path, utc, dur + MOV_MARGIN_S,
                             os.path.splitext(out)[0] + ".mov"))

    if mov_jobs:
        print(f"\nrendering {len(mov_jobs)} animated overlay videos "
              f"(every {args.mov_step:g} s of clip time)...")
        ctx = multiprocessing.get_context("fork")
        with ctx.Pool(args.jobs) as pool:
            total = 0
            for mov, n in pool.imap(_render_clip_video, mov_jobs):
                size = os.path.getsize(mov)
                total += size
                print(f"  {os.path.basename(mov)}  {n:4d} frames  "
                      f"{size / 1e6:5.1f} MB")
        print(f"  total {total / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
