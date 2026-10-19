# Process notes — how this pipeline came to be

This documents the *why* behind some of the less obvious choices in
`roadbook/*.py`. The scripts in this folder are a cleaned-up, standalone
rewrite of a pipeline first prototyped inside a sandboxed AI coding session
(no direct internet access there, browser automation only) — most of that
prototype's constraints don't apply here, but the reasoning is worth keeping.

## Why fetch tiles ourselves instead of using a slippy-map screenshot

The very first prototype tried screenshotting a live OpenTopoMap web page.
That has two problems that don't show up until you check pixel-for-pixel:

1. **Alignment.** A screenshot of an interactive map is only as accurate as
   your ability to read back its exact geographic bounds, and slippy maps use
   Web Mercator, not plain lat/lon — small errors compound into a track that
   doesn't sit exactly on the terrain.
2. **Resolution.** Screenshot pathways (browser automation, remote desktop,
   etc.) are usually capped to a "reasonable for viewing" resolution — in the
   original sandbox this was a hard ~1300–1500px per image, confirmed by
   checking the saved file on disk, not just an on-screen preview.

Fetching raw XYZ tiles directly (`fetch_basemap.py`) sidesteps both: tile
edges have exact, computable lat/lon bounds (`deg2tile`/`tile2lon`/`tile2lat`
— standard slippy-map math), and there's no artificial resolution ceiling —
you get however many pixels `--zoom` implies.

## Why the PDF is built as two separate renders, not one

`render_pages.py` calls `build_page()` twice per page — once with only a
low-res preview of the basemap image (`mode="raster"`), once with everything
else on a transparent background (`mode="vector"`) — then `merge_layers.py`
stacks a layer built from each with real PDF Optional Content Groups (OCGs).

The alternative (rendering everything once, then trying to sort "which
objects in this one PDF are raster vs vector" after the fact) is much more
fragile — you'd be parsing/classifying an already-flattened content stream.
Rendering twice from the same layout code guarantees the two results align
pixel-for-pixel (same GridSpec, same axes limits) and keeps the split clean
by construction.

**The actual basemap layer in the merged PDF isn't the matplotlib raster
render, though.** Early versions embedded it by having matplotlib rasterize
`ax_m.imshow(...)` on savefig — but savefig's dpi caps how many pixels of
the fetched tile mosaic actually make it into the PDF, silently throwing
away most of the detail `--zoom` had fetched. Fixed by having
`render_pages.py` compute and export the map panel's exact rectangle (as a
fraction of the page, after the aspect/centering fix below) to
`map_rects.json`, and having `merge_layers.py` insert `page{N}_raw.jpg`
directly into the raster OCG at that rectangle via
`fitz.Page.insert_image()` — bypassing matplotlib's rasterization for the
basemap entirely. The `mode="raster"` matplotlib pass is now just a quick
low-res preview (`work/*_raster.pdf`), unused by the actual merge.

## Why gradient (%), not elevation, drives the color

Elevation-based coloring (a fixed color per altitude, e.g. green valleys ->
brown peaks) looks nice but isn't very useful mid-race — you already know
roughly how high you are from the profile axis. Coloring by **steepness**
(computed as smoothed `Δelevation/Δdistance` per segment, §`compute_gradient`
in `render_pages.py`) highlights the thing that actually matters
underfoot — where the punishing climbs and technical descents are — directly
on the map, not just in the profile.

The smoothing window (`GRADIENT_SMOOTH_WINDOW`) exists because raw
point-to-point GPS/barometric elevation is noisy; without smoothing, the
color flickers between extremes on flat ground.

## Why every page shows its own start/end markers

With a multi-page split, it's easy to lose track of where page 2 picks up
relative to where page 1 left off. Every page marks its own segment's start
(green circle) and end (red square) with the distance and a
"(from part N)" / "(continues part N+1)" label — only the true race start and
finish get "START"/"FINISH" instead. This was a specific fix for early drafts
where only the first and last page had endpoint markers, making the
interior page breaks hard to follow.

## Why waypoints are distance-matched, not manually listed

Race GPX files often ship one big waypoint list for the *entire* multi-stage
race, not just the segment in this particular file (true for the TOR450
source files — 96 waypoints in a single-sector GPX). `parse_gpx.py` keeps
only the waypoints within `--waypoint-radius-m` of the actual track, so
`Col de la Seigne`, `Rifugio Deffeyes`, etc. show up automatically without
maintaining a POI list by hand — and it stays correct if the race organizer
updates the GPX.
