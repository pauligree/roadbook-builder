# Roadbook Builder

Turns a GPX track into a print-ready, multi-page A4 roadbook PDF: a real
OpenTopoMap basemap, the track colored by steepness (blue = descending, red =
climbing), waypoints pulled straight from the GPX, an elevation/gradient
profile, and a two-layer PDF (raster basemap + vector track/text) that opens
in Illustrator as separate, editable layers.

This is a standalone local version of a pipeline originally prototyped inside
a sandboxed AI coding session (see `PROCESS_NOTES.md` if you want the full
story of how it was built and why it looks the way it does). Running it here,
on your own machine, is actually **better** than that original prototype:
your machine has normal internet access, so basemap tiles are downloaded
directly — no browser workaround, and no ~1500px resolution cap. You can
push `--zoom` as high as you want for genuinely sharp print output.

## 1. One-time setup

Requires Python 3.10+ and git.

```bash
cd ~
git clone https://github.com/pauligree/roadbook-builder.git
cd roadbook-builder
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

To come back later in a new terminal session, you only need:

```bash
cd ~/roadbook-builder
source .venv/bin/activate
```

## 2. Running it

Drop a GPX file into `input/`, then:

```bash
python -m roadbook.run_pipeline "input/TOR450 Tor des Glaciers T1 V7.0 START - Rif. Sella - UP2025.gpx"
```

That single command runs all four steps and writes the final PDF to
`output/<slug>_roadbook.pdf`. Useful flags:

```bash
python -m roadbook.run_pipeline input/mytrack.gpx \
    --pages 4 \                 # how many A4 pages to split the track into
    --zoom 13 \                 # tile zoom — controls label style/detail, not sharpness (see below)
    --race-title "TOR450 - TOR DES GLACIERS" \   # overrides the auto-guessed header title
    --waypoint-radius-m 60      # how close a GPX <wpt> must be to the track to be shown
```

First run on a new machine will be slow-ish (downloading tiles); re-runs with
the same GPX are instant to re-render if you only change styling — see §4.

### Running the steps individually

Useful when tweaking one stage without re-downloading tiles:

```bash
python -m roadbook.parse_gpx      input/mytrack.gpx
python -m roadbook.fetch_basemap  work/mytrack_data.pkl --pages 4 --zoom 13
python -m roadbook.render_pages   work/mytrack_data.pkl work/mytrack_basemaps --pages 4
python -m roadbook.merge_layers   work/mytrack_vector.pdf work/mytrack_basemaps output/mytrack_roadbook.pdf
```

`work/` holds everything intermediate (parsed GPX cache, basemap tiles, the
two un-merged PDFs) so you can re-run just the rendering step after a style
change without re-fetching tiles. `output/` holds only the final deliverables.

## 2b. Web app (mockup)

A local browser UI on top of the same pipeline — pick a GPX, tweak options,
see a live schematic preview of every page (no tiles fetched), then render
the real PDF. See `PLAN.md` for where this is heading (Mac app, iPad/iPhone).

```bash
pip install -r requirements.txt     # adds fastapi/uvicorn
python -m webapp.server
# open http://127.0.0.1:8000
```

- **Overview** tab: whole track coloured by grade, each page's exact
  (tile-snapped) map area, elevation profile split by page. Click a page box.
- **Page layout** tab: A4 wireframe with the same geometry `render_pages.py`
  uses — header text, aspect-correct map panel, labels, profile, footer — plus
  tile count and approximate print ppi for the chosen zoom.
- **Final PDF** tab: the rendered roadbook, once you press *Render roadbook*.
- **Race presets:** the *Race preset* box at the top of Options saves the
  whole option set (pages, zoom, provider + style ID, tables, race title,
  radius, style) under a name, e.g. `TORX 2026`, as `presets/<name>.json`
  (gitignored). Picking a preset loads it; *Save* with an existing name
  overwrites it. API keys are never stored in presets. Per-track settings —
  custom page splits and moved labels — stay with the GPX, not the preset.
- **Deleting files:** click the **×** button beside a GPX or an output PDF, then
  **Delete?** to confirm. Files go to the macOS Trash, not straight to oblivion.

**Desktop launcher.** `scripts/make_desktop_app.sh` creates
`~/Desktop/Roadbook Builder.app`: double-click starts the server in the
background (if not already running; log in `work/server.log`) and opens the
app in your default browser. If the project lives inside a protected folder
(e.g. `~/Documents`), first launch may ask to allow access to it — click
*Allow*. Stop the server with
`scripts/launch.sh --stop`. Re-run the script if you move the project folder.

GPX files are listed from `input/` and `input-TORX/`; uploads go to `input/`.
Renders run one at a time and write to `output/` exactly like the CLI.

## 3. Choosing a zoom level

**Two different things are easy to confuse here — zoom does not control
label size.** Every page shows a *fixed* real-world area (set by how the
track is split into pages, not by zoom) squeezed into a *fixed* print
panel, so the map scale (real meters per printed inch) never changes with
zoom. What zoom actually selects is *which pre-designed map style
renders*: low zoom pulls in the tile provider's decluttered "regional
overview" style — fewer labels, each with more visual weight, meant to be
read at a glance. High zoom pulls in the "close-up navigation" style —
more real detail (minor trails, spot elevations, dense contour numbers)
but many small labels packed tightly, meant for viewing a tiny area. Force
that high-zoom style to cover several km on one A4 page and everything
shrinks to fit.

**So: lower `--zoom` for bigger-looking native labels. Raise it for more
real terrain detail** (extra trails, finer contour intervals) **at the
cost of denser, smaller text.** Each level up roughly quadruples tile
count/download time/disk use for the same area, and (independently)
roughly doubles embedded print resolution:

| zoom | feel | approx. print ppi (non-retina / retina) |
|---|---|---|
| 13 (default) | decluttered overview style, biggest native labels | ~90 / ~180 |
| 14 | a bit more trail/contour detail | ~180 / ~360 |
| 15 | noticeably denser labels | ~360 / ~720 |
| 16+ | close-up navigation style — small, packed labels | ~720+ / ~1440+ |

(ppi figures assume a page covering ~9km printed into a ~7.5in map panel —
your actual numbers scale with however much ground your page covers.)

13 is the default: at any reasonable zoom the embedded resolution is
already well past print quality (300ppi), so the label-size/detail
tradeoff above — not sharpness — is what actually drives your choice.
Raising zoom does **not** fix small text; if anything it makes native
labels smaller. The vector-layer text this pipeline draws itself
(waypoint names, header, footer — see `render_pages.py`'s font-size
constants) is unaffected either way, since that's independent real vector
type, not part of the fetched basemap image.

The merged PDF embeds `page{N}_raw.jpg` directly at its native fetched
resolution (see §5, "Resolution") — so whatever `--zoom` you fetch at is
exactly what ends up in the final print file, no extra downsampling in
between.

### Retina/HiDPI tiles via `--provider mapbox` or `--provider maptiler`

OpenTopoMap (the default provider when no MapTiler key is configured — see
`config.local.json` below) does not serve a genuine `@2x` tile
variant — its tiles come at one fixed pixel density regardless of screen
DPI (confirmed against the OSM wiki's tile provider comparison, which
lists retina support explicitly for other providers but not for
OpenTopoMap).

If you have a Mapbox access token, `fetch_basemap.py` can pull genuinely
higher-density tiles from Mapbox's Static Tiles API instead — the same
labels rendered sharper/crisper (not bigger — see §3 above for what
actually controls apparent label size), not an interpolated upscale:

```bash
python -m roadbook.run_pipeline input/mytrack.gpx --provider mapbox --api-key pk.xxxxxxxx
# or, to avoid putting the token on the command line / in shell history:
export MAPBOX_ACCESS_TOKEN=pk.xxxxxxxx
python -m roadbook.run_pipeline input/mytrack.gpx --provider mapbox
```

Notes:

- Default style is `mapbox/outdoors-v12` (contours, trails, terrain shading
  — the closest match to OpenTopoMap's look). Override with `--mapbox-style
  your-username/your-style-id` if you've built a custom style in Mapbox
  Studio.
- Tiles are requested at `tilesize=256` + `@2x`, i.e. genuine 512×512px
  retina tiles on the exact same tile grid `--zoom` already uses for
  OpenTopoMap — no other tile math changes. Mapbox's own docs note this
  costs **4x the API requests** of their default 512px tiles for the same
  area (their default trades off request volume against pixel density
  differently); check your plan's request quota if you're batch-processing
  many sectors. See [Mapbox's Static Tiles
  pricing](https://docs.mapbox.com/api/maps/static-tiles/#static-tiles-api-pricing).
- **Attribution is stricter than OpenTopoMap's.** OpenTopoMap only needs the
  on-map credit line this pipeline already draws. Mapbox's terms for
  [static/print
  use](https://docs.mapbox.com/help/dive-deeper/attribution/#static--print)
  require the text `© Mapbox, © OpenStreetMap` near the image at minimum —
  the pipeline sets this automatically when `--provider mapbox` is used —
  but printed/distributed roadbooks (e.g. handed out at a race) may fall
  under different terms than personal use. This isn't legal advice — check
  Mapbox's [attribution guide](https://docs.mapbox.com/help/dive-deeper/attribution/)
  and [terms of service](https://www.mapbox.com/tos/) against your actual
  distribution plan before printing at scale.
- A bad or missing token fails fast with a clear error before any tiles are
  fetched, rather than silently producing a blank basemap.

**MapTiler is also supported**, same idea, different account/API:

```bash
export MAPTILER_API_KEY=xxxxxxxx
python -m roadbook.run_pipeline input/mytrack.gpx --provider maptiler
```

- Default map ID is `topo-v4`. MapTiler occasionally renames/reversions
  its style catalog — if you get a `404 Style not found` error, check
  [cloud.maptiler.com/maps/](https://cloud.maptiler.com/maps/) (in your
  MapTiler account) for the current outdoor map's ID and pass it with
  `--maptiler-map-id`.
- To avoid exporting the key every time, put it (and optionally your own
  map ID) in `config.local.json` at the project root — gitignored, used by
  both the CLI and the web app when no `--api-key` / env var is given:
  `{"maptiler": {"api_key": "xxxx", "style_id": "<map id>"}}`
  Easiest: `cp config.local.example.json config.local.json`, then edit it.
  With a MapTiler key configured (here or via the env var), **maptiler
  becomes the default provider** for both the CLI and the web app.
- Also requests genuine 512×512px `@2x` retina tiles on the same tile grid
  as OpenTopoMap — no other tile math changes.
- Attribution requirement is `© MapTiler © OpenStreetMap contributors`
  (set automatically); check MapTiler's own
  [attribution guide](https://docs.maptiler.com/guides/map-design/attribution/add-attribution/)
  and [terms](https://www.maptiler.com/terms/) before printing/distributing
  at scale, same caveat as Mapbox above.
- Same fail-fast behavior on a missing/bad key or a bad map ID.

## 4. Customizing the look

Edit the constants near the top of `roadbook/render_pages.py`, then just
re-run the render + merge steps (no need to re-fetch tiles):

```python
GRADIENT_SMOOTH_WINDOW = 11        # raise if the color looks noisy on GPS jitter
GRADIENT_VMAX_CLIP = (15, 35)      # % grade mapped to full blue/full red
CMAP_NAME = "RdBu_r"               # any matplotlib diverging colormap
HEIGHT_RATIOS = [0.5, 8.35, 0.95, 0.2]   # header / map / profile / footer split
```

Then:

```bash
python -m roadbook.render_pages work/mytrack_data.pkl work/mytrack_basemaps --pages 4
python -m roadbook.merge_layers work/mytrack_vector.pdf work/mytrack_basemaps output/mytrack_roadbook.pdf
```

### Page split (`--splits`)

By default the track is cut into `--pages` equal-distance pages. To end
pages at specific points instead (e.g. at refuges), pass the distances in
km — the page count becomes the number of splits + 1:

```bash
python -m roadbook.run_pipeline input/mytrack.gpx --splits 11.95,139.16
```

The web app does the same with *Page split → Custom*: tick the waypoints
where pages should end. Each page header carries a subtitle: `km 41.5 →
km 83.0` for an equal split, the start/end waypoint names (`Rif. Bonatti →
Rif. Deffeyes`) for a custom one. Either way, each page's map is sized to fill the
whole map panel (no blank bands) — the basemap box is grown to the panel's
proportions around the page's track.

### Waypoint tables

Two kinds of table page, both on by default:

- **After each map page** (`--no-part-tables` to turn off): that part's
  points, with the map page's header repeated (title, sector line +
  subtitle, stats). When a page edge isn't at a waypoint (equal-distance
  split), italic *Part N start* / *Part N end* rows mark it, so the rows
  chain from the page start to the page end.
- **Final table** (`--no-final-table`): every point on the track, with a
  header covering the whole track.

Columns: point name, km from start, km / D+ / D- from the previous point,
D+ / D- to the next point, D+ / D- from the start. Rows are the GPX
waypoints plus track start and finish; "previous" and "next" run along the
whole track, also across part breaks. D+/D- use the same elevation sums as
the header stats. A list longer than ~74 points continues on another page.
Table pages carry no basemap. Web app: two checkboxes under Options; in
*Page layout*, ‹ › step through every page in print order and the
*All points* chip jumps to the final table.

### Style file (`--style`)

Label font, line widths, colour scale and hand-placed label positions can
also be set per render with a JSON file — this is what the web app's
*Style* panel sends:

```json
{
  "label_font": "Avenir Next Condensed",
  "track_width": 2.0,
  "profile_width": 1.2,
  "grade_colors": ["#1a9850", "#e6c229", "#d73027"],
  "grade_max": 25,
  "label_offsets": {"map:1:12.40": [-90, 20], "prof:1:12.40": [10, -30]}
}
```

```bash
python -m roadbook.run_pipeline input/mytrack.gpx --style mystyle.json
```

Every key is optional (see `DEFAULT_STYLE` in `render_pages.py`).
`label_offsets` keys are `map|prof:<page>:<km of the POI>` (or
`map:<page>:start|end`); values are points from the labelled point to the
label's top-left corner, y down. In the web app you set them by dragging
labels in *Page layout* — they're remembered per GPX file in the browser.

## 5. Using the output

**Illustrator.** Open the PDF normally. The Layers panel shows two entries:
*Basemap (raster)* and *Track, labels & text (vector)*. Everything in the
vector layer — headers, waypoint names, distances, scale bar, "km" labels —
is real, editable type; the track is real vector paths, colored per segment.
Hide/lock/reorder the layers independently, or select and edit any text/path
directly.

**Printing.** Pages are true A4 (210×297mm) with minimal margins. Print at
**100% / "Actual size"** — the scale bar and grid are only accurate at 1:1.
Don't let the print driver "fit to page."

**Swapping in a different basemap later.** Since the basemap is its own
layer, you can delete it and paste in a higher-res export from QGIS or IGN
Géoportail if you ever need one — as long as you scale/position it to match
the vector layer's page frame, everything else lines up.

**Resolution.** The basemap image in the merged PDF is the raw
`page{N}_raw.jpg` fetched from the tile server, embedded directly at its
native pixel resolution — it is not re-rasterized or downsampled by
matplotlib on the way in. Sharpness in the final PDF is controlled entirely
by `--zoom` in step 2 (see §3); the intermediate `work/*_raster.pdf` is a
low-res preview only and isn't used in the merge.

## 6. GPX filename convention (optional but recommended)

If your GPX filenames follow the pattern used for the TOR450 files —

```
<RACE NAME> T<N> V<version> <START NAME> - <END NAME> - UP<year>.gpx
```

e.g. `TOR450 Tor des Glaciers T2 V7.0 Rif. Sella - Oyace - UP2025.gpx` — the
header/subtitle/footer text is filled in automatically (race name, sector
code, start/end names, revision). Anything that doesn't match this pattern
still works fine; the header just falls back to the bare filename, or you can
force it with `--race-title`.

**Single-page override.** Any filename containing **"backup route"**
(case-insensitive) is always rendered as a single page, ignoring `--pages` —
useful for short alternate/backup route files that don't warrant splitting
across multiple A4 sheets. To add more trigger phrases, edit
`SINGLE_PAGE_FILENAME_MARKERS` at the top of `roadbook/run_pipeline.py`.

## 7. Batch-processing all sectors of a race

Point `run_pipeline` at a **directory** instead of a single file and it scans
it for every `*.gpx` and runs the full pipeline on each one:

```bash
python -m roadbook.run_pipeline input/
python -m roadbook.run_pipeline input/ --pages 4 --zoom 13   # flags apply to every file
```

Each GPX gets its own `work/<slug>_*` intermediates and its own
`output/<slug>_roadbook.pdf`. If one file fails (bad GPX, network hiccup),
the rest of the batch keeps going — a pass/fail summary prints at the end.
`--out` is ignored in directory mode (only meaningful for a single file).

## 8. Tile usage / attribution

Tiles come from OpenTopoMap (built on OpenStreetMap + SRTM data), licensed
CC-BY-SA. The rendered pages always include the required attribution line in
the map's corner — don't remove it if you redistribute the PDFs. The fetch
script sets a descriptive `User-Agent` and uses modest concurrency
(`--workers`, default 4); please don't crank that way up for large batch
jobs.

**Tile cache.** Every downloaded tile is kept in `work/tiles/<provider>/<style>/z/x/y.tile`
and reused by later renders, so re-rendering the same race after tweaking
options fetches nothing new (the console says `N from cache`). Tiles older
than 30 days are downloaded again — maps change, and Mapbox/MapTiler only
allow caching for a limited time. The API key is never stored with them.
Delete `work/tiles/` any time to start fresh, or pass `--no-tile-cache` to
`run_pipeline` / `fetch_basemap` to bypass it for one run.

## 9. Known limitations

- The map preserves true geographic proportions (`ax_m.set_aspect(1.0 /
  cos(lat))` — a lon-degree covers less ground than a lat-degree away from
  the equator, so a naive 1:1 stretch would distort the track). Because of
  this, the map panel doesn't always fill the full page width; matplotlib
  shrinks the panel to the correct shape and centers it horizontally in the
  available frame, so you'll see even blank margins on either side rather
  than a stretched image. If you'd rather have a full-bleed map and accept
  a small (generally <20%) geographic distortion instead, change that line
  to `ax_m.set_aspect("auto")`.
- Waypoint labels can occasionally overlap when two POIs are very close
  together (e.g. right at a page's start/end). Cosmetic only.
- Elevation values come straight from the GPX; if your device/track has
  noisy barometric or GPS altitude, consider smoothing the source file first
  (e.g. in GPSBabel) — `GRADIENT_SMOOTH_WINDOW` only smooths the gradient
  *color*, not the profile line's raw shape.

## 10. Folder structure

```
roadbook-builder/
├── README.md                 (this file)
├── requirements.txt
├── input/                    put source .gpx files here
├── work/                     intermediates (parsed cache, tiles, un-merged PDFs) — safe to delete, regenerable;
│                             move it with ROADBOOK_WORK_DIR or "work_dir" in config.local.json
├── output/                   final roadbook PDFs
├── presets/                  web app race presets (<name>.json), created on first save
└── roadbook/
    ├── parse_gpx.py          step 1: GPX -> work/<slug>_data.pkl
    ├── fetch_basemap.py      step 2: tiles -> work/<slug>_basemaps/ (pageN_raw.jpg, bounds.json)
    ├── render_pages.py       step 3: -> work/<slug>_raster.pdf (preview), _vector.pdf,
    │                                    work/<slug>_basemaps/map_rects.json
    ├── merge_layers.py       step 4: raw JPGs + _vector.pdf -> output/<slug>_roadbook.pdf (2 PDF layers)
    ├── run_pipeline.py       runs all four steps in one command
    └── api.py                analyze()/render() for front-ends
webapp/
    ├── server.py             FastAPI server (python -m webapp.server)
    └── static/               index.html, app.js, style.css (no build step)
```
