"""
run_pipeline.py — runs all 4 steps for one GPX file, or for every GPX file in
a directory, in one command.

Usage:
    # single file
    python -m roadbook.run_pipeline "input/TOR450 ... T1 V7.0 START - Rif. Sella - UP2025.gpx"

    # a whole directory: scans it for *.gpx and runs the pipeline on each one
    python -m roadbook.run_pipeline input/
    python -m roadbook.run_pipeline input/ --pages 4 --zoom 13 --race-title "TOR450 - TOR DES GLACIERS"

    # also combine every sector's PDF into one single merged PDF
    python -m roadbook.run_pipeline input/ --merge

    # genuine retina tiles via Mapbox or MapTiler instead of OpenTopoMap
    python -m roadbook.run_pipeline input/mytrack.gpx --provider mapbox --api-key pk.xxxx
    python -m roadbook.run_pipeline input/mytrack.gpx --provider maptiler --api-key xxxx

When given a directory, one GPX failing (bad file, network hiccup, etc.)
does not stop the rest of the batch — a summary of successes/failures is
printed at the end. `--out` is ignored in directory mode (each file gets its
own output/<slug>_roadbook.pdf); use it only for single-file runs. `--merge`
is directory-mode only (a single-file run already produces one PDF) and, if
any sector succeeded, additionally writes output/<race>_ALL.pdf containing
every succeeded sector's pages back to back, in filename order. It's built
straight from each sector's intermediate vector-PDF + basemap JPGs (not by
concatenating the already-merged per-sector PDFs — PyMuPDF's page-copy
silently drops OCG registration, which would leave the combined file with
no toggleable layers), sharing one raster/vector OCG pair across the whole
merged document.

Any GPX filename containing "backup route" (case-insensitive) is always
rendered as a single page, ignoring --pages — see SINGLE_PAGE_FILENAME_MARKERS
below if you want to add more trigger phrases.

Equivalent to running, per GPX file, in order:
    python -m roadbook.parse_gpx      <gpx>
    python -m roadbook.fetch_basemap  work/<slug>_data.pkl --pages N --zoom Z
    python -m roadbook.render_pages   work/<slug>_data.pkl work/<slug>_basemaps --pages N --race-title "..."
    python -m roadbook.merge_layers   work/<slug>_vector.pdf work/<slug>_basemaps output/<slug>_roadbook.pdf
"""
import argparse
import json
import pickle
import re
import subprocess
import sys
from pathlib import Path

from roadbook import fetch_basemap, merge_layers, parse_gpx
from roadbook.config import PROJECT_ROOT, WORK_DIR

OUTPUT_DIR = PROJECT_ROOT / "output"

# GPX filenames containing any of these (case-insensitive) are treated as a
# single-page roadbook regardless of --pages — e.g. short backup/variant
# routes that don't warrant splitting across multiple A4 sheets.
SINGLE_PAGE_FILENAME_MARKERS = ["backup route"]


def effective_pages(gpx_path: Path, args, total_km=None) -> int:
    """Page count: from explicit --splits (needs the track length to drop
    out-of-range ones), else 1 for single-page filenames, else --pages."""
    splits = getattr(args, "splits", None)
    if splits and total_km is not None:
        return len(fetch_basemap.page_edges(total_km, args.pages, splits)) - 1
    name_lower = gpx_path.name.lower()
    for marker in SINGLE_PAGE_FILENAME_MARKERS:
        if marker in name_lower:
            return 1
    return args.pages


def process_one(gpx_path: Path, args, out_override: Path = None, progress=None) -> Path:
    """Run all four pipeline steps for a single GPX file. Returns the final PDF path.

    `progress`, if given, is called as progress(step, message) with step in
    1..4 at the start of each step (and with step 2 once per fetched page) —
    used by the web app to drive its progress bar."""
    progress = progress or (lambda step, message: None)
    slug = parse_gpx.slug_for(gpx_path)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"=== [1/4] Parsing {gpx_path.name} ===")
    progress(1, f"Parsing {gpx_path.name}")
    data = parse_gpx.parse_gpx(gpx_path, waypoint_radius_m=args.waypoint_radius_m)
    data_pkl = WORK_DIR / f"{slug}_data.pkl"
    with open(data_pkl, "wb") as f:
        pickle.dump(data, f)
    print(f"  {data['race_title']} — sector {data['sector_code']}: {data['start_name']} -> {data['end_name']}")
    print(f"  {data['total_dist_km']:.2f} km | D+ {data['gain']:.0f} m | D- {data['loss']:.0f} m | "
          f"{len(data['pois'])} waypoints matched")

    splits = getattr(args, "splits", None)
    n_pages = effective_pages(gpx_path, args, data["total_dist_km"])
    if splits:
        print(f"  split at {', '.join(f'{k:.2f}' for k in splits)} km -> {n_pages} pages")
    elif n_pages != args.pages:
        print(f"(filename matches a single-page marker -> generating 1 page instead of {args.pages})")

    print(f"\n=== [2/4] Fetching basemap tiles ({args.provider}, zoom {args.zoom}) ===")
    progress(2, f"Fetching basemap tiles ({args.provider}, zoom {args.zoom})")
    basemap_dir = WORK_DIR / f"{slug}_basemaps"
    style_id = args.mapbox_style or args.maptiler_map_id
    fetch_basemap.fetch_all_pages(data, basemap_dir, n_pages=n_pages, zoom=args.zoom,
                                   workers=args.tile_workers, provider=args.provider,
                                   api_key=args.api_key, style_id=style_id, splits=splits,
                                   on_page=lambda p, n: progress(2, f"Fetched basemap page {p}/{n}"))

    print(f"\n=== [3/4] Rendering {n_pages} page{'s' if n_pages != 1 else ''} ===")
    progress(3, f"Rendering {n_pages} page{'s' if n_pages != 1 else ''}")
    # Run as a subprocess (not an in-process call) so a matplotlib crash on
    # one sector doesn't take down a batch run over many GPX files.
    cmd = [sys.executable, "-m", "roadbook.render_pages", str(data_pkl), str(basemap_dir),
           "--pages", str(n_pages)]
    if args.race_title:
        cmd += ["--race-title", args.race_title]
    if not getattr(args, "part_tables", True):
        cmd += ["--no-part-tables"]
    if not getattr(args, "final_table", True):
        cmd += ["--no-final-table"]
    style = getattr(args, "style", None)
    if style:
        # a dict from the web app, or a JSON path from --style
        if isinstance(style, dict):
            style_path = WORK_DIR / f"{slug}_style.json"
            with open(style_path, "w") as f:
                json.dump(style, f, indent=2)
            style = style_path
        cmd += ["--style", str(style)]
    subprocess.run(cmd, check=True, cwd=PROJECT_ROOT)

    print("\n=== [4/4] Merging layers ===")
    progress(4, "Merging layers")
    out_pdf = out_override if out_override else OUTPUT_DIR / f"{slug}_roadbook.pdf"
    merge_layers.merge(WORK_DIR / f"{slug}_vector.pdf", basemap_dir, out_pdf)

    print(f"\nDone -> {out_pdf}")
    return out_pdf


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gpx_path", help="A single .gpx file, OR a directory to scan for *.gpx files")
    ap.add_argument("--pages", type=int, default=4)
    ap.add_argument("--zoom", type=int, default=13)
    ap.add_argument("--provider", choices=list(fetch_basemap.PROVIDERS), default=None,
                     help="Tile provider (default: maptiler if a MapTiler key is configured, "
                          "else opentopomap, which needs no key). "
                          "mapbox/maptiler need --api-key (or the matching env var) and give "
                          "genuine retina tiles.")
    ap.add_argument("--splits", type=fetch_basemap.parse_splits, default=None,
                     help="Comma-separated km where pages end, e.g. 12.4,31.0 (overrides --pages: "
                          "pages = number of splits + 1). Default: equal-distance split.")
    ap.add_argument("--no-part-tables", dest="part_tables", action="store_false",
                     help="Don't add a waypoint-table page after each map page")
    ap.add_argument("--no-final-table", dest="final_table", action="store_false",
                     help="Don't add the final waypoint table of all points")
    ap.add_argument("--api-key", default=None,
                     help="API key/access token for --provider mapbox or maptiler")
    ap.add_argument("--mapbox-style", default=None, help="Mapbox style ID (default: mapbox/outdoors-v12)")
    ap.add_argument("--maptiler-map-id", default=None, help="MapTiler map ID (default: topo-v4)")
    ap.add_argument("--waypoint-radius-m", type=float, default=60.0)
    ap.add_argument("--tile-workers", type=int, default=4)
    ap.add_argument("--race-title", default=None)
    ap.add_argument("--style", default=None,
                     help="JSON file with style overrides (label font, line widths, colour scale, "
                          "label offsets) — see DEFAULT_STYLE in render_pages.py")
    ap.add_argument("--out", default=None,
                     help="Final PDF path — single-file mode only (default: output/<slug>_roadbook.pdf)")
    ap.add_argument("--merge", action="store_true",
                     help="Directory mode only: also combine every succeeded sector's PDF into one "
                          "output/<race>_ALL.pdf, in filename order")
    args = ap.parse_args()
    args.provider = args.provider or fetch_basemap.default_provider()

    target = Path(args.gpx_path).resolve()

    if target.is_dir():
        gpx_files = sorted(target.glob("*.gpx"))
        if not gpx_files:
            print(f"No .gpx files found in {target}")
            return
        print(f"Found {len(gpx_files)} GPX file(s) in {target}:")
        for f in gpx_files:
            print(f"  - {f.name}")
        print()

        succeeded, failed = [], []
        for i, gpx_file in enumerate(gpx_files, 1):
            print(f"\n{'#' * 70}\n# [{i}/{len(gpx_files)}] {gpx_file.name}\n{'#' * 70}\n")
            try:
                out_pdf = process_one(gpx_file, args)
                succeeded.append((gpx_file, out_pdf))
            except Exception as e:
                print(f"!! FAILED on {gpx_file.name}: {e}")
                failed.append((gpx_file, str(e)))

        print(f"\n{'=' * 70}\nBatch summary: {len(succeeded)} succeeded, {len(failed)} failed\n{'=' * 70}")
        for gpx_file, out_pdf in succeeded:
            print(f"  OK   {gpx_file.name} -> {out_pdf}")
        for gpx_file, err in failed:
            print(f"  FAIL {gpx_file.name}: {err}")

        if args.merge:
            if not succeeded:
                print("\n--merge requested but no sector succeeded — nothing to merge.")
            else:
                merged_slug = re.sub(r"[^a-zA-Z0-9]+", "_", args.race_title or "roadbook").strip("_").lower()
                merged_path = OUTPUT_DIR / f"{merged_slug}_ALL.pdf"
                # succeeded is already in gpx_files order (sorted filenames),
                # which is the natural sector order for this race. Rebuild
                # straight from each sector's intermediate vector-PDF +
                # basemap JPGs (still sitting in work/ from process_one)
                # rather than concatenating the already-merged per-sector
                # PDFs — see merge_layers.merge_multi()'s docstring for why.
                sectors = [
                    (WORK_DIR / f"{parse_gpx.slug_for(gpx_file)}_vector.pdf",
                     WORK_DIR / f"{parse_gpx.slug_for(gpx_file)}_basemaps")
                    for gpx_file, _out_pdf in succeeded
                ]
                merge_layers.merge_multi(sectors, merged_path)
                print(f"\nMerged {len(succeeded)} PDF(s) -> {merged_path}")

    elif target.is_file():
        if args.merge:
            print("(--merge has no effect in single-file mode — only one PDF is produced)")
        out_override = Path(args.out) if args.out else None
        process_one(target, args, out_override=out_override)

    else:
        raise SystemExit(f"Not found: {target}")


if __name__ == "__main__":
    main()
