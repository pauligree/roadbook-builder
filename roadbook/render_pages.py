"""
render_pages.py — Step 3 of the roadbook pipeline.

Renders the A4 roadbook pages TWICE per page with an identical layout:
  - "raster" pass: a low-res preview of the basemap image only (see note
    below — this is NOT what ends up in the final PDF's resolution).
  - "vector" pass: everything else (grid, gradient-colored track + dark
    casing, start/end markers, waypoint labels, scale bar, north arrow,
    colorbar, header, footer, elevation profile) on a transparent background.

The two passes are merged into one layered PDF by merge_layers.py. Keeping
them as two separate matplotlib renders (rather than one figure split into
"raster objects" vs "vector objects" after the fact) is what makes the two
PDF layers align pixel-for-pixel and keeps all text/lines as real, editable
vector objects in the final file.

Resolution note: the basemap image is NOT embedded via matplotlib's
raster_pdf output — savefig would rasterize it at the figure's dpi (a few
hundred px across at most), throwing away most of the detail actually
present in the fetched page{N}_raw.jpg mosaics. Instead, this script writes
each page's exact map-panel rectangle (post aspect-fix, as a fraction of
the page) to <basemap_dir>/map_rects.json, and merge_layers.py inserts the
raw JPG directly into the merged PDF at that rectangle, at full native
resolution. raster_pdf is kept only as a quick low-res preview.

Usage:
    python -m roadbook.render_pages work/tor450_t1_..._data.pkl work/tor450_t1_..._basemaps
"""
import argparse
import json
import math
import pickle
import re
import textwrap
from datetime import date
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.gridspec as gridspec
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.collections import LineCollection
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.text import Text
from matplotlib.transforms import Bbox
from mpl_toolkits.axes_grid1.inset_locator import inset_axes
from PIL import Image

from roadbook.layout import (GRID, HEIGHT_RATIOS, PAGE_SIZE_IN, TABLE_FONT_PT, TABLE_HEAD_IN,
                             TABLE_ROW_IN, table_rows_per_page)

from roadbook.config import WORK_DIR

# ---- styling knobs -----------------------------------------------------
GRADIENT_SMOOTH_WINDOW = 11          # points, rolling average before computing % grade
GRADIENT_VMAX_CLIP = (15, 35)        # % grade range clipped for the color scale
# Direct blue -> purple -> red interpolation (no white midpoint, unlike
# RdBu_r/coolwarm) so flat/near-zero grade doesn't visually fade out
# against the white page.
GRADIENT_COLORS = [(0, 0, 1), (0.5, 0, 0.5), (1, 0, 0)]   # blue -> purple -> red
CMAP = LinearSegmentedColormap.from_list("blue_purple_red_grade", GRADIENT_COLORS)
# page geometry (HEIGHT_RATIOS, PAGE_SIZE_IN, GRID) lives in layout.py

# Legibility backing for text drawn over the busy basemap/track. Earlier
# versions used matplotlib path_effects.withStroke() (a white halo) for
# this, but that forces the PDF backend to convert the text into vector
# outline paths instead of real Tj text — invisible on screen, but it means
# the labels are no longer selectable/editable text in Illustrator/Acrobat,
# which breaks the "editable PDF" requirement this whole pipeline exists
# for. A bbox background patch achieves the same "readable over anything"
# effect while leaving the glyphs themselves as real text.
# label placement costs, see build_page (mirrored in webapp/static/app.js)
LABEL_VSTEPS = [10, 24, 38, 54, 70, -10, -24, -38, -54, -70]  # pt above (+) / below (-) the point
LABEL_TRACK_COST = 25.0  # per covered track sample, ~ px² of label overlap
LABEL_DIST_COST = 2.0    # per px of offset from the point
LABEL_CROSS_COST = 600.0  # per leader crossing another leader, or running through a label

LABEL_BG = dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.75)
LABEL_BG_DARK = dict(boxstyle="round,pad=0.12", facecolor="black", edgecolor="none", alpha=0.55)

# ---- per-render style (--style JSON) ------------------------------------
# Defaults reproduce the look above exactly. The web app's style panel
# writes a JSON with any subset of these keys (see roadbook/api.py
# clean_style() for the accepted ranges).
DEFAULT_STYLE = dict(
    label_font=None,     # font family for map + profile labels; None = matplotlib default
    track_width=1.4,     # pt, gradient-coloured map track (dark casing drawn 0.9 pt wider)
    profile_width=0.9,   # pt, gradient-coloured profile line (dark casing 0.6 pt wider)
    grade_colors=None,   # [descent, flat, climb] colours; None = GRADIENT_COLORS
    grade_max=None,      # % grade drawn at full colour; None = auto (97th percentile, clipped)
    label_offsets={},    # {label key: [dx, dy]} — dragged labels, see label_key()
)


def load_style(path=None):
    style = dict(DEFAULT_STYLE)
    if path:
        with open(path) as f:
            style.update({k: v for k, v in json.load(f).items() if k in DEFAULT_STYLE and v is not None})
    return style


def label_key(where, pnum, which):
    """Id of a draggable label: where = "map" | "prof", which = "start" /
    "end" or the POI's km. Must match labelKey() in webapp/static/app.js.
    The stored offset [dx, dy] is in points from the labelled point to the
    label's top-left corner, y pointing DOWN (screen convention)."""
    return f"{where}:{pnum}:{which if isinstance(which, str) else f'{which:.2f}'}"


def clean_name(name):
    repl = {
        " - waypoint": "",
        "Tratto attrezzato": "(via ferrata)",
    }
    for k, v in repl.items():
        name = name.replace(k, v)
    return name.strip(" -")


def place_name(pois, km, total_km, start_name, end_name, near_km=0.3):
    """Name of the waypoint at `km` (nearest within near_km). At the track's
    start/finish, prefer a waypoint named START/FINISH (a loop has both at
    km 0) and fall back to the GPX's start/end name; elsewhere fall back
    to the distance."""
    at_start, at_end = km < near_km, km > total_km - near_km
    cands = [p for p in pois if abs(p[0] - km) <= near_km]
    if at_start:
        cands = [p for p in cands if "start" in p[1].lower()] or ([] if start_name else cands)
    elif at_end:
        cands = [p for p in cands if re.search(r"finish|arriv", p[1], re.I)] or ([] if end_name else cands)
    if cands:
        return min(cands, key=lambda p: abs(p[0] - km))[1]
    if at_start and start_name:
        return start_name
    if at_end and end_name:
        return end_name
    return f"km {km:.1f}"


def page_subtitle(d0, d1, custom, pois, total_km, start_name, end_name, max_len=32):
    """Header subtitle: "km d0 → km d1" for an equal-distance split, the
    start/end place names for a custom (waypoint) split."""
    if not custom:
        return f"km {d0:.1f} → km {d1:.1f}"
    names = [place_name(pois, k, total_km, start_name, end_name) for k in (d0, d1)]
    names = [n if len(n) <= max_len else n[:max_len - 1].rstrip() + "…" for n in names]
    return " → ".join(names)


def compute_gradient(eles, dist):
    window = GRADIENT_SMOOTH_WINDOW
    pad = window // 2
    padded = np.pad(eles, (pad, pad), mode="edge")
    smoothed = np.convolve(padded, np.ones(window) / window, mode="valid")[: len(eles)]
    d_dist_m = np.diff(dist)
    d_ele_s = np.diff(smoothed)
    with np.errstate(divide="ignore", invalid="ignore"):
        grad = np.where(d_dist_m > 1, d_ele_s / d_dist_m * 100.0, 0.0)
    return grad


def build_page(mode, page, ctx):
    """mode: 'raster' (basemap image only) or 'vector' (everything else)."""
    d = ctx["data"]
    lats, lons, eles, dist = d["lats"], d["lons"], d["eles"], d["dist"]
    n_pages = ctx["n_pages"]
    edges = ctx["edges"]
    grad_all = ctx["grad_all"]
    cmap, norm = ctx["cmap"], ctx["norm"]
    basemap_bounds, basemap_imgs = ctx["basemap_bounds"], ctx["basemap_imgs"]
    race_title = ctx["race_title"]
    sector_code, start_name, end_name = d["sector_code"], d["start_name"], d["end_name"]
    version, update_tag = d["version"], d["update_tag"]
    pois_all = ctx["pois_all"]
    style = ctx["style"]
    font_kw = dict(fontfamily=style["label_font"]) if style["label_font"] else {}
    offsets = style["label_offsets"]
    dist_km = dist / 1000.0

    pnum = page + 1
    d0, d1 = edges[page], edges[page + 1]
    mask = (dist_km >= d0 - 0.1) & (dist_km <= d1 + 0.1)
    idx = np.where(mask)[0]
    seg_lon, seg_lat = lons[idx], lats[idx]
    seg_ele = eles[idx]
    seg_dist = dist_km[idx]
    seg_grad = grad_all[idx[:-1]]

    d_ele = np.diff(seg_ele)
    seg_gain = float(d_ele[d_ele > 0].sum())
    seg_loss = float(-d_ele[d_ele < 0].sum())
    seg_min, seg_max = float(seg_ele.min()), float(seg_ele.max())
    seg_len = float(seg_dist[-1] - seg_dist[0])

    page_pois = [p for p in pois_all if d0 - 0.05 <= p[0] <= d1 + 0.05]
    page_pois.sort(key=lambda p: p[0])
    merged = []
    for p in page_pois:
        if merged and abs(p[0] - merged[-1][0]) < 0.3:
            prev = merged[-1]
            merged[-1] = ((prev[0] + p[0]) / 2, prev[1] + " / " + p[1], p[2], p[3], (prev[4] + p[4]) / 2)
        else:
            merged.append(p)
    page_pois = merged

    is_vector = mode == "vector"

    fig = plt.figure(figsize=PAGE_SIZE_IN)
    if is_vector:
        fig.patch.set_alpha(0.0)
    gs = gridspec.GridSpec(4, 1, height_ratios=HEIGHT_RATIOS, **GRID)

    # ---- HEADER ----
    ax_h = fig.add_subplot(gs[0])
    if is_vector:
        subtitle = page_subtitle(d0, d1, ctx["custom_split"], pois_all, d["total_dist_km"],
                                 start_name, end_name)
        stats = f"Part {pnum}/{n_pages} ({d0:.1f}-{d1:.1f} km)  |  " + stats_text(
            seg_len, seg_gain, seg_loss, seg_min, seg_max)
        draw_header(ax_h, ctx, subtitle, stats)
    else:
        ax_h.axis("off"); ax_h.patch.set_alpha(0.0)

    # ---- MAP ----
    ax_m = fig.add_subplot(gs[1])
    ax_m.patch.set_alpha(0.0)
    bb = basemap_bounds[pnum]
    west, east, south, north = bb["west"], bb["east"], bb["south"], bb["north"]
    lat0 = (south + north) / 2
    xmin, xmax, ymin, ymax = west, east, south, north

    ax_m.set_xlim(xmin, xmax); ax_m.set_ylim(ymin, ymax)
    # True geographic proportions (1 lon-degree != 1 lat-degree in real
    # distance away from the equator) instead of stretching to fill the
    # frame. adjustable="box" shrinks the axes box to match instead of
    # distorting the data; anchor="C" re-centers that (now smaller) box
    # in the space the GridSpec allocated, so the map stays centered on
    # the page rather than sticking to one edge. Applied up front (before
    # any track/label is drawn) so every pixel-based measurement below —
    # the POI-label collision test in particular — uses the axes' real,
    # final on-page transform instead of a transient pre-aspect-fix one.
    # fetch_basemap.fill_panel() already sized the bounds to the panel's
    # proportions, so this is a no-op to within a pixel and the map fills
    # the whole panel.
    ax_m.set_aspect(1.0 / math.cos(math.radians(lat0)), adjustable="box", anchor="C")
    pos_m = ax_m.get_position()
    map_rect = dict(x0=pos_m.x0, y0=pos_m.y0, x1=pos_m.x1, y1=pos_m.y1)

    if not is_vector:
        # Low-res preview only — matplotlib's savefig dpi would otherwise
        # downsample the fetched mosaic before it ever reaches the PDF.
        # The final merged PDF embeds the raw page{N}_raw.jpg directly at
        # full native resolution instead (see merge_layers.py + map_rect
        # returned below); this figure/raster_pdf exists just so you can
        # quickly eyeball a page without waiting for the full merge.
        ax_m.imshow(basemap_imgs[pnum], extent=[west, east, south, north], origin="upper", zorder=0)
    else:
        km_per_deg_lat = 111.32
        km_per_deg_lon = 111.32 * math.cos(math.radians(lat0))

        grid_step = 2 if (xmax - xmin) * km_per_deg_lon < 15 else 5
        dlon = grid_step / km_per_deg_lon
        dlat = grid_step / km_per_deg_lat
        for gx in np.arange(math.floor(xmin / dlon) * dlon, xmax, dlon):
            ax_m.axvline(gx, color="white", linewidth=0.5, alpha=0.5, zorder=1)
        for gy in np.arange(math.floor(ymin / dlat) * dlat, ymax, dlat):
            ax_m.axhline(gy, color="white", linewidth=0.5, alpha=0.5, zorder=1)

        # neighbouring parts' track (fetch_basemap.page_bboxes made room
        # for it), faded + dashed so it can't be mistaken for this part's
        ov = bb.get("overlap_km", 0.0)
        for a, b in ((d0 - ov, d0), (d1, d1 + ov)) if ov > 0 else ():
            m = (dist_km >= a) & (dist_km <= b)
            if m.sum() > 1:
                ax_m.plot(lons[m], lats[m], color="#1a1a1a", linewidth=style["track_width"], alpha=0.45,
                          linestyle=(0, (2, 1.5)), dash_capstyle="round", zorder=1.9)

        ax_m.plot(seg_lon, seg_lat, color="#1a1a1a", linewidth=style["track_width"] + 0.9, solid_capstyle="round",
                  solid_joinstyle="round", zorder=2)

        pts_xy = np.array([seg_lon, seg_lat]).T.reshape(-1, 1, 2)
        segs = np.concatenate([pts_xy[:-1], pts_xy[1:]], axis=1)
        lc = LineCollection(segs, cmap=cmap, norm=norm, linewidth=style["track_width"], zorder=3,
                             capstyle="round", joinstyle="round")
        lc.set_array(seg_grad)
        ax_m.add_collection(lc)

        is_true_start = page == 0
        is_true_end = page == n_pages - 1
        start_label = "START" if is_true_start else f"{d0:.1f} km\n(from part {pnum - 1})"
        end_label = "FINISH" if is_true_end else f"{d1:.1f} km\n(continues part {pnum + 1})"

        ax_m.scatter([seg_lon[0]], [seg_lat[0]], marker="o", s=70, facecolor="#1b8a3a",
                     edgecolor="white", linewidth=1.2, zorder=6)
        ax_m.scatter([seg_lon[-1]], [seg_lat[-1]], marker="o", s=70, facecolor="#c0392b",
                     edgecolor="white", linewidth=1.2, zorder=6)
        for (_km, _name, plat, plon, _pele) in page_pois:
            ax_m.scatter([plon], [plat], marker="o", s=32, facecolor="#c0392b", edgecolor="white",
                         linewidth=1.0, zorder=7)

        # ---- map furniture, drawn before the labels so they can avoid it --
        # longest round length up to ~40 % of the map width (5 km on a
        # 15-20 km wide map, 1 km at 1:25k where the map is ~5 km wide)
        width_km = (xmax - xmin) * km_per_deg_lon
        sb_len = max([l for l in (0.5, 1, 2, 5, 10, 20, 50) if l <= width_km * 0.4] or [0.5])
        sb_len_deg = sb_len / km_per_deg_lon
        sb_x0 = xmax - sb_len_deg - (xmax - xmin) * 0.06
        sb_y0 = ymin + (ymax - ymin) * 0.05
        furniture_artists = []  # obstacles for the label placement below
        furniture_artists += ax_m.plot([sb_x0, sb_x0 + sb_len_deg], [sb_y0, sb_y0], color="black", linewidth=2.4, zorder=9,
                                       path_effects=[pe.Stroke(linewidth=3.6, foreground="white"), pe.Normal()])
        ax_m.plot([sb_x0, sb_x0], [sb_y0 - (ymax - ymin) * 0.01, sb_y0 + (ymax - ymin) * 0.01], color="black", linewidth=2.4, zorder=9)
        ax_m.plot([sb_x0 + sb_len_deg, sb_x0 + sb_len_deg], [sb_y0 - (ymax - ymin) * 0.01, sb_y0 + (ymax - ymin) * 0.01], color="black", linewidth=2.4, zorder=9)
        print_scale = bb.get("print_scale")
        sb_text = f"{sb_len:g} km" + (f"  ·  1:{print_scale:,}" if print_scale else "")
        furniture_artists.append(ax_m.text(sb_x0 + sb_len_deg / 2, sb_y0 + (ymax - ymin) * 0.018, sb_text,
                                           ha="center", fontsize=8.5, bbox=LABEL_BG, zorder=9))

        nx0, ny0 = xmin + (xmax - xmin) * 0.05, ymax - (ymax - ymin) * 0.12
        furniture_artists.append(ax_m.annotate("N", xy=(nx0, ny0 + (ymax - ymin) * 0.05), xytext=(nx0, ny0),
                      ha="center", fontsize=10.5, fontweight="bold", color="black",
                      bbox=LABEL_BG,
                      arrowprops=dict(arrowstyle="-|>", color="black", linewidth=1.8)))

        furniture_artists.append(ax_m.text(0.01, 0.01, ctx["attribution"],
                                           transform=ax_m.transAxes, fontsize=6.5, color="white", va="bottom",
                                           ha="left", bbox=LABEL_BG_DARK))

        cax = inset_axes(ax_m, width="1.8%", height="28%", loc="upper right",
                          bbox_to_anchor=(-0.012, -0.02, 1, 1), bbox_transform=ax_m.transAxes, borderpad=0)
        cbar = fig.colorbar(lc, cax=cax)
        cbar.set_label("grade (%)", fontsize=7.5, color="white")
        cbar.ax.tick_params(labelsize=6.5, colors="white")
        cbar.outline.set_edgecolor("white")

        # ---- labels: collision-aware placement ---------------------------
        # Each label (start/end and every POI) tries a ring of offsets around
        # its point — either side, two distances out, stepping further away
        # vertically — and takes the one with the lowest cost:
        #   overlap area with labels placed so far, markers and map furniture
        #   (scale bar, north arrow, attribution, colour bar)
        #   + LABEL_TRACK_COST per track sample (~3 px apart) it covers
        #   + LABEL_DIST_COST per px of offset (nearer is better)
        #   + LABEL_CROSS_COST per crossing with a leader line placed so far,
        #     and per leader running through a label (this one's or another's)
        #   + a huge penalty for leaving the map panel.
        # A label is measured once per alignment; candidates are scored by
        # translating that box, so no annotation is created per candidate.
        # Dragged labels go first and are fixed obstacles for the rest.
        # webapp/static/app.js mirrors this (placeLabels) for the preview.
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        map_disp_bbox = ax_m.get_window_extent(renderer=renderer)
        px_per_pt = fig.dpi / 72

        marker_r_px = 8  # keep labels clear of marker glyphs, not just each other

        def marker_obstacle(lon, lat):
            dxpx, dypx = ax_m.transData.transform((lon, lat))
            return Bbox.from_extents(dxpx - marker_r_px, dypx - marker_r_px,
                                      dxpx + marker_r_px, dypx + marker_r_px)

        def overlap_area(b1, b2):
            dx = min(b1.x1, b2.x1) - max(b1.x0, b2.x0)
            dy = min(b1.y1, b2.y1) - max(b1.y0, b2.y0)
            return max(dx, 0.0) * max(dy, 0.0)

        track_px = ax_m.transData.transform(np.column_stack([seg_lon, seg_lat]))
        step_px = np.r_[0.0, np.cumsum(np.hypot(*np.diff(track_px, axis=0).T))]
        _, keep = np.unique(np.floor(step_px / 3.0), return_index=True)
        track_x, track_y = track_px[keep].T

        obstacle_boxes = [marker_obstacle(seg_lon[0], seg_lat[0]), marker_obstacle(seg_lon[-1], seg_lat[-1])]
        obstacle_boxes += [marker_obstacle(plon, plat) for (_k, _n, plat, plon, _e) in page_pois]
        obstacle_boxes += [a.get_window_extent(renderer=renderer) for a in furniture_artists]
        obstacle_boxes.append(cax.get_tightbbox(renderer))

        leaders = []      # (anchor px, label attach px) of labels placed so far
        label_boxes = []  # their boxes (leaders must not run through these)

        def attach(box, a):
            """Point of `box` nearest to `a` — where the leader line meets the label."""
            return (min(max(a[0], box.x0), box.x1), min(max(a[1], box.y0), box.y1))

        def crosses(p1, p2, q1, q2):
            def orient(a, b, c):
                return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
            return (orient(p1, p2, q1) * orient(p1, p2, q2) < 0 and
                    orient(q1, q2, p1) * orient(q1, q2, p2) < 0)

        def through(a, b, box):
            """Does segment a-b pass through `box` (shrunk 1 px, so a leader
            ending on its own label's edge doesn't count)? Sampled."""
            t = np.linspace(0.05, 0.95, 12)
            x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            return bool(np.any((x > box.x0 + 1) & (x < box.x1 - 1) & (y > box.y0 + 1) & (y < box.y1 - 1)))

        def cost(box, dist_px, anchor):
            c = sum(overlap_area(box, ob) for ob in obstacle_boxes)
            tip = attach(box, anchor)
            c += LABEL_CROSS_COST * sum(crosses(anchor, tip, a, b) for a, b in leaders)
            c += LABEL_CROSS_COST * sum(through(anchor, tip, lb) for lb in label_boxes)
            c += LABEL_CROSS_COST * sum(through(a, b, box) for a, b in leaders)
            c += LABEL_TRACK_COST * np.count_nonzero((track_x >= box.x0) & (track_x <= box.x1) &
                                                     (track_y >= box.y0) & (track_y <= box.y1))
            c += LABEL_DIST_COST * dist_px
            if not (box.x0 >= map_disp_bbox.x0 and box.x1 <= map_disp_bbox.x1 and
                    box.y0 >= map_disp_bbox.y0 and box.y1 <= map_disp_bbox.y1):
                c += 1e7  # dwarfs any real cost, but finite so the least-bad one still wins
            return c

        def candidates(x_px, dxs):
            sides = (1, -1) if x_px <= map_disp_bbox.x0 + map_disp_bbox.width / 2 else (-1, 1)
            for side in sides:
                for dx in dxs:
                    for dy in LABEL_VSTEPS:
                        yield side * dx, dy, "left" if side > 0 else "right"

        def place_label(text, xy, dxs, text_kw, leader_color):
            """Annotate `text` at the cheapest candidate offset; returns its box."""
            anchor = tuple(ax_m.transData.transform(xy))
            x_px = anchor[0]
            measured, best = {}, None
            for dx, dy, ha in candidates(x_px, dxs):
                if ha not in measured:
                    probe = ax_m.annotate(text, xy, xytext=(0, 0), textcoords="offset points",
                                          ha=ha, va="baseline", bbox=LABEL_BG, **text_kw)
                    # + the white background's padding (LABEL_BG pad x font size)
                    pad = 0.15 * probe.get_fontsize() * px_per_pt
                    measured[ha] = probe.get_window_extent(renderer=renderer).padded(pad)
                    probe.remove()
                box = measured[ha].translated(dx * px_per_pt, dy * px_per_pt)
                c = cost(box, math.hypot(dx, dy) * px_per_pt, anchor)
                if best is None or c < best[0]:
                    best = (c, dx, dy, ha, box)
            _c, dx, dy, ha, box = best
            ax_m.annotate(text, xy, xytext=(dx, dy), textcoords="offset points", ha=ha, va="baseline",
                          bbox=LABEL_BG, **text_kw,
                          arrowprops=dict(arrowstyle="-", color=leader_color, linewidth=0.5,
                                          shrinkA=0, shrinkB=0))
            leaders.append((anchor, attach(box, anchor)))
            label_boxes.append(box)
            return box

        def place_dragged(text, xy, off, text_kw, leader_color):
            ann = ax_m.annotate(text, xy, xytext=(off[0], -off[1]), textcoords="offset points",
                                ha="left", va="top", bbox=LABEL_BG, **text_kw,
                                arrowprops=dict(arrowstyle="-", color=leader_color, linewidth=0.5,
                                                shrinkA=0, shrinkB=0) if leader_color else None)
            box = Text.get_window_extent(ann, renderer).padded(0.15 * ann.get_fontsize() * px_per_pt)
            anchor = tuple(ax_m.transData.transform(xy))
            leaders.append((anchor, attach(box, anchor)))
            label_boxes.append(box)
            return box  # text box only, not the leader

        end_kw = dict(fontsize=8.5, fontweight="bold", zorder=8, **font_kw)
        labels = [  # key, text, anchor, candidate dx (pt), text kwargs, leader colour
            (label_key("map", pnum, "start"), start_label, (seg_lon[0], seg_lat[0]), (8, 24),
             dict(end_kw, color="#1b8a3a"), "#1b8a3a"),
            (label_key("map", pnum, "end"), end_label, (seg_lon[-1], seg_lat[-1]), (8, 24),
             dict(end_kw, color="#c0392b"), "#c0392b"),
        ] + [
            (label_key("map", pnum, km), f"{name}\n{km:.1f} km - {pele:.0f} m", (plon, plat), (12, 30),
             dict(fontsize=8, color="#1a1a2e", zorder=8, **font_kw), "#333333")
            for (km, name, plat, plon, pele) in page_pois
        ]
        for key, text, xy, _dxs, text_kw, leader in labels:
            if offsets.get(key):
                obstacle_boxes.append(place_dragged(text, xy, offsets[key], text_kw, leader))
        for key, text, xy, dxs, text_kw, leader in labels:
            if not offsets.get(key):
                obstacle_boxes.append(place_label(text, xy, dxs, text_kw, leader))

    ax_m.set_xticks([]); ax_m.set_yticks([])
    if is_vector:
        for spine in ax_m.spines.values():
            spine.set_edgecolor("#1a1a2e"); spine.set_linewidth(1.0)
    else:
        for spine in ax_m.spines.values():
            spine.set_visible(False)

    # ---- ELEVATION PROFILE ----
    ax_e = fig.add_subplot(gs[2])
    pos = ax_e.get_position()
    ax_e.set_position([pos.x0 + 0.045, pos.y0, pos.width - 0.055, pos.height])
    ax_e.patch.set_alpha(0.0)
    if not is_vector:
        ax_e.axis("off")
    else:
        ax_e.set_facecolor("none")
        ax_e.plot(seg_dist, seg_ele, color="#1a1a1a", linewidth=style["profile_width"] + 0.6, solid_capstyle="round",
                  solid_joinstyle="round", zorder=2)
        epts = np.array([seg_dist, seg_ele]).T.reshape(-1, 1, 2)
        esegs = np.concatenate([epts[:-1], epts[1:]], axis=1)
        elc = LineCollection(esegs, cmap=cmap, norm=norm, linewidth=style["profile_width"], zorder=3,
                              capstyle="round", joinstyle="round")
        elc.set_array(seg_grad)
        ax_e.add_collection(elc)

        for (km, name, plat, plon, pele) in page_pois:
            ax_e.axvline(km, color="#dddddd", linewidth=0.4, zorder=1)
            wrapped = textwrap.fill(name, width=11)
            lines = wrapped.split("\n")
            if len(lines) > 2:
                lines = lines[:2]
                lines[-1] = lines[-1] + "..."
            wrapped = "\n".join(lines)
            off = offsets.get(label_key("prof", pnum, km))
            pos = (dict(xytext=(off[0], -off[1]), ha="left", va="top",
                        arrowprops=dict(arrowstyle="-", color="#888888", linewidth=0.4, shrinkA=0, shrinkB=0))
                   if off else dict(xytext=(0, 5), ha="center", va="bottom"))
            ax_e.annotate(wrapped, xy=(km, pele), textcoords="offset points", **pos, **font_kw,
                          rotation=0, fontsize=6.5, color="#1a1a2e",
                          linespacing=1.15, zorder=5,
                          bbox=LABEL_BG)
            ax_e.scatter([km], [pele], s=8, facecolor="#2c3e50", edgecolor="white", linewidth=0.4, zorder=6)

        ax_e.scatter([seg_dist[0]], [seg_ele[0]], marker="o", s=40, facecolor="#1b8a3a", edgecolor="white", linewidth=1.0, zorder=7)
        ax_e.scatter([seg_dist[-1]], [seg_ele[-1]], marker="s", s=40, facecolor="#c0392b", edgecolor="white", linewidth=1.0, zorder=7)

        ax_e.set_xlim(d0, d1)
        ax_e.set_ylim(min(seg_min - 100, 900), seg_max + 340)
        ax_e.set_ylabel("elevation (m)", fontsize=7.5)
        ax_e.tick_params(labelsize=7)
        ax_e.grid(axis="x", color="#eeeeee", linewidth=0.4)
        for spine in ["top", "right"]:
            ax_e.spines[spine].set_visible(False)
        xticks = [t for t in ax_e.get_xticks() if d0 <= t <= d1]
        ax_e.set_xticks(xticks)
        ax_e.annotate("km", xy=(1.0, 0), xycoords=("axes fraction", "axes fraction"),
                      xytext=(4, -3), textcoords="offset points", fontsize=7.5,
                      color="#333333", ha="left", va="top")

    # ---- FOOTER ----
    ax_f = fig.add_subplot(gs[3])
    if is_vector:
        draw_footer(ax_f, ctx, ctx["sheets"].index(dict(kind="map", page=page)) + 1)
    else:
        ax_f.axis("off"); ax_f.patch.set_alpha(0.0)

    return fig, map_rect


def stats_text(length, gain, loss, ele_min, ele_max):
    return (f"Distance {length:.1f} km  |  D+ {gain:.0f} m  |  D- {loss:.0f} m  |  "
            f"Elev min/max {ele_min:.0f}/{ele_max:.0f} m")


def draw_header(ax_h, ctx, subtitle, stats):
    """Page header: title, sector line + subtitle (right), stats line, rule.
    Shared by the map pages and the final waypoint-table page."""
    d = ctx["data"]
    ax_h.set_xlim(0, 1); ax_h.set_ylim(0, 1); ax_h.axis("off")
    ax_h.patch.set_alpha(0.0)
    ax_h.text(0, 1.0, ctx["race_title"], fontsize=13, fontweight="bold", va="top", ha="left", color="#1a1a2e")
    ax_h.text(0, 0.40, f"Sector {d['sector_code']}: {d['start_name']} -> {d['end_name']}", fontsize=10.5,
              fontweight="bold", va="top", ha="left", color="#16213e")
    ax_h.text(1.0, 0.40, subtitle, fontsize=10.5, fontweight="bold", va="top", ha="right", color="#2f3e8a")
    ax_h.text(0, -0.05, stats, fontsize=8, va="top", ha="left", color="#333333", family="monospace")
    if d["version"] or d["update_tag"]:
        ax_h.text(1.0, 1.0, f"Rev. {d['version']} - {d['update_tag']}", fontsize=7.5, va="top", ha="right",
                  color="#888888")
    ax_h.plot([0, 1], [-0.28, -0.28], color="#1a1a2e", linewidth=1.2, transform=ax_h.transAxes, clip_on=False)


def draw_footer(ax_f, ctx, num):
    """Footer with the physical page number (position in ctx["sheets"])."""
    d = ctx["data"]
    total = len(ctx["sheets"])
    ax_f.axis("off"); ax_f.patch.set_alpha(0.0)
    ax_f.set_xlim(0, 1); ax_f.set_ylim(0, 1)
    ax_f.plot([0, 1], [1.0, 1.0], color="#1a1a2e", linewidth=0.7, transform=ax_f.transAxes, clip_on=False)
    ax_f.text(0, 0.15, f"Roadbook - Sector {d['sector_code']} - Page {num} of {total}",
              fontsize=7.5, color="#666666")
    ax_f.text(1.0, 0.15, f"Generated {date.today().isoformat()} - GPX: {d['start_name']} - {d['end_name']} "
              f"({d['update_tag']})", fontsize=7.5, color="#666666", ha="right")


def merge_close_pois(pois, within_km=0.3):
    """Waypoints closer than within_km along the track become one entry
    ("A / B") — same rule the map labels use."""
    merged = []
    for p in sorted(pois, key=lambda p: p[0]):
        if merged and abs(p[0] - merged[-1][0]) < within_km:
            prev = merged[-1]
            merged[-1] = ((prev[0] + p[0]) / 2, prev[1] + " / " + p[1], p[2], p[3], (prev[4] + p[4]) / 2)
        else:
            merged.append(p)
    return merged


def waypoint_table(data, pois, extra=()):
    """Every point along the track (start and finish always included), as
    dicts with name, km, boundary, and the distance / D+ / D- to the
    previous and next point and from the start. "Previous" and "next" run
    along the whole track, also across page breaks. `extra` adds (km, name)
    points — page start/end rows, flagged boundary=True — unless a point
    already sits within 0.3 km. D+/D- are raw elevation-difference sums,
    the same as the header stats."""
    dist_km = data["dist"] / 1000.0
    total = float(data["total_dist_km"])
    d_ele = np.diff(data["eles"])
    cum_up = np.concatenate([[0.0], np.cumsum(np.clip(d_ele, 0, None))])
    cum_down = np.concatenate([[0.0], np.cumsum(np.clip(-d_ele, 0, None))])

    # a waypoint at the very start/finish *is* that end of the track
    points = [(0.0 if p[0] <= 0.3 else total if p[0] >= total - 0.3 else float(p[0]), p[1], False)
              for p in merge_close_pois(pois)]
    if not points or points[0][0] > 0.3:
        points.insert(0, (0.0, data["start_name"] or "Start", False))
    if points[-1][0] < total - 0.3:
        points.append((total, data["end_name"] or "Finish", False))
    for k, name in extra:
        if all(abs(p[0] - k) > 0.3 for p in points):
            points.append((float(k), name, True))
    points.sort(key=lambda p: p[0])

    km = [p[0] for p in points]
    up = np.interp(km, dist_km, cum_up).tolist()
    down = np.interp(km, dist_km, cum_down).tolist()
    rows = []
    for i, (k, name, boundary) in enumerate(points):
        prev_i, next_i = (i - 1 if i else None), (i + 1 if i + 1 < len(points) else None)
        rows.append(dict(
            name=name, km=k, boundary=boundary,
            dkm_prev=None if prev_i is None else k - km[prev_i],
            up_prev=None if prev_i is None else up[i] - up[prev_i],
            down_prev=None if prev_i is None else down[i] - down[prev_i],
            up_next=None if next_i is None else up[next_i] - up[i],
            down_next=None if next_i is None else down[next_i] - down[i],
            up_total=up[i], down_total=down[i],
        ))
    return rows


def _chunks(rows):
    n = table_rows_per_page()
    return [rows[i:i + n] for i in range(0, len(rows), n)] or [[]]


def build_sheets(data, pois, edges, part_tables=True, final_table=True):
    """The document's page sequence: each map page, optionally followed by
    its part table, then optionally the final table of all points. Each
    entry is a dict(kind="map"|"part"|"final", page, rows, chunk, n_chunks);
    tables longer than one page are split into chunks. A sheet's position
    in this list + 1 is its page number (footer). Part tables also get
    "Part N start/end" rows at the page edges (when no waypoint is there),
    so their rows chain from the page start to the page end."""
    n_parts = len(edges) - 1
    sheets = []
    for p in range(n_parts):
        sheets.append(dict(kind="map", page=p))
        if part_tables:
            d0, d1 = float(edges[p]), float(edges[p + 1])
            extra = ([(d0, f"Part {p + 1} start")] if p > 0 else []) + \
                    ([(d1, f"Part {p + 1} end")] if p < n_parts - 1 else [])
            part = [r for r in waypoint_table(data, pois, extra) if d0 - 0.05 <= r["km"] <= d1 + 0.05]
            chunks = _chunks(part)
            sheets += [dict(kind="part", page=p, rows=c, chunk=i, n_chunks=len(chunks))
                       for i, c in enumerate(chunks)]
    if final_table:
        chunks = _chunks(waypoint_table(data, pois))
        sheets += [dict(kind="final", page=None, rows=c, chunk=i, n_chunks=len(chunks))
                   for i, c in enumerate(chunks)]
    return sheets


def segment_stats(data, d0, d1):
    """(length km, D+, D-, min ele, max ele) of the track between d0 and d1
    km — the same slice and sums a map page's header uses."""
    dist_km = data["dist"] / 1000.0
    m = (dist_km >= d0 - 0.1) & (dist_km <= d1 + 0.1)
    seg_ele, seg_dist = data["eles"][m], dist_km[m]
    d_ele = np.diff(seg_ele)
    return (float(seg_dist[-1] - seg_dist[0]), float(d_ele[d_ele > 0].sum()), float(-d_ele[d_ele < 0].sum()),
            float(seg_ele.min()), float(seg_ele.max()))


def table_header(sheet, data, edges, custom_split, pois):
    """(subtitle, stats line) for a table sheet: a part table repeats its
    map page's header, the final table covers the whole track."""
    more = f" {sheet['chunk'] + 1}/{sheet['n_chunks']}" if sheet["n_chunks"] > 1 else ""
    total = float(data["total_dist_km"])
    if sheet["kind"] == "part":
        p = sheet["page"]
        d0, d1 = float(edges[p]), float(edges[p + 1])
        label = f"Part {p + 1}/{len(edges) - 1} waypoints{more}"
    else:
        d0, d1 = 0.0, total
        label = f"All waypoints{more}"
    subtitle = page_subtitle(d0, d1, custom_split, pois, total, data["start_name"], data["end_name"])
    return subtitle, f"{label} ({d0:.1f}-{d1:.1f} km)  |  " + stats_text(*segment_stats(data, d0, d1))


TABLE_COLUMNS = [  # (key, header, width fraction, format)
    ("name", "Point", 0.31, None),
    ("km", "km\nfrom start", 0.085, "{:.1f}"),
    ("dkm_prev", "km from\nprevious", 0.085, "{:.1f}"),
    ("up_prev", "D+ from\nprevious", 0.075, "{:.0f}"),
    ("down_prev", "D- from\nprevious", 0.075, "{:.0f}"),
    ("up_next", "D+ to\nnext", 0.075, "{:.0f}"),
    ("down_next", "D- to\nnext", 0.075, "{:.0f}"),
    ("up_total", "D+ from\nstart", 0.11, "{:.0f}"),
    ("down_total", "D- from\nstart", 0.11, "{:.0f}"),
]


def build_table_page(num, ctx):
    """Vector-only waypoint-table page for ctx["sheets"][num - 1] — a
    part table (after its map page) or the final table of all points."""
    sheet = ctx["sheets"][num - 1]
    fig = plt.figure(figsize=PAGE_SIZE_IN)
    fig.patch.set_alpha(0.0)
    gs = gridspec.GridSpec(4, 1, height_ratios=HEIGHT_RATIOS, **GRID)
    draw_header(fig.add_subplot(gs[0]), ctx,
                *table_header(sheet, ctx["data"], ctx["edges"], ctx["custom_split"], ctx["pois_all"]))

    # table fills the map + profile rows
    top = fig.add_subplot(gs[1]).get_position()
    bottom = fig.add_subplot(gs[2]).get_position()
    for a in fig.axes[1:]:
        a.remove()
    ax = fig.add_axes([top.x0, bottom.y0, top.width, top.y1 - bottom.y0])
    ax.axis("off"); ax.patch.set_alpha(0.0)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)

    rows = sheet["rows"]
    height_in = (top.y1 - bottom.y0) * PAGE_SIZE_IN[1]
    head_h = TABLE_HEAD_IN / height_in                          # axes fraction
    row_h = min(TABLE_ROW_IN / height_in, (0.99 - head_h) / max(len(rows), 1))
    size = max(TABLE_FONT_PT[0], min(TABLE_FONT_PT[1], row_h * height_in * 72 * 0.55))  # pt, shrinks for long lists
    font_kw = dict(fontfamily=ctx["style"]["label_font"]) if ctx["style"]["label_font"] else {}
    max_chars = int(TABLE_COLUMNS[0][2] * top.width * PAGE_SIZE_IN[0] * 72 / (size * 0.55))

    xs = np.concatenate([[0], np.cumsum([c[2] for c in TABLE_COLUMNS])])
    pad = 0.006
    ax.add_patch(plt.Rectangle((0, 1 - head_h), 1, head_h, facecolor="#1a1a2e", edgecolor="none"))
    for (key, label, _, _), x0, x1 in zip(TABLE_COLUMNS, xs[:-1], xs[1:]):
        left = key == "name"
        ax.text(x0 + pad if left else x1 - pad, 1 - head_h / 2, label, color="white", fontsize=7.5,
                fontweight="bold", ha="left" if left else "right", va="center", linespacing=1.1)
    for i, r in enumerate(rows):
        y1 = 1 - head_h - i * row_h
        if i % 2:
            ax.add_patch(plt.Rectangle((0, y1 - row_h), 1, row_h, facecolor="#f0f2f6", edgecolor="none"))
        ax.plot([0, 1], [y1 - row_h] * 2, color="#d5d9e0", linewidth=0.4)
        for (key, _, _, fmt), x0, x1 in zip(TABLE_COLUMNS, xs[:-1], xs[1:]):
            v = r[key]
            if fmt is None:
                text = v if len(v) <= max_chars else v[:max_chars - 1].rstrip() + "…"
            else:
                text = "–" if v is None else fmt.format(v)
            left = key == "name"
            ax.text(x0 + pad if left else x1 - pad, y1 - row_h / 2, text, fontsize=size,
                    ha="left" if left else "right", va="center",
                    color="#5a6170" if r["boundary"] else "#1a1a2e",
                    fontstyle="italic" if r["boundary"] else "normal",
                    fontweight="bold" if key in ("name", "km") else "normal", **font_kw)
    # light separators: between the "previous", "next" and "from start" groups
    y_end = 1 - head_h - len(rows) * row_h
    for k in (2, 5, 7):
        ax.plot([xs[k], xs[k]], [y_end, 1], color="#b8bec9", linewidth=0.5)
    if not rows:
        ax.text(0.5, 1 - head_h - 0.05, "No waypoints.", ha="center", va="top", fontsize=9, color="#666666")

    draw_footer(fig.add_subplot(gs[3]), ctx, num)
    return fig


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_pkl", help="Output of parse_gpx.py")
    ap.add_argument("basemap_dir", help="Output folder of fetch_basemap.py (contains pageN_raw.jpg + bounds.json)")
    ap.add_argument("--pages", type=int, default=4,
                     help="Only used for basemap dirs older than bounds.json page edges (d0/d1); "
                          "the page split is otherwise read from bounds.json (default: 4)")
    ap.add_argument("--race-title", default=None, help="Override the header race title (default: guessed from filename)")
    ap.add_argument("--out-dir", default=None, help="Output folder for the two intermediate PDFs (default: work/)")
    ap.add_argument("--style", default=None,
                     help="JSON file overriding DEFAULT_STYLE (label font, line widths, colour scale, label offsets)")
    ap.add_argument("--no-part-tables", action="store_true",
                     help="Don't add a waypoint-table page after each map page")
    ap.add_argument("--no-final-table", action="store_true",
                     help="Don't add the final waypoint table of all points")
    ap.add_argument("--no-tables", action="store_true", help="Neither of the two")
    args = ap.parse_args()

    data_pkl = Path(args.data_pkl)
    with open(data_pkl, "rb") as f:
        data = pickle.load(f)

    basemap_dir = Path(args.basemap_dir)
    with open(basemap_dir / "bounds.json") as f:
        basemap_bounds = {int(k): v for k, v in json.load(f).items()}
    basemap_imgs = {p: np.asarray(Image.open(basemap_dir / f"page{p}_raw.jpg")) for p in basemap_bounds}

    provider_meta_path = basemap_dir / "provider.json"
    if provider_meta_path.exists():
        with open(provider_meta_path) as f:
            attribution = json.load(f)["attribution"]
    else:
        # Older basemap_dir from before provider.json existed — fall back
        # to the OpenTopoMap default that fetch_basemap.py used exclusively.
        attribution = "Map data (c) OpenStreetMap contributors, SRTM | Map style (c) OpenTopoMap (CC-BY-SA)"

    pois_all = [(km, clean_name(name), plat, plon, pele) for (km, name, plat, plon, pele) in data["pois"]]

    grad_all = compute_gradient(data["eles"], data["dist"])
    style = load_style(args.style)
    if style["grade_max"]:
        grad_vmax = float(style["grade_max"])
    else:
        grad_vmax = float(np.clip(np.percentile(np.abs(grad_all), 97), *GRADIENT_VMAX_CLIP))
    cmap = (LinearSegmentedColormap.from_list("custom_grade", style["grade_colors"])
            if style["grade_colors"] else CMAP)
    norm = Normalize(vmin=-grad_vmax, vmax=grad_vmax)

    pages_sorted = sorted(basemap_bounds)
    if all("d0" in basemap_bounds[p] for p in pages_sorted):
        # the split fetch_basemap actually used (equal or --splits)
        edges = np.array([basemap_bounds[p]["d0"] for p in pages_sorted]
                         + [basemap_bounds[pages_sorted[-1]]["d1"]])
    else:
        edges = np.linspace(0, data["total_dist_km"], args.pages + 1)
    n_pages = len(edges) - 1
    custom_split = any(basemap_bounds[p].get("split") == "custom" for p in pages_sorted)

    ctx = dict(
        data=data, n_pages=n_pages, edges=edges, custom_split=custom_split, grad_all=grad_all,
        sheets=build_sheets(data, pois_all, edges,
                            part_tables=not (args.no_tables or args.no_part_tables),
                            final_table=not (args.no_tables or args.no_final_table)),
        cmap=cmap, norm=norm, basemap_bounds=basemap_bounds, basemap_imgs=basemap_imgs,
        race_title=args.race_title or data.get("race_title", "Roadbook"),
        pois_all=pois_all, attribution=attribution, style=style,
    )

    slug = data_pkl.stem.replace("_data", "")
    out_dir = Path(args.out_dir) if args.out_dir else WORK_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    raster_pdf = out_dir / f"{slug}_raster.pdf"
    vector_pdf = out_dir / f"{slug}_vector.pdf"

    # map_rects is keyed by vector-PDF page (1-based); "basemap" names the
    # pageN_raw.jpg to place there. Table pages have no entry (no basemap).
    map_rects = {}
    with PdfPages(raster_pdf) as pdf_r, PdfPages(vector_pdf) as pdf_v:
        for num, sheet in enumerate(ctx["sheets"], start=1):
            if sheet["kind"] == "map":
                page = sheet["page"]
                fig_r, _ = build_page("raster", page, ctx)
                pdf_r.savefig(fig_r)
                plt.close(fig_r)

                fig_v, map_rect = build_page("vector", page, ctx)
                map_rects[num] = dict(map_rect, basemap=page + 1)
            else:
                fig_v = build_table_page(num, ctx)
            pdf_v.savefig(fig_v, transparent=True)
            plt.close(fig_v)

    map_rects_path = basemap_dir / "map_rects.json"
    with open(map_rects_path, "w") as f:
        json.dump(map_rects, f, indent=2)

    n_tables = sum(s["kind"] != "map" for s in ctx["sheets"])
    print(f"Rendered {n_pages} map pages + {n_tables} waypoint-table pages "
          f"(grad_vmax={grad_vmax:.1f}%)")
    print(f"  raster -> {raster_pdf}  (low-res preview only)")
    print(f"  vector -> {vector_pdf}")
    print(f"  map rects -> {map_rects_path}  (used by merge_layers.py to place full-res JPGs)")


if __name__ == "__main__":
    main()
