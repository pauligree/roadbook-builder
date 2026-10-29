"""
api.py — library entry points for front-ends (the web app in webapp/, later a
Mac/mobile shell). Wraps the four pipeline steps behind two calls:

  analyze(gpx_path, opts) -> JSON-ready dict
      Everything the schematic preview needs, computed in well under a
      second and WITHOUT fetching any tiles: downsampled track with grade,
      per-page tile-snapped map bounds (exactly the area the final render
      will cover), per-page stats and POIs, layout ratios, tile counts.

  render(gpx_path, opts, progress=None) -> Path to the final PDF
      Runs the real pipeline (run_pipeline.process_one), reporting progress.

Both share the same page-split / gradient code as the CLI, so the preview
can't drift from what render_pages.py actually draws.
"""
import math
import re
from dataclasses import asdict, dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from roadbook import fetch_basemap, layout, parse_gpx, run_pipeline
from roadbook.render_pages import (
    GRADIENT_COLORS, GRADIENT_VMAX_CLIP, HEIGHT_RATIOS, PAGE_SIZE_IN,
    build_sheets, clean_name, compute_gradient, cumulative_gain, page_subtitle, table_header,
)

PREVIEW_MAX_POINTS = 3000

# Label fonts offered in the UI — kept to families that are common on macOS
# and embed cleanly (as real, selectable text) via matplotlib's PDF backend.
# Only the ones actually installed are offered; see available_fonts().
LABEL_FONTS = [
    "DejaVu Sans", "Helvetica", "Helvetica Neue", "Arial", "Arial Narrow",
    "Avenir Next", "Avenir Next Condensed", "Futura", "Gill Sans", "Optima",
    "Verdana", "Trebuchet MS", "Roboto", "Roboto Condensed", "Open Sans",
    "Source Sans Pro", "Noto Sans", "PT Sans", "PT Sans Narrow", "Georgia",
]
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


@lru_cache(maxsize=1)
def available_fonts():
    from matplotlib import font_manager
    installed = {f.name for f in font_manager.fontManager.ttflist}
    return [f for f in LABEL_FONTS if f in installed]


def clean_style(s):
    """Validate a style dict from a client into render_pages.DEFAULT_STYLE keys."""
    s = s if isinstance(s, dict) else {}
    out = {}
    if s.get("label_font") in available_fonts() and s["label_font"] != "DejaVu Sans":
        out["label_font"] = s["label_font"]
    for key, lo, hi in (("track_width", 0.3, 6.0), ("profile_width", 0.3, 4.0)):
        if s.get(key) not in (None, ""):
            out[key] = min(hi, max(lo, float(s[key])))
    colors = s.get("grade_colors")
    if isinstance(colors, list) and len(colors) == 3 and all(isinstance(c, str) and HEX_COLOR.match(c) for c in colors):
        out["grade_colors"] = colors
    if s.get("grade_max"):
        out["grade_max"] = min(60.0, max(2.0, float(s["grade_max"])))
    if s.get("profile_smooth_m"):
        out["profile_smooth_m"] = min(1000.0, max(0.0, float(s["profile_smooth_m"])))
    if s.get("profile_cum_gain"):
        out["profile_cum_gain"] = True
    bins = s.get("grade_bins")
    if isinstance(bins, list) and 1 <= len(bins) <= 6:
        bins = sorted({round(min(60.0, max(1.0, float(b))), 1) for b in bins})
        out["grade_bins"] = bins
    offsets = s.get("label_offsets")
    if isinstance(offsets, dict):
        out["label_offsets"] = {
            str(k)[:80]: [max(-500.0, min(500.0, float(v[0]))), max(-500.0, min(500.0, float(v[1])))]
            for k, v in list(offsets.items())[:1000]
            if isinstance(v, (list, tuple)) and len(v) == 2
        }
    return out
PAD_DEG = 0.02  # same default as fetch_basemap.page_bboxes / run_pipeline


@dataclass
class RenderOptions:
    pages: int = 4
    zoom: int = 13
    provider: str | None = None  # None -> fetch_basemap.default_provider()
    api_key: str | None = None
    style_id: str | None = None
    splits: list | None = None  # km where pages end; None = equal-distance split
    hidden_pois: list | None = None  # km of waypoints left out, see parse_gpx.hide_pois
    part_tables: bool = True  # waypoint-table page after each map page
    final_table: bool = True  # final waypoint table of all points
    waypoint_radius_m: float = 60.0
    overlap_km: float = fetch_basemap.OVERLAP_KM  # neighbouring track shown faded on each map
    same_scale: bool = False  # every map page at the largest page's scale
    print_scale: int | None = None  # fixed 1:N; page count then follows from the track
    saturation: float = 1.0  # basemap colour, 0 = greyscale
    lighten: float = 0.0     # basemap blended towards white
    hillshade: float = 0.0   # relief shading strength, 0 = off
    race_title: str | None = None
    tile_workers: int = 4
    style: dict = field(default_factory=dict)  # see render_pages.DEFAULT_STYLE / clean_style()

    @classmethod
    def from_dict(cls, d):
        known = {f.name for f in fields(cls)}
        opts = cls(**{k: v for k, v in (d or {}).items() if k in known and v not in ("", None)})
        opts.pages = max(1, min(int(opts.pages), 20))
        opts.zoom = max(8, min(int(opts.zoom), 17))
        opts.waypoint_radius_m = max(1.0, float(opts.waypoint_radius_m))
        opts.overlap_km = max(0.0, min(float(opts.overlap_km), 5.0))
        opts.same_scale = bool(opts.same_scale)
        opts.print_scale = max(5000, min(int(opts.print_scale), 500000)) if opts.print_scale else None
        opts.saturation = max(0.0, min(float(opts.saturation), 1.5))
        opts.lighten = max(0.0, min(float(opts.lighten), 0.9))
        opts.hillshade = max(0.0, min(float(opts.hillshade), 1.0))
        opts.style = clean_style(opts.style)
        if opts.splits:
            opts.splits = sorted({round(float(k), 3) for k in opts.splits})[:50] or None
        if opts.hidden_pois:
            opts.hidden_pois = sorted({round(float(k), 3) for k in opts.hidden_pois})[:500] or None
        opts.provider = opts.provider or fetch_basemap.default_provider()
        if opts.provider not in fetch_basemap.PROVIDERS:
            raise ValueError(f"Unknown provider {opts.provider!r}")
        return opts

    def as_args(self):
        """Namespace shaped like run_pipeline's argparse result."""
        d = asdict(self)
        return SimpleNamespace(
            pages=d["pages"], zoom=d["zoom"], provider=d["provider"], api_key=d["api_key"],
            mapbox_style=d["style_id"] if d["provider"] == "mapbox" else None,
            maptiler_map_id=d["style_id"] if d["provider"] == "maptiler" else None,
            waypoint_radius_m=d["waypoint_radius_m"], tile_workers=d["tile_workers"],
            race_title=d["race_title"], style=d["style"], splits=d["splits"],
            part_tables=d["part_tables"], final_table=d["final_table"],
            overlap_km=d["overlap_km"], same_scale=d["same_scale"], print_scale=d["print_scale"],
            saturation=d["saturation"], lighten=d["lighten"], hillshade=d["hillshade"],
            hidden_pois=d["hidden_pois"],
        )


# Parsing an 18k-point GPX takes a few hundred ms (python haversine loop);
# the preview re-analyzes on every option change, so cache per file+radius.
_parse_cache = {}


def _parsed(gpx_path: Path, radius_m: float):
    key = (str(gpx_path), gpx_path.stat().st_mtime, radius_m)
    if key not in _parse_cache:
        if len(_parse_cache) > 16:
            _parse_cache.clear()
        _parse_cache[key] = parse_gpx.parse_gpx(gpx_path, waypoint_radius_m=radius_m)
    return _parse_cache[key]


def _page_bounds(box, zoom, tile_px):
    """Same extent as fetch_basemap.fetch_all_pages writes to bounds.json
    (panel-filling box, cropped to whole mosaic pixels)."""
    ext = fetch_basemap.page_extent(box, zoom, tile_px)
    x0, x1, y0, y1 = ext["tiles"]
    return dict(west=ext["west"], east=ext["east"], north=ext["north"], south=ext["south"],
                tiles=(x1 - x0) * (y1 - y0))


def ground_scale(box):
    """Approximate print scale denominator (1:N) of a page's map panel."""
    panel_w_m = layout.map_panel_size_in()[0] * 0.0254
    return fetch_basemap.ground_width_km(box) * 1000 / panel_w_m


def analyze(gpx_path, opts: RenderOptions):
    gpx_path = Path(gpx_path)
    data, hidden = parse_gpx.hide_pois(_parsed(gpx_path, opts.waypoint_radius_m), opts.hidden_pois)
    n_pages = run_pipeline.effective_pages(gpx_path, opts.as_args(), data["total_dist_km"])
    provider = fetch_basemap.PROVIDERS[opts.provider]

    lats, lons, eles, dist = data["lats"], data["lons"], data["eles"], data["dist"]
    dist_km = dist / 1000.0
    grad = compute_gradient(eles, dist)
    grad_vmax = float(np.clip(np.percentile(np.abs(grad), 97), *GRADIENT_VMAX_CLIP))
    grad_pt = np.append(grad, grad[-1] if len(grad) else 0.0)  # one value per point

    step = max(1, math.ceil(len(lats) / PREVIEW_MAX_POINTS))
    idx = np.unique(np.append(np.arange(0, len(lats), step), len(lats) - 1))
    # mean grade over each downsample bucket, so the preview's colours look
    # as smooth as the full-resolution track render_pages.py draws
    grade_ds = np.add.reduceat(grad_pt, idx) / np.diff(np.append(idx, len(grad_pt)))

    pois = [dict(km=km, name=clean_name(name), lat=plat, lon=plon, ele=pele)
            for (km, name, plat, plon, pele) in data["pois"]]

    boxes = fetch_basemap.page_bboxes(data, n_pages, PAD_DEG, splits=opts.splits,
                                      overlap_km=opts.overlap_km, same_scale=opts.same_scale,
                                      print_scale=opts.print_scale)
    n_pages = len(boxes)  # a fixed print scale picks its own page count
    pages = []
    for box in boxes:
        d0, d1 = box["d0"], box["d1"]
        m = (dist_km >= d0 - 0.1) & (dist_km <= d1 + 0.1)
        seg_ele = eles[m]
        d_ele = np.diff(seg_ele)
        pages.append(dict(
            page=box["page"], d0=d0, d1=d1, overlap_km=box["overlap_km"],
            scale=round(ground_scale(box)), fits=box.get("fits", True),
            bounds=_page_bounds(box, opts.zoom, provider["tile_px"]),
            subtitle=page_subtitle(d0, d1, bool(opts.splits), [(p["km"], p["name"]) for p in pois],
                                   data["total_dist_km"],
                                   data["start_name"], data["end_name"]),
            length_km=float(dist_km[m][-1] - dist_km[m][0]),
            gain=float(d_ele[d_ele > 0].sum()), loss=float(-d_ele[d_ele < 0].sum()),
            min_ele=float(seg_ele.min()), max_ele=float(seg_ele.max()),
            poi_count=sum(1 for p in pois if d0 - 0.05 <= p["km"] <= d1 + 0.05),
        ))

    # page sequence (maps, part tables, final table) — numbering and table
    # rows/headers for the preview, see render_pages.build_sheets
    edges = [p["d0"] for p in pages] + [pages[-1]["d1"]]
    poi_tuples = [(p["km"], p["name"], p["lat"], p["lon"], p["ele"]) for p in pois]
    sheets = build_sheets(data, poi_tuples, edges,
                          part_tables=opts.part_tables, final_table=opts.final_table)
    for sh in sheets:
        if sh["kind"] != "map":
            sh["subtitle"], sh["stats"] = table_header(sh, data, edges, bool(opts.splits), poi_tuples)

    return dict(
        file=gpx_path.name,
        slug=parse_gpx.slug_for(gpx_path),
        meta=dict(
            race_title=opts.race_title or data["race_title"],
            sector_code=data["sector_code"], version=data["version"],
            start_name=data["start_name"], end_name=data["end_name"],
            update_tag=data["update_tag"],
        ),
        stats=dict(
            total_km=float(data["total_dist_km"]), gain=data["gain"], loss=data["loss"],
            min_ele=data["min_ele"], max_ele=data["max_ele"], points=int(len(lats)),
        ),
        n_pages=n_pages,
        pages_forced_single=not opts.splits and not opts.print_scale and n_pages != opts.pages,
        print_scale=opts.print_scale,
        splits=[p["d1"] for p in pages[:-1]] if opts.splits else None,
        track=dict(
            lat=np.round(lats[idx], 6).tolist(), lon=np.round(lons[idx], 6).tolist(),
            ele=np.round(eles[idx], 1).tolist(), km=np.round(dist_km[idx], 4).tolist(),
            grade=np.round(grade_ds, 2).tolist(),
            cum_up=np.round(cumulative_gain(eles)[idx], 1).tolist(),  # full-resolution D+ sums
        ),
        pois=pois,
        hidden_pois=[dict(km=km, name=clean_name(name)) for (km, name, _lat, _lon, _ele) in hidden],
        pages=pages,
        sheets=sheets,
        tiles_total=sum(p["bounds"]["tiles"] for p in pages),
        tile_px=provider["tile_px"],
        attribution=provider["attribution"],
        grade_vmax=grad_vmax,
        grade_colors=GRADIENT_COLORS,
        layout=dict(height_ratios=HEIGHT_RATIOS, page_size_in=PAGE_SIZE_IN),
    )


def render(gpx_path, opts: RenderOptions, progress=None) -> Path:
    return run_pipeline.process_one(Path(gpx_path), opts.as_args(), progress=progress)


def merge(gpx_paths, race_title=None) -> Path:
    """One output/<race>_ALL.pdf from sectors already rendered with render(),
    in the given order."""
    return run_pipeline.merge_sectors([Path(p) for p in gpx_paths], race_title)
