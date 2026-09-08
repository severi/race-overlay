#!/usr/bin/env python3
"""
resolve_add_overlays.py — drop the clip overlays onto a DaVinci Resolve
timeline, one PNG per clip, on their own video track.

Runs INSIDE Resolve (Workspace > Scripts), which is the only place the
free edition exposes its scripting API. Install a launcher in
  ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/
that runs this file with the settings below filled in, e.g.:

    import runpy
    runpy.run_path("/path/to/resolve_add_overlays.py", init_globals={
        "resolve": resolve,
        "OVERLAY_DIR": "/path/to/overlays_clips",
    })

For every clip on the current timeline whose source file has an overlay
(overlay_<clip stem>_km<km>.mov / .png, as gopro_batch.py --mov writes them)
the overlay is imported into an "Overlays" bin and placed on a video track
named "Overlays" at the same timeline position and length as the clip —
and, for pieces cut from a longer clip, starting at the same offset into
the overlay video, so an animated overlay stays in sync with the footage.

Use the .mov overlays (gopro_batch.py --mov): Resolve trims a video to any
length, but ignores the requested length for a still image and places it
at the "Standard still duration" preference instead, so PNGs are only a
fallback and come out that long. Overlays rendered with --canvas at the
timeline size (e.g. 3840x2160) are placed as-is; smaller ones are zoomed
into a corner as a best guess. MODE "replace" (default) empties the
"Overlays" track and bin first and places an overlay on every clip;
"update" only re-places overlays that are already there (so ones you
removed on purpose stay removed); "add" only fills clips that have none.
Note that re-rendering the overlay files in place is enough on its own
when only the look changes — Resolve reads them by path.
"""

import os
import re

# --- settings (override via init_globals from the launcher) ---------------
OVERLAY_DIR = globals().get("OVERLAY_DIR", "overlays_clips")
TRACK_NAME = globals().get("TRACK_NAME", "Overlays")
BIN_NAME = globals().get("BIN_NAME", "Overlays")
# overlay width as a fraction of the frame width, and which corner
WIDTH_FRAC = float(globals().get("WIDTH_FRAC", 1 / 3))
ANCHOR = globals().get("ANCHOR", "bottom-left")   # top/bottom-left/right
MARGIN_FRAC = float(globals().get("MARGIN_FRAC", 0.03))  # of frame height
# only place overlays on clips from these video tracks (None = all)
SOURCE_TRACKS = globals().get("SOURCE_TRACKS", None)
# "replace": clear the Overlays track and place an overlay for every clip
# "update":  re-place only where an overlay already is (ones you deleted on
#            purpose stay gone; picks up new files / placement fixes)
# "add":     only add overlays to clips that have none
MODE = str(globals().get("MODE", "replace")).lower()

OVERLAY_RE = re.compile(r"^overlay_(.+)_km\d{3}\.\d\.(mov|png)$",
                        re.IGNORECASE)


def get_resolve():
    r = globals().get("resolve")
    if r is not None:
        return r
    try:
        import DaVinciResolveScript as dvr
        return dvr.scriptapp("Resolve")
    except ImportError:
        return None


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def overlay_index(directory):
    """clip stem (lower-case, no extension) -> overlay path; a .mov wins
    over a .png for the same clip."""
    idx = {}
    for name in sorted(os.listdir(directory)):
        m = OVERLAY_RE.match(name)
        if not m:
            continue
        stem = m.group(1).lower()
        if stem not in idx or name.lower().endswith(".mov"):
            idx[stem] = os.path.join(directory, name)
    return idx


def clip_stem(item):
    mpi = item.GetMediaPoolItem()
    name = None
    if mpi is not None:
        name = mpi.GetClipProperty("File Path") or mpi.GetName()
    name = name or item.GetName()
    return os.path.splitext(os.path.basename(name))[0].lower()


def source_offset(item):
    """Frames trimmed off the head of a timeline item, i.e. where in its
    source clip the piece starts."""
    try:
        off = item.GetLeftOffset()
    except Exception:
        off = None
    if off is None:
        try:
            off = item.GetSourceStartFrame()
        except Exception:
            off = 0
    return max(int(off or 0), 0)


def find_track(timeline, name):
    for i in range(1, timeline.GetTrackCount("video") + 1):
        if timeline.GetTrackName("video", i) == name:
            return i
    return None


def find_bin(media_pool, name):
    root = media_pool.GetRootFolder()
    for f in root.GetSubFolderList():
        if f.GetName() == name:
            return f
    return media_pool.AddSubFolder(root, name)


def frame_size(project, timeline):
    w = int(timeline.GetSetting("timelineResolutionWidth")
            or project.GetSetting("timelineResolutionWidth"))
    h = int(timeline.GetSetting("timelineResolutionHeight")
            or project.GetSetting("timelineResolutionHeight"))
    return w, h


def base_scale(project, timeline, iw, ih, fw, fh):
    """How Resolve scales a still of iw x ih into the frame before any
    zoom, per the mismatched-resolution setting."""
    mode = (timeline.GetSetting("timelineInputResMismatchBehavior")
            or project.GetSetting("timelineInputResMismatchBehavior") or "")
    if mode == "scaleToFit":
        return min(fw / iw, fh / ih)
    if mode == "scaleFullFrame":
        return max(fw / iw, fh / ih)
    return 1.0  # centerCrop / stretch / unknown: no uniform resize


def transform_for(iw, ih, fw, fh, base):
    """(zoom, pan, tilt) putting the overlay in the chosen corner."""
    target_w = WIDTH_FRAC * fw
    zoom = target_w / (iw * base)
    ow, oh = target_w, ih * base * zoom
    margin = MARGIN_FRAC * fh
    pan = fw / 2 - margin - ow / 2
    tilt = fh / 2 - margin - oh / 2
    if "left" in ANCHOR:
        pan = -pan
    if "bottom" in ANCHOR:
        tilt = -tilt
    return zoom, pan, tilt


def main():
    resolve = get_resolve()
    if resolve is None:
        print("Could not reach Resolve — run this from Workspace > Scripts")
        return
    project = resolve.GetProjectManager().GetCurrentProject()
    timeline = project.GetCurrentTimeline() if project else None
    if timeline is None:
        print("Open the project and timeline first")
        return
    if not os.path.isdir(OVERLAY_DIR):
        print(f"Overlay folder not found: {OVERLAY_DIR}")
        return
    overlays = overlay_index(OVERLAY_DIR)
    n_mov = sum(p.lower().endswith(".mov") for p in overlays.values())
    print(f"{len(overlays)} overlays in {OVERLAY_DIR} ({n_mov} videos, "
          f"{len(overlays) - n_mov} stills)")
    if n_mov < len(overlays):
        print("  NOTE: still-image overlays keep Resolve's standard still "
              "duration — render with gopro_batch.py --mov for exact "
              "lengths")
    print(f"Timeline '{timeline.GetName()}'")

    # what needs an overlay: (clip item, overlay path)
    jobs, missing = [], []
    n_tracks = timeline.GetTrackCount("video")
    for t in range(1, n_tracks + 1):
        if timeline.GetTrackName("video", t) == TRACK_NAME:
            continue
        if SOURCE_TRACKS and t not in SOURCE_TRACKS:
            continue
        for item in timeline.GetItemListInTrack("video", t) or []:
            stem = clip_stem(item)
            path = overlays.get(stem)
            if path:
                jobs.append((item, path))
            else:
                missing.append(item.GetName())
    print(f"{len(jobs)} clips with an overlay, {len(missing)} without")
    for name in missing:
        print(f"  no overlay: {name}")
    if not jobs:
        return

    media_pool = project.GetMediaPool()
    folder = find_bin(media_pool, BIN_NAME)
    media_pool.SetCurrentFolder(folder)

    if MODE not in ("replace", "update", "add"):
        print(f"MODE must be replace, update or add (got {MODE!r})")
        return
    track = find_track(timeline, TRACK_NAME)
    old_items = list(timeline.GetItemListInTrack("video", track) or []
                 if track is not None else [])
    old_starts = {i.GetStart() for i in old_items}
    if MODE == "update":
        jobs = [(it, p) for it, p in jobs if it.GetStart() in old_starts]
        print(f"update: {len(jobs)} clips currently have an overlay")
    elif MODE == "add":
        jobs = [(it, p) for it, p in jobs if it.GetStart() not in old_starts]
        print(f"add: {len(jobs)} clips have no overlay yet")
        old_items = []
    if not jobs:
        return
    if old_items:
        if timeline.DeleteClips(old_items):
            print(f"removed {len(old_items)} old overlays from "
                  f"'{TRACK_NAME}'")
        else:
            print(f"could not clear '{TRACK_NAME}' — remove old "
                  f"overlays by hand")
    if MODE != "add":
        # re-import so re-rendered files (and, for stills, the current
        # still-duration preference) are picked up
        stale = folder.GetClipList() or []
        if stale:
            if media_pool.DeleteClips(stale):
                print(f"re-importing {len(stale)} overlays")
            else:
                print("could not remove old overlays from the bin, keeping them")

    have = {os.path.basename(c.GetClipProperty("File Path") or c.GetName()): c
            for c in folder.GetClipList() or []}
    need = sorted({p for _, p in jobs if os.path.basename(p) not in have})
    if need:
        imported = media_pool.ImportMedia(need) or []
        for c in imported:
            have[os.path.basename(c.GetClipProperty("File Path")
                                  or c.GetName())] = c
        print(f"imported {len(imported)} overlays into bin '{BIN_NAME}'")

    if track is None:
        if not timeline.AddTrack("video"):
            print("Could not add a video track")
            return
        track = timeline.GetTrackCount("video")
        timeline.SetTrackName("video", track, TRACK_NAME)
        print(f"added video track V{track} '{TRACK_NAME}'")
    existing = {(i.GetName(), i.GetStart())
                for i in timeline.GetItemListInTrack("video", track) or []}

    fw, fh = frame_size(project, timeline)
    print(f"frame {fw}x{fh}")
    placed, skipped, failed, short = 0, 0, 0, []
    warned_size = False
    for item, path in jobs:
        png = os.path.basename(path)
        mpi = have.get(png)
        if mpi is None:
            print(f"  not in media pool (import failed?): {png}")
            continue
        start, dur = item.GetStart(), item.GetDuration()
        if (png, start) in existing or (mpi.GetName(), start) in existing:
            skipped += 1
            continue
        size = png_size(path)
        transform = None
        if size and size != (fw, fh):  # not full-frame: zoom into a corner
            base = base_scale(project, timeline, size[0], size[1], fw, fh)
            transform = transform_for(size[0], size[1], fw, fh, base)
            if not warned_size:
                print(f"  overlays are {size[0]}x{size[1]}, frame is "
                      f"{fw}x{fh}: zoomed to {WIDTH_FRAC:.0%} width as a "
                      f"guess — render with --canvas {fw}x{fh} to avoid this")
                warned_size = True

        # a piece cut from the middle of a clip must show the overlay from
        # the same offset: the overlay video and the clip share a timebase
        # (both start at the clip's first frame, same fps)
        src_in = source_offset(item)
        # endFrame is exclusive: src_in + dur - 1 came out one frame short
        added = media_pool.AppendToTimeline([{
            "mediaPoolItem": mpi,
            "startFrame": src_in,
            "endFrame": src_in + dur,
            "mediaType": 1,
            "trackIndex": track,
            "recordFrame": start,
        }]) or []
        if not added:
            print(f"  FAILED to place {png} at frame {start}")
            failed += 1
            continue
        ov = added[0]
        got, got_start = ov.GetDuration(), ov.GetStart()
        if transform:
            zoom, pan, tilt = transform
            ov.SetProperty("ZoomX", zoom)
            ov.SetProperty("ZoomY", zoom)
            ov.SetProperty("Pan", pan)
            ov.SetProperty("Tilt", tilt)
        ok = got == dur and got_start == start
        if not ok:
            short.append((png, got_start, got, start, dur))
        placed += 1
        print(f"  {item.GetName():40s} frames {start}-{start + dur - 1} "
              f"src +{src_in} <- {png}" +
              ("" if ok else f"  MISMATCH: landed {got_start} +{got}"))

    print(f"\nplaced {placed}, already there {skipped}, failed {failed}")
    if short:
        print(f"  {len(short)} overlays did not land exactly on their clip:")
        for png, gs, g, s0, d in short:
            print(f"    {png}: wanted {s0} +{d}, got {gs} +{g}")
        if any(p.lower().endswith(".png") for p, *_ in short):
            print("  (stills ignore the requested length — use the .mov "
                  "overlays from gopro_batch.py --mov)")


main()
