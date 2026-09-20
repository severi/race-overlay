# CLAUDE.md

Transparent "you are here" overlay PNGs (elevation profile, 2-D route map
or lap-race loop profile) for race videos. User-facing usage is in
README.md — don't duplicate it here.

## Commands

```bash
.venv/bin/python race_overlay.py <activity.fit> --km 34        # positions
.venv/bin/python gopro_batch.py <activity.fit> <clips-dir>     # per-clip
# other event / view:  --event events/nuuksio-classic-2026.toml --view map
```

Always use `.venv/bin/python` (fitparse/matplotlib live in the venv).
No test suite; verify visually (see below).

## Architecture

- `race_overlay.py` — everything: TOML event config loading, FIT/GPX parsing,
  distance alignment, stat lookups, matplotlib rendering (`_render_profile`,
  `_render_map` and `_render_laps`, dispatched by `render()` on
  `args.view`), CLI.
  `gopro_batch.py` imports it as a library (`import race_overlay as ro`) and
  mutates its module globals via `ro.load_event()` — import values
  dynamically (`ro.OFFICIAL_TOTAL_KM`), never `from race_overlay import
  CONSTANT`. Shared CLI flags live in `ro.add_render_args` /
  `ro.resolve_render_args` (view-dependent size defaults).
- `event.toml` / `events/*.toml` — ALL event-specific data (checkpoints,
  course length, timezones, camera clock offset, view, label sides). Never
  hardcode event details in code; the repo is generic and public
  (github.com/severi/race-overlay). `event.toml` = RTTS profile example,
  `events/nuuksio-classic-2026.toml` = loop-course map example,
  `events/messila-vertical-2026.toml` = timed lap race (laps view).
- `resolve_add_overlays.py` — runs inside DaVinci Resolve (free edition =
  Workspace > Scripts only, no external API), matches timeline clips to
  `overlay_<stem>_km*.mov` (from `gopro_batch.py --mov`: QuickTime
  Animation + alpha, animated every `--mov-step` s, unchanged frames
  merged via the ffmpeg concat demuxer, ~0.06 MB/s; the 79 Nuuksio clips
  take ~2 min on all cores) and places them on an "Overlays" track.
  Videos, not stills: Resolve's `AppendToTimeline` ignores startFrame/
  endFrame for stills (always the "Standard still duration" preference)
  and there is no API to trim a timeline item afterwards. Pieces cut from
  a clip get the overlay from the same source offset (`GetLeftOffset`).
  MODE replace/update/add; the launchers with the owner's paths live in
  `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/`
  (`Add Race Overlays.py` = replace, `Update Race Overlays.py` = update).
  The project DB (`Project.db`, SQLite; copy it before reading) is handy
  to see what a run actually did: `Sm2TiItem` rows = timeline items.
- Visual styling stays as constants at the top of `race_overlay.py`.

## Verifying render changes

Transparent PNGs look washed-out/invisible previewed on white. Composite
before judging:

```python
bg = Image.new('RGBA', img.size, (30, 50, 25, 255))  # and a bright bg too
bg.alpha_composite(img)
```

## Gotchas

- **Never commit activity files or overlays** — `.fit`/`.gpx` contain GPS +
  heart-rate data. `imports/`, `overlays*/` are gitignored deliberately.
- Local test data: `imports/*.fit` (not in git). Regenerating all clip
  overlays needs the owner's camera folders — see project memory.
- FIT timestamps are UTC. Race-local clock = `event.utc_offset_hours`;
  cameras without tz metadata use `camera.utc_offset_hours` = whatever the
  MP4 `mvhd` creation time is relative to UTC: GoPro stamps its local clock
  (synced to a phone from home = home timezone), DJI Osmo stamps UTC (offset
  0; local time only in the `DJI_YYYYMMDDhhmmss_…` file name). iPhone .MOVs
  carry their own tz.
- Camera clocks drift by seconds (`camera.clock_behind_s`, Nuuksio Osmo:
  11 s behind the watch). Symptom: pace shows "--:--" while visibly
  running. Diagnose by extracting frames around an aid-station stop/start
  (ffmpeg -ss) and comparing with the FIT distance/cadence transitions;
  don't paper over it with a bigger `PACE_LEAD_S` — that was tried.
- Garmin Ultra Run "rest" = manual lap at ~standstill, NOT a timer pause —
  that's what checkpoint auto-detection keys on first. With no manual rest
  laps (auto 1 km laps, plain run mode, GPX) `_detect_standstills` derives
  them from the samples instead; same thresholds.
- `GAIN_SMOOTH_WINDOW_M = 100` is calibrated so total ascent matches watch
  totals (raw GPS deltas overshoot ~60%). Don't "simplify" the two separate
  smoothing windows.
- Matplotlib + Avenir Next: no `↑`/`▲` glyphs — stick to ASCII in labels.
- Pace (`--pace`) is built from what each FIT channel is good at (see the
  comment block above `PACE_WINDOW_S`): distance decides *if* you move
  (6 s window, moving seconds only), speed or cadence at that moment must
  corroborate (table shuffles move metres on GPS alone), the smooth speed
  channel supplies the value, and while it is still ramping after a start
  the distance channel may lift it only to a brisk walk (`PACE_WALK_MS`) —
  GPS distance jumps as you set off. The value is averaged over
  `PACE_SMOOTH_S` = 5 s (owner's choice after comparing 1/3/6/12/20 s and
  reading up: Garmin instant pace lags 5-7 s, Stryd offers 3/10/30 s;
  12 s was rejected as too laggy, a deadband was declined). Display rounds
  to 5 s. Verified frame by frame against the three aid-station clips;
  don't simplify to a single channel — that was tried and rejected.
- Laps view: `prepare_laps` finds lap boundaries geometrically (passes of
  the first GPS fix), NOT from FIT laps — the Messilä file has a missed
  button press (first manual lap = two loops). All lap logic lives in
  `Laps.state(km, when)`; the label carries that dict (`label["laps"]`) and
  `_render_laps` draws from it. Two numbers, two meanings, never swapped
  mid-race (owner's call): big `LAP n` = lap in progress, `n completed` =
  laps that count. A lap overrunning the time limit turns "not counted" at
  the limit, not before. Timed lap events have no `total_km`
  (`OFFICIAL_TOTAL_KM = inf`, alignment `none`); other views refuse that.
  The loop profile is the per-fraction median of all laps, lightly smoothed
  (`LOOP_SMOOTH_WINDOW_M`) — the normal 150 m window shaves the summit.
- Map view: the stats column is drawn first and measured, and the map axes
  take the remaining width (long "Next <name>" lines widen the column).
  Route is a flat equirectangular projection around the track centre — fine
  for race-sized areas, don't reach for a projection library.
