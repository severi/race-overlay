# CLAUDE.md

Transparent "you are here" elevation-overlay PNGs for race videos.
User-facing usage is in README.md — don't duplicate it here.

## Commands

```bash
.venv/bin/python race_overlay.py <activity.fit> --km 34        # positions
.venv/bin/python gopro_batch.py <activity.fit> <clips-dir>     # per-clip
```

Always use `.venv/bin/python` (fitparse/matplotlib live in the venv).
No test suite; verify visually (see below).

## Architecture

- `race_overlay.py` — everything: TOML event config loading, FIT/GPX parsing,
  distance alignment, stat lookups, matplotlib rendering, CLI. `gopro_batch.py`
  imports it as a library (`import race_overlay as ro`) and mutates its
  module globals via `ro.load_event()` — import values dynamically
  (`ro.OFFICIAL_TOTAL_KM`), never `from race_overlay import CONSTANT`.
- `event.toml` — ALL event-specific data (checkpoints, course length,
  timezones, camera clock offset). Never hardcode event details in code;
  the repo is generic and public (github.com/severi/race-overlay).
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
  overlays needs the owner's GoPro folder — see project memory.
- FIT timestamps are UTC. Race-local clock = `event.utc_offset_hours`;
  cameras without tz metadata use `camera.utc_offset_hours` (a GoPro synced
  to a phone from home keeps home timezone). iPhone .MOVs carry their own tz.
- Garmin Ultra Run "rest" = manual lap at ~standstill, NOT a timer pause —
  that's what checkpoint auto-detection keys on.
- `GAIN_SMOOTH_WINDOW_M = 100` is calibrated so total ascent matches watch
  totals (raw GPS deltas overshoot ~60%). Don't "simplify" the two separate
  smoothing windows.
- Matplotlib + Avenir Next: no `↑`/`▲` glyphs — stick to ASCII in labels.
