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
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from roadbook import fetch_basemap, parse_gpx, run_pipeline
from roadbook.render_pages import (
    GRADIENT_COLORS, GRADIENT_VMAX_CLIP, HEIGHT_RATIOS, PAGE_SIZE_IN,
    clean_name, compute_gradient,
)

PREVIEW_MAX_POINTS = 3000
PAD_DEG = 0.02  # same default as fetch_basemap.page_bboxes / run_pipeline


@dataclass
class RenderOptions:
    pages: int = 4
    zoom: int = 13
    provider: str = "opentopomap"
    api_key: str | None = None
    style_id: str | None = None
    waypoint_radius_m: float = 60.0
    race_title: str | None = None
    tile_workers: int = 4

    @classmethod
    def from_dict(cls, d):
        known = {f.name for f in fields(cls)}
        opts = cls(**{k: v for k, v in (d or {}).items() if k in known and v not in ("", None)})
        opts.pages = max(1, min(int(opts.pages), 20))
        opts.zoom = max(8, min(int(opts.zoom), 17))
        opts.waypoint_radius_m = max(1.0, float(opts.waypoint_radius_m))
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
            race_title=d["race_title"],
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


def _snapped_bounds(box, zoom):
    """Same tile snapping as fetch_basemap.fetch_all_pages — the real map
    extent of a page is the tile mosaic, not the padded track bbox."""
    x1f, y1f = fetch_basemap.deg2tile(box["north"], box["west"], zoom)
    x2f, y2f = fetch_basemap.deg2tile(box["south"], box["east"], zoom)
    x0, x1 = math.floor(x1f), math.ceil(x2f)
    y0, y1 = math.floor(y1f), math.ceil(y2f)
    return dict(
        west=fetch_basemap.tile2lon(x0, zoom), east=fetch_basemap.tile2lon(x1, zoom),
        north=fetch_basemap.tile2lat(y0, zoom), south=fetch_basemap.tile2lat(y1, zoom),
        tiles=(x1 - x0) * (y1 - y0),
    )


def analyze(gpx_path, opts: RenderOptions):
    gpx_path = Path(gpx_path)
    data = _parsed(gpx_path, opts.waypoint_radius_m)
    n_pages = run_pipeline.effective_pages(gpx_path, opts.as_args())

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

    pages = []
    for box in fetch_basemap.page_bboxes(data, n_pages, PAD_DEG):
        d0, d1 = box["d0"], box["d1"]
        m = (dist_km >= d0 - 0.1) & (dist_km <= d1 + 0.1)
        seg_ele = eles[m]
        d_ele = np.diff(seg_ele)
        pages.append(dict(
            page=box["page"], d0=d0, d1=d1,
            bounds=_snapped_bounds(box, opts.zoom),
            length_km=float(dist_km[m][-1] - dist_km[m][0]),
            gain=float(d_ele[d_ele > 0].sum()), loss=float(-d_ele[d_ele < 0].sum()),
            min_ele=float(seg_ele.min()), max_ele=float(seg_ele.max()),
            poi_count=sum(1 for p in pois if d0 - 0.05 <= p["km"] <= d1 + 0.05),
        ))

    provider = fetch_basemap.PROVIDERS[opts.provider]
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
        pages_forced_single=n_pages != opts.pages,
        track=dict(
            lat=np.round(lats[idx], 6).tolist(), lon=np.round(lons[idx], 6).tolist(),
            ele=np.round(eles[idx], 1).tolist(), km=np.round(dist_km[idx], 4).tolist(),
            grade=np.round(grade_ds, 2).tolist(),
        ),
        pois=pois,
        pages=pages,
        tiles_total=sum(p["bounds"]["tiles"] for p in pages),
        tile_px=provider["tile_px"],
        attribution=provider["attribution"],
        grade_vmax=grad_vmax,
        grade_colors=GRADIENT_COLORS,
        layout=dict(height_ratios=HEIGHT_RATIOS, page_size_in=PAGE_SIZE_IN),
    )


def render(gpx_path, opts: RenderOptions, progress=None) -> Path:
    return run_pipeline.process_one(Path(gpx_path), opts.as_args(), progress=progress)
