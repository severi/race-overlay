#!/usr/bin/env python3
"""
gopro_batch.py — generate one race_overlay PNG per video clip.

Reads each clip's recording start time from its QuickTime/MP4 metadata (no
ffprobe needed), converts camera clock -> UTC, maps it to a course km via the
activity track, and renders one transparent overlay per clip, named after
the clip. iPhone .MOV files carry a timezone-aware creation date which is
used directly; for other cameras (e.g. GoPro) set camera.utc_offset_hours in
the event TOML to whatever timezone the camera clock was on.

Usage:
    python gopro_batch.py my_run.fit /path/to/clips extra_clip.MOV \
        --out-dir overlays_clips
"""

import argparse
import datetime
import os
import re
import struct
from bisect import bisect_left

import race_overlay as ro
from race_overlay import (
    build_km_mapper,
    load_track,
    make_gain_lookup,
    make_label,
    make_moving_lookup,
    prepare_profile,
    render,
)

EPOCH_1904 = datetime.datetime(1904, 1, 1)


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
                    else:
                        ctime = struct.unpack(">Q", data[4:12])[0]
                        tscale, dur = struct.unpack(">II", data[20:28])
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
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=400)
    ap.add_argument("--dpi", type=int, default=100)
    ap.add_argument("--event", default=None, metavar="TOML",
                    help=f"event config file (default: {ro.DEFAULT_EVENT_FILE})")
    ap.add_argument("--align", choices=["checkpoints", "linear", "none"],
                    default=None,
                    help="distance alignment mode (default: from event config)")
    ap.add_argument("--camera-offset", type=float, default=None,
                    help="camera clock UTC offset in hours "
                         "(default: from event config)")
    ap.add_argument("--no-label", action="store_true")
    ap.add_argument("--no-time", action="store_true",
                    help="omit the clock/elapsed-time line from the label")
    args = ap.parse_args()
    ro.load_event(args.event)
    if args.align is None:
        args.align = ro.DISTANCE_ALIGN
    cam_offset = (args.camera_offset if args.camera_offset is not None
                  else ro.CAMERA_UTC_OFFSET_HOURS)

    track = load_track(args.activity)
    moving_at = make_moving_lookup(track)
    map_fn, report = build_km_mapper(track, args.align)
    gain_at = make_gain_lookup(track, map_fn)
    print(f"Distance alignment: {report}")
    px, py = prepare_profile(track, map_fn)
    os.makedirs(args.out_dir, exist_ok=True)

    paths = []
    for p in args.clips:
        if os.path.isdir(p):
            paths += sorted(os.path.join(p, f) for f in os.listdir(p)
                            if f.upper().endswith((".MP4", ".MOV")))
        else:
            paths.append(p)
    print(f"{len(paths)} clips\n")
    off = datetime.timedelta(hours=cam_offset)

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
            utc = cam_time - off

        note = ""
        if utc < track.ts[0]:
            note = f" (clip starts {(track.ts[0]-utc).total_seconds():.0f}s pre-start)"
        elif utc > track.ts[-1]:
            note = f" (clip starts {(utc-track.ts[-1]).total_seconds():.0f}s post-finish)"
        i = min(bisect_left(track.ts, utc), len(track.ts) - 1)
        km = min(map_fn(track.dist_km[i]), ro.OFFICIAL_TOTAL_KM)

        label = None
        if not args.no_label:
            when = None if args.no_time else min(max(utc, track.ts[0]),
                                                 track.ts[-1])
            label = make_label(km, when,
                               None if args.no_time else track.ts[0],
                               None if args.no_time else moving_at(when),
                               gain_at(km))
        stem = os.path.splitext(clip)[0]
        out = os.path.join(args.out_dir, f"overlay_{stem}_km{km:05.1f}.png")
        render(px, py, km, out, label, args)
        print(f"  {clip}  cam {cam_time:%H:%M:%S}  utc {utc:%H:%M:%S}  "
              f"dur {dur:5.1f}s  -> km {km:6.2f}  {os.path.basename(out)}{note}")


if __name__ == "__main__":
    main()
