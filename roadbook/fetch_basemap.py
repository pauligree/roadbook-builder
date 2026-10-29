"""
fetch_basemap.py — Step 2 of the roadbook pipeline.

Downloads basemap tiles directly (this machine has normal internet access,
so — unlike the original Claude-sandbox prototype — there is no need to
route tile fetching through a browser and no artificial ~1500px screenshot
cap). Stitches them into one mosaic per page and records the mosaic's exact
geographic bounds for later alignment with the track.

Three tile providers are supported (--provider):

  opentopomap            Free, no API key. Standard 256px tiles, no genuine
                          retina/@2x variant — see README.md §3 for why.
  mapbox                 Needs a Mapbox access token (--api-key, or set the
                          MAPBOX_ACCESS_TOKEN environment variable). Uses the
                          Static Tiles API (docs.mapbox.com/api/maps/static-tiles/)
                          with tilesize=256 + @2x, which returns genuine
                          512x512px retina tiles — sharper/crisper rendering
                          of the same labels (not an interpolated upscale,
                          and NOT bigger text — see the --zoom help below for
                          what actually controls apparent label size) — at
                          the SAME z/x/y grid as opentopomap's 256px tiles
                          (no changes needed to the tile-math below). Default style is
                          mapbox/outdoors-v12 (--mapbox-style to override).
                          NOTE: requesting tilesize=256 costs 4x the API
                          requests of Mapbox's default 512px tiles for the
                          same area — see README.md for the tradeoff and
                          Mapbox's print-attribution requirements, which are
                          stricter than OpenTopoMap's.
  maptiler                Needs a MapTiler API key (--api-key, or set the
                          MAPTILER_API_KEY environment variable). Uses the
                          Raster XYZ Tiles API (docs.maptiler.com/cloud/api/maps/)
                          with @2x, also genuine 512x512px retina tiles on
                          the same z/x/y grid. Default map is "topo-v4"
                          (--maptiler-map-id to override) — MapTiler renames/
                          reversions its style IDs occasionally, so if you
                          get a 404 "Style not found" error, check your
                          MapTiler account's map catalog (cloud.maptiler.com/maps/)
                          for the current outdoor map's ID and pass it via
                          --maptiler-map-id.

Two independent things affect what you see on the printed page — don't
confuse them:
  --zoom     Since each page always shows a FIXED real-world area (set by
             how the track is split, not by zoom) squeezed into a FIXED
             print panel, the map SCALE never changes with zoom. What
             zoom actually picks is which pre-designed map style renders:
             low zoom = the decluttered "regional overview" style (fewer
             labels, each with more visual weight); high zoom = the
             "close-up navigation" style (more real detail — trails, spot
             elevations, minor contours — but many small, densely packed
             labels). Forcing a high-zoom style to cover a wide area on
             one page shrinks everything to fit. Lower --zoom for bigger-
             looking native labels; raise it for more real terrain detail.
  retina     (mapbox/maptiler only) Renders the SAME labels the chosen
             zoom's style would show anyway, just with more source pixels
             — sharper/crisper, not bigger (roughly doubles printed ppi;
             see --help for --zoom for approximate numbers).

Tile usage policy: OpenTopoMap (like the underlying OpenStreetMap tile
infrastructure) asks bulk consumers to identify themselves with a descriptive
User-Agent and to keep request rates modest. This script sets a User-Agent
and caps concurrency accordingly — please don't crank --workers way up for
large batch jobs.

Usage:
    python -m roadbook.fetch_basemap work/tor450_t1_..._data.pkl
    python -m roadbook.fetch_basemap work/..._data.pkl --pages 4 --zoom 15
    python -m roadbook.fetch_basemap work/..._data.pkl --provider mapbox --api-key pk.xxxx
    MAPBOX_ACCESS_TOKEN=pk.xxxx python -m roadbook.fetch_basemap work/..._data.pkl --provider mapbox
    MAPTILER_API_KEY=xxxx python -m roadbook.fetch_basemap work/..._data.pkl --provider maptiler

Default provider: maptiler when a MapTiler key is available (env var or
config.local.json), otherwise opentopomap — see default_provider().

Local defaults: config.local.json in the project root (gitignored) can hold a
per-provider key and default style, used when neither --api-key nor the env
var is set:
    {"maptiler": {"api_key": "xxxx", "style_id": "<map id>"}}
(loaded by roadbook/config.py, which also reads its optional "work_dir").
"""
import argparse
import io
import json
import math
import os
import pickle
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests
from PIL import Image

from roadbook import layout
from roadbook.config import LOCAL_CONFIG, WORK_DIR


USER_AGENT = "roadbook-builder/1.0 (personal trail-race roadbook generator)"

# Downloaded tiles are kept in WORK_DIR/tiles/<provider>/<style>/z/x/y.tile
# (raw bytes as served; the API key is never part of the path) and reused by
# later renders — tweaking options and re-rendering the same race then costs
# no tile requests. Tiles older than TILE_CACHE_MAX_AGE_DAYS are fetched
# again: maps do change, and Mapbox/MapTiler terms allow caching only for a
# limited time. Safe to delete at any time. --no-tile-cache bypasses it.
TILE_CACHE_DIR = WORK_DIR / "tiles"
TILE_CACHE_MAX_AGE_DAYS = 30

# Each provider's tiles url is built by build_tile_url() below. "subdomains"
# is only meaningful for the {s} placeholder (request sharding across
# a/b/c hostnames); providers without it just hit a single host.
PROVIDERS = {
    "opentopomap": dict(
        subdomains=["a", "b", "c"],
        tile_px=256,
        needs_key=False,
        attribution="Map data (c) OpenStreetMap contributors, SRTM | Map style (c) OpenTopoMap (CC-BY-SA)",
    ),
    "mapbox": dict(
        subdomains=None,
        tile_px=512,   # 256 logical tile + @2x = 512px actual, see module docstring
        needs_key=True,
        key_env_var="MAPBOX_ACCESS_TOKEN",
        default_style="mapbox/outdoors-v12",
        attribution="(c) Mapbox, (c) OpenStreetMap",
    ),
    "maptiler": dict(
        subdomains=None,
        tile_px=512,   # 256 logical tile + @2x = 512px actual, see module docstring
        needs_key=True,
        key_env_var="MAPTILER_API_KEY",
        default_style="topo-v4",  # verify against your account's catalog; see module docstring
        attribution="(c) MapTiler (c) OpenStreetMap contributors",
    ),
}

for _name, _local in LOCAL_CONFIG.items():
    if _name in PROVIDERS and _local.get("style_id"):
        PROVIDERS[_name]["default_style"] = _local["style_id"]


def deg2tile(lat, lon, zoom):
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    xt = (lon + 180.0) / 360.0 * n
    yt = (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return xt, yt


def tile2lon(x, z):
    return x / (2 ** z) * 360.0 - 180.0


def tile2lat(y, z):
    n = 2 ** z
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))


def default_provider():
    """maptiler if a key is configured, else the keyless opentopomap, so a
    fresh checkout without credentials still works out of the box."""
    try:
        resolve_api_key("maptiler", None)
        return "maptiler"
    except BasemapFetchError:
        return "opentopomap"


def resolve_api_key(provider, api_key_arg):
    """Fail fast (before fetching any tiles) if a provider needs a key and
    none was given, rather than surfacing it as a wall of failed-tile
    retries and a confusing BasemapFetchError."""
    cfg = PROVIDERS[provider]
    if not cfg["needs_key"]:
        return None
    key = (api_key_arg or os.environ.get(cfg["key_env_var"])
           or LOCAL_CONFIG.get(provider, {}).get("api_key"))
    if not key:
        raise BasemapFetchError(
            f"--provider {provider} requires an API key. Pass --api-key, set the "
            f"{cfg['key_env_var']} environment variable, or add it to config.local.json."
        )
    return key


def build_tile_url(provider, z, x, y, style=None, api_key=None):
    if provider == "opentopomap":
        cfg = PROVIDERS[provider]
        sub = cfg["subdomains"][(x + y) % len(cfg["subdomains"])]
        return f"https://{sub}.tile.opentopomap.org/{z}/{x}/{y}.png"
    elif provider == "mapbox":
        # tilesize=256 (explicit, not Mapbox's default of 512) keeps this on
        # the SAME z/x/y grid opentopomap and our deg2tile/tile2lon/tile2lat
        # math use; @2x renders that same tile at genuine 512x512 retina
        # density instead of upscaling it. See module docstring for the
        # request-volume tradeoff of this choice.
        return (f"https://api.mapbox.com/styles/v1/{style}/tiles/256/{z}/{x}/{y}@2x"
                f"?access_token={api_key}")
    elif provider == "maptiler":
        # MapTiler's documented URL template is
        # {mapId}/{tileSize}/{z}/{x}/{y}{scale}.{format} — tileSize is a
        # required path segment, not optional (the docs' own shorthand
        # example just omits it, which is misleading). Omitting it caused
        # exactly the "shuffled tiles" bug: each tile came back as valid,
        # correctly-styled content for roughly the right area, but at a
        # zoom/grid registration MapTiler's server assumed by default that
        # didn't match our z/x/y math, so neighboring tiles didn't actually
        # share a border. Pinning tilesize=256 explicitly puts @2x on the
        # SAME z/x/y grid opentopomap/deg2tile use, giving genuine 512x512
        # retina tiles that tile together correctly.
        return f"https://api.maptiler.com/maps/{style}/256/{z}/{x}/{y}@2x.png?key={api_key}"
    else:
        raise ValueError(f"Unknown provider: {provider}")


def tile_cache_path(provider, z, x, y, style=None):
    # opentopomap has a single style; for the others the style/map ID picks
    # the look, so it is part of the key ("mapbox/outdoors-v12" -> one dir)
    style_dir = re.sub(r"[^\w.-]+", "_", style) if style and PROVIDERS[provider]["needs_key"] else "default"
    return TILE_CACHE_DIR / provider / style_dir / str(z) / str(x) / f"{y}.tile"


def read_cached_tile(path):
    """The cached tile as an RGB image, or None if missing, expired or
    unreadable (a half-written or corrupt file is simply fetched again)."""
    try:
        if time.time() - path.stat().st_mtime > TILE_CACHE_MAX_AGE_DAYS * 86400:
            return None
        return Image.open(path).convert("RGB")
    except (OSError, ValueError):
        return None


def fetch_tile(session, provider, z, x, y, style=None, api_key=None, retries=3, save_to=None):
    """Download one tile. With `save_to`, the response bytes are also
    written there (atomically: tiles are fetched from several threads)."""
    url = build_tile_url(provider, z, x, y, style=style, api_key=api_key)
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=10)
            if resp.status_code == 200:
                img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                if save_to:
                    save_to.parent.mkdir(parents=True, exist_ok=True)
                    tmp = save_to.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
                    tmp.write_bytes(resp.content)
                    tmp.replace(save_to)
                return img
            if resp.status_code in (400, 401, 403, 404):
                # Bad/missing key, bad style or map ID, malformed request —
                # not worth retrying 3x, and worth surfacing clearly rather
                # than letting every tile fail silently and hitting
                # max_failed_fraction with a generic "check your internet"
                # message. A 404 here usually means the style/map ID is
                # wrong (see --mapbox-style / --maptiler-map-id).
                raise BasemapFetchError(
                    f"Tile request to {provider} failed with HTTP {resp.status_code} "
                    f"({resp.text[:200]!r}) — check your --api-key / access token and "
                    f"style/map ID."
                )
        except requests.RequestException:
            pass
        time.sleep(0.3 * (attempt + 1))
    return None  # leave blank if a tile truly can't be fetched


def build_mosaic(session, provider, zoom, x0, x1, y0, y1, style=None, api_key=None,
                  tile_px=256, workers=4, tile_cache=True):
    """Returns (mosaic_image, n_tiles_total, n_tiles_failed, n_tiles_cached).
    A failed tile is left blank/white in the mosaic — the caller decides how
    to react to a high failure count (see fetch_all_pages)."""
    cols, rows = x1 - x0, y1 - y0
    mosaic = Image.new("RGB", (cols * tile_px, rows * tile_px), "white")
    tasks = [(x, y) for x in range(x0, x1) for y in range(y0, y1)]

    def worker(xy):
        x, y = xy
        path = tile_cache_path(provider, zoom, x, y, style) if tile_cache else None
        img = read_cached_tile(path) if path else None
        if img is not None:
            return (x, y, img, True)
        img = fetch_tile(session, provider, zoom, x, y, style=style, api_key=api_key, save_to=path)
        return (x, y, img, False)

    n_failed = n_cached = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for x, y, img, cached in pool.map(worker, tasks):
            if img is not None:
                mosaic.paste(img, ((x - x0) * tile_px, (y - y0) * tile_px))
                n_cached += cached
            else:
                n_failed += 1
    return mosaic, len(tasks), n_failed, n_cached


def parse_splits(text):
    """argparse type for --splits: '12.4,31' -> [12.4, 31.0]."""
    return [float(v) for v in str(text).split(",") if v.strip()]


def page_edges(total_km, n_pages, splits=None):
    """Page boundaries in km. `splits` (km values where a page should end,
    e.g. chosen waypoints) overrides the default equal-distance split;
    values too close to the start/end or to each other are dropped."""
    if splits:
        cuts = []
        for km in sorted(float(k) for k in splits):
            if 0.05 < km < total_km - 0.05 and (not cuts or km - cuts[-1] > 0.05):
                cuts.append(km)
        return np.array([0.0, *cuts, total_km])
    return np.linspace(0, total_km, n_pages + 1)


def fill_panel(box, aspect=None):
    """Grow a lat/lon box (symmetrically, about its centre) so its true
    ground proportions match the map panel's — the map then fills the whole
    panel instead of leaving blank bands beside or above/below it. Uses the
    same equirectangular cos(lat0) scaling render_pages applies via
    set_aspect()."""
    aspect = aspect or layout.map_panel_aspect()
    w, e, s, n = box["west"], box["east"], box["south"], box["north"]
    k = math.cos(math.radians((s + n) / 2))
    width, height = (e - w) * k, n - s
    if width / height < aspect:
        half = (aspect * height / k) / 2
        mid = (w + e) / 2
        w, e = mid - half, mid + half
    else:
        half = (width / aspect) / 2
        mid = (s + n) / 2
        s, n = mid - half, mid + half
    return dict(box, west=w, east=e, south=s, north=n)


def page_extent(box, zoom, tile_px):
    """Tile range to fetch for a page box, plus the pixel crop of the
    stitched mosaic that matches the box exactly and the lat/lon bounds of
    that (pixel-rounded) crop. Shared with the web app's preview."""
    xf0, yf0 = deg2tile(box["north"], box["west"], zoom)
    xf1, yf1 = deg2tile(box["south"], box["east"], zoom)
    x0, x1 = math.floor(xf0), math.ceil(xf1)
    y0, y1 = math.floor(yf0), math.ceil(yf1)
    crop = (round((xf0 - x0) * tile_px), round((yf0 - y0) * tile_px),
            round((xf1 - x0) * tile_px), round((yf1 - y0) * tile_px))
    return dict(
        tiles=(x0, x1, y0, y1), crop=crop,
        west=tile2lon(x0 + crop[0] / tile_px, zoom), east=tile2lon(x0 + crop[2] / tile_px, zoom),
        north=tile2lat(y0 + crop[1] / tile_px, zoom), south=tile2lat(y0 + crop[3] / tile_px, zoom),
    )


# Each map also shows this much of the neighbouring parts' track (drawn
# faded by render_pages), so the hand-over between pages is easy to follow.
OVERLAP_KM = 0.5


# Fixed print scale: margin kept free around the track on each side (room
# for start/end labels), as a fraction of the map panel (stands in for
# pad_deg, which has no fixed size).
SCALE_MARGIN = 0.08


def panel_ground_km(print_scale):
    """(width, height) in km covered by the map panel at 1:print_scale."""
    w_in, h_in = layout.map_panel_size_in()
    return w_in * 0.0254 * print_scale / 1000, h_in * 0.0254 * print_scale / 1000


def _segment_mask(dist_km, d0, d1, overlap_km):
    return (dist_km >= d0 - 0.1 - overlap_km) & (dist_km <= d1 + 0.1 + overlap_km)


def _fits(lats, lons, mask, w_km, h_km):
    la, lo = lats[mask], lons[mask]
    k = math.cos(math.radians((la.min() + la.max()) / 2))
    return ((lo.max() - lo.min()) * k * 111.32 <= w_km and
            (la.max() - la.min()) * 111.32 <= h_km)


def scale_edges(data, print_scale, overlap_km=OVERLAP_KM):
    """Page edges (km) for a fixed print scale: as few pages as possible
    with every page's track (+ overlap) inside its map at 1:print_scale.
    An equal-distance split with that many pages is used when it fits
    (evenly sized parts read better); otherwise each page is filled
    greedily as far as it goes."""
    lats, lons, dist_km = data["lats"], data["lons"], data["dist"] / 1000.0
    total = float(data["total_dist_km"])
    w_km, h_km = (v * (1 - 2 * SCALE_MARGIN) for v in panel_ground_km(print_scale))
    fits = lambda d0, d1: _fits(lats, lons, _segment_mask(dist_km, d0, d1, overlap_km), w_km, h_km)

    edges, d0 = [0.0], 0.0
    while d0 < total:
        if fits(d0, total):
            edges.append(total)
            break
        # largest track point index whose km still fits — the box only
        # grows with d1, so binary search over the points after d0
        lo, hi = int(np.searchsorted(dist_km, d0, side="right")), len(dist_km) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            lo, hi = (mid, hi) if fits(d0, dist_km[mid]) else (lo, mid - 1)
        d1 = max(float(dist_km[lo]), d0 + 0.2)  # always make progress
        edges.append(min(d1, total))
        d0 = edges[-1]
    n = len(edges) - 1
    even = np.linspace(0, total, n + 1)
    if all(fits(even[i], even[i + 1]) for i in range(n)):
        return even
    return np.array(edges)


def page_bboxes(data, n_pages, pad_deg=0.02, splits=None, fill=True,
                overlap_km=OVERLAP_KM, same_scale=False, print_scale=None):
    """One lat/lon box per page: the page's track segment, extended by
    `overlap_km` into the neighbouring parts, plus `pad_deg`, grown to fill
    the map panel when `fill`. With `splits`, the page count is
    len(edges) - 1, not n_pages.

    `same_scale` grows every box (about its centre) to the ground size of
    the largest one, so all map pages print at one scale.

    `print_scale` (N of 1:N) gives every box exactly that ground size,
    centred on its track; without `splits` the page count then follows
    from the scale (see scale_edges) and n_pages is ignored. A box whose
    track doesn't fit (custom splits too far apart) gets fits=False."""
    lats, lons, dist = data["lats"], data["lons"], data["dist"]
    dist_km = dist / 1000.0
    if print_scale and not splits:
        edges = scale_edges(data, print_scale, overlap_km)
    else:
        edges = page_edges(data["total_dist_km"], n_pages, splits)
    boxes = []
    for i in range(len(edges) - 1):
        d0, d1 = edges[i], edges[i + 1]
        mask = _segment_mask(dist_km, d0, d1, overlap_km)
        la, lo = lats[mask], lons[mask]
        box = dict(
            page=i + 1, d0=float(d0), d1=float(d1), overlap_km=float(overlap_km),
            south=float(la.min() - pad_deg), north=float(la.max() + pad_deg),
            west=float(lo.min() - pad_deg), east=float(lo.max() + pad_deg),
        )
        if print_scale:
            w_km, h_km = panel_ground_km(print_scale)
            lat_c, lon_c = (la.min() + la.max()) / 2, (lo.min() + lo.max()) / 2
            half_lon = w_km / (111.32 * math.cos(math.radians(lat_c))) / 2
            half_lat = h_km / 111.32 / 2
            box.update(west=float(lon_c - half_lon), east=float(lon_c + half_lon),
                       south=float(lat_c - half_lat), north=float(lat_c + half_lat),
                       fits=bool(_fits(lats, lons, mask, w_km * (1 - 2 * SCALE_MARGIN),
                                       h_km * (1 - 2 * SCALE_MARGIN))))
            boxes.append(box)
        else:
            boxes.append(fill_panel(box) if fill else box)
    if same_scale and fill and not print_scale:
        boxes = same_scale_boxes(boxes)
    return boxes


def ground_width_km(box):
    k = math.cos(math.radians((box["south"] + box["north"]) / 2))
    return (box["east"] - box["west"]) * k * 111.32


def same_scale_boxes(boxes, aspect=None):
    """Panel-filling boxes regrown about their centres to the widest box's
    ground width (heights follow from the panel aspect). Same equirectangular
    scaling as fill_panel, so the result still fills the panel."""
    aspect = aspect or layout.map_panel_aspect()
    width_km = max(ground_width_km(b) for b in boxes)
    out = []
    for b in boxes:
        lat_c, lon_c = (b["south"] + b["north"]) / 2, (b["west"] + b["east"]) / 2
        half_lon = width_km / (111.32 * math.cos(math.radians(lat_c))) / 2
        half_lat = width_km / aspect / 111.32 / 2
        out.append(dict(b, west=lon_c - half_lon, east=lon_c + half_lon,
                        south=lat_c - half_lat, north=lat_c + half_lat))
    return out


class BasemapFetchError(RuntimeError):
    """Raised when tile downloads failed badly enough that the resulting
    basemap would be blank or mostly blank — e.g. no internet connection,
    the tile server being unreachable, or (for keyed providers) a missing/
    invalid API key. Deliberately NOT swallowed: a silently blank basemap
    in the final PDF is worse than a loud failure."""


def fetch_all_pages(data, out_dir, n_pages=4, zoom=13, pad_deg=0.02, workers=4,
                     verbose=True, max_failed_fraction=0.5,
                     provider=None, api_key=None, style_id=None, on_page=None, splits=None,
                     tile_cache=True, overlap_km=OVERLAP_KM, same_scale=False, print_scale=None):
    """Fetch+stitch the basemap mosaic for every page and write
    pageN_raw.jpg + bounds.json + provider.json into out_dir. Returns the
    bounds dict. This is the single implementation used by both the CLI
    below and run_pipeline.py — don't duplicate this loop elsewhere.

    `style_id` is the provider's style/map identifier (Mapbox style ID,
    MapTiler map ID, ...); each provider has its own default (see PROVIDERS)
    used when style_id is None. Ignored for opentopomap.

    `splits` (km values) ends pages at those distances instead of splitting
    into n_pages equal parts; the page edges used are written into
    bounds.json (d0/d1) so render_pages draws exactly the same split.

    `on_page`, if given, is called as on_page(page, n_pages) after each
    page's mosaic is saved (progress reporting for the web app).

    `overlap_km` / `same_scale` / `print_scale`: see page_bboxes; the
    overlap and print scale are written into bounds.json so render_pages
    draws the same stretch faded and labels the scale.

    `tile_cache` reads/writes the persistent tile cache (TILE_CACHE_DIR);
    False always downloads and stores nothing.

    Raises BasemapFetchError if more than `max_failed_fraction` of tiles on
    any page failed to download (most commonly: no internet access, or a
    missing/invalid API key or bad style/map ID for a keyed provider) rather
    than silently writing a blank white basemap image.
    """
    provider = provider or default_provider()
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r} — choose from {list(PROVIDERS)}")
    cfg = PROVIDERS[provider]
    api_key = resolve_api_key(provider, api_key)
    style = style_id or cfg.get("default_style")
    tile_px = cfg["tile_px"]

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    boxes = page_bboxes(data, n_pages, pad_deg, splits=splits,
                        overlap_km=overlap_km, same_scale=same_scale, print_scale=print_scale)
    bounds_out = {}

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    for box in boxes:
        p = box["page"]
        ext = page_extent(box, zoom, tile_px)
        x0, x1, y0, y1 = ext["tiles"]
        n_tiles = (x1 - x0) * (y1 - y0)
        if verbose:
            print(f"Page {p}: {provider} zoom {zoom}, {n_tiles} tiles "
                  f"({box['d0']:.1f}-{box['d1']:.1f} km) ...", end=" ", flush=True)

        mosaic, n_total, n_failed, n_cached = build_mosaic(session, provider, zoom, x0, x1, y0, y1,
                                                            style=style, api_key=api_key,
                                                            tile_px=tile_px, workers=workers,
                                                            tile_cache=tile_cache)

        if n_total > 0 and n_failed / n_total > max_failed_fraction:
            raise BasemapFetchError(
                f"Page {p}: {n_failed}/{n_total} tiles failed to download from "
                f"{provider} — check your internet connection"
                f"{' and API key' if cfg['needs_key'] else ''}. "
                f"(Refusing to write a mostly-blank basemap image.)"
            )

        mosaic = mosaic.crop(ext["crop"])  # exactly the panel-filling box
        img_path = out_dir / f"page{p}_raw.jpg"
        mosaic.save(img_path, quality=92)

        bounds_out[p] = dict(west=ext["west"], east=ext["east"], north=ext["north"],
                             south=ext["south"], d0=box["d0"], d1=box["d1"],
                             split="custom" if splits else "scale" if print_scale else "equal",
                             overlap_km=overlap_km, print_scale=print_scale)
        if verbose and not box.get("fits", True):
            print(f"\n  !! page {p}: track doesn't fit the map at 1:{print_scale:,} — "
                  f"add a page split or use a smaller scale", end=" ")
        if verbose:
            warn = f" ({n_failed} tile(s) failed)" if n_failed else ""
            hits = f", {n_cached} from cache" if n_cached else ""
            print(f"-> {img_path.name} ({mosaic.width}x{mosaic.height}px{hits}){warn}")
        if on_page:
            on_page(p, len(boxes))

    with open(out_dir / "bounds.json", "w") as f:
        json.dump(bounds_out, f, indent=2)

    with open(out_dir / "provider.json", "w") as f:
        json.dump(dict(provider=provider, style=style, attribution=cfg["attribution"]), f, indent=2)

    return bounds_out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_pkl", help="Output of parse_gpx.py (work/<slug>_data.pkl)")
    ap.add_argument("--pages", type=int, default=4, help="Number of pages to split the track into (default: 4)")
    ap.add_argument("--zoom", type=int, default=13,
                     help="Tile zoom level (default: 13 — ~90ppi at print scale on a non-retina "
                          "provider, ~180ppi with a retina provider's @2x tiles). Lower zoom uses the "
                          "map style's decluttered overview rendering (fewer, visually bigger native "
                          "labels); higher zoom pulls in the style meant for close-up navigation "
                          "(more real detail — trails, spot elevations — but smaller, denser labels "
                          "when a wide area is squeezed onto one page). Raise it if you want more "
                          "real terrain detail, not for bigger text. ~4x more tiles per extra level.")
    ap.add_argument("--provider", choices=list(PROVIDERS), default=None,
                     help="Tile provider (default: maptiler if a MapTiler key is configured, "
                          "else opentopomap, which needs no key). "
                          "mapbox/maptiler need --api-key (or the matching env var) and give "
                          "genuine retina tiles — see module docstring.")
    ap.add_argument("--api-key", default=None,
                     help="API key/access token for --provider mapbox or maptiler "
                          "(or set MAPBOX_ACCESS_TOKEN / MAPTILER_API_KEY instead)")
    ap.add_argument("--mapbox-style", default=None,
                     help="Mapbox style ID, e.g. mapbox/outdoors-v12 (default) or your own username/style-id")
    ap.add_argument("--maptiler-map-id", default=None,
                     help="MapTiler map ID (default: topo-v4 — see module docstring if this 404s)")
    ap.add_argument("--splits", type=parse_splits, default=None,
                     help="Comma-separated km where pages end, e.g. 12.4,31.0 (overrides --pages: "
                          "pages = number of splits + 1). Default: equal-distance split.")
    ap.add_argument("--overlap-km", type=float, default=OVERLAP_KM,
                     help=f"Track shown (faded) beyond each page's ends into the neighbouring "
                          f"parts, in km (default: {OVERLAP_KM}; 0 = none)")
    ap.add_argument("--same-scale", action="store_true",
                     help="Print every map page at the same scale (the largest page's)")
    ap.add_argument("--print-scale", type=int, default=None, metavar="N",
                     help="Fixed map scale 1:N, e.g. 25000 or 50000; the page count then follows "
                          "from the track (overrides --pages, not --splits)")
    ap.add_argument("--pad-deg", type=float, default=0.02, help="Lat/lon padding around each page's track (default: 0.02)")
    ap.add_argument("--workers", type=int, default=4, help="Concurrent tile downloads (keep modest, default: 4)")
    ap.add_argument("--out-dir", default=None, help="Output folder (default: work/<slug>_basemaps)")
    ap.add_argument("--no-tile-cache", dest="tile_cache", action="store_false",
                     help=f"Download every tile again and cache nothing (default: reuse tiles "
                          f"under work/tiles/ younger than {TILE_CACHE_MAX_AGE_DAYS} days)")
    args = ap.parse_args()
    args.provider = args.provider or default_provider()

    data_pkl = Path(args.data_pkl)
    with open(data_pkl, "rb") as f:
        data = pickle.load(f)

    slug = data_pkl.stem.replace("_data", "")
    out_dir = Path(args.out_dir) if args.out_dir else WORK_DIR / f"{slug}_basemaps"

    style_id = args.mapbox_style or args.maptiler_map_id

    fetch_all_pages(data, out_dir, n_pages=args.pages, zoom=args.zoom,
                     pad_deg=args.pad_deg, workers=args.workers,
                     provider=args.provider, api_key=args.api_key, style_id=style_id,
                     splits=args.splits, tile_cache=args.tile_cache,
                     overlap_km=args.overlap_km, same_scale=args.same_scale,
                     print_scale=args.print_scale)

    attribution = PROVIDERS[args.provider]["attribution"]
    print(f"\nSaved basemap mosaics + bounds -> {out_dir}")
    print(f"Attribution (keep this on the rendered pages): {attribution}")
    if args.provider in ("mapbox", "maptiler"):
        print(f"Note: {args.provider}'s print/distribution attribution requirements may be "
              f"stricter than OpenTopoMap's — check their terms before printing/distributing "
              f"roadbooks built with it.")


if __name__ == "__main__":
    main()
