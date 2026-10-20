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
from matplotlib.transforms import Bbox
from mpl_toolkits.axes_grid1.inset_locator import inset_axes
from PIL import Image

from roadbook.layout import GRID, HEIGHT_RATIOS, PAGE_SIZE_IN

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = PROJECT_ROOT / "work"

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
    ax_h.set_xlim(0, 1); ax_h.set_ylim(0, 1); ax_h.axis("off")
    ax_h.patch.set_alpha(0.0)
    if is_vector:
        ax_h.text(0, 1.0, race_title, fontsize=13, fontweight="bold", va="top", ha="left", color="#1a1a2e")
        ax_h.text(0, 0.40, f"Sector {sector_code}: {start_name} -> {end_name}", fontsize=10.5,
                   fontweight="bold", va="top", ha="left", color="#16213e")
        ax_h.text(1.0, 0.40, page_subtitle(d0, d1, ctx["custom_split"], pois_all, d["total_dist_km"],
                                           start_name, end_name),
                  fontsize=10.5, fontweight="bold", va="top", ha="right", color="#2f3e8a")
        stats = (f"Part {pnum}/{n_pages} ({d0:.1f}-{d1:.1f} km)  |  "
                 f"Distance {seg_len:.1f} km  |  D+ {seg_gain:.0f} m  |  D- {seg_loss:.0f} m  |  "
                 f"Elev min/max {seg_min:.0f}/{seg_max:.0f} m")
        ax_h.text(0, -0.05, stats, fontsize=8, va="top", ha="left", color="#333333", family="monospace")
        if version or update_tag:
            ax_h.text(1.0, 1.0, f"Rev. {version} - {update_tag}", fontsize=7.5, va="top", ha="right", color="#888888")
        ax_h.plot([0, 1], [-0.28, -0.28], color="#1a1a2e", linewidth=1.2, transform=ax_h.transAxes, clip_on=False)

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
        def text_pos(key, default_xytext, ha="left", va="baseline"):
            """annotate() placement: the dragged offset if there is one, else the default."""
            off = offsets.get(key)
            if off:
                return dict(xytext=(off[0], -off[1]), ha="left", va="top")
            return dict(xytext=default_xytext, ha=ha, va=va)

        ann_start = ax_m.annotate(start_label, (seg_lon[0], seg_lat[0]), textcoords="offset points",
                                   **text_pos(label_key("map", pnum, "start"), (8, 10)), **font_kw,
                                   fontsize=8.5, fontweight="bold", color="#1b8a3a", zorder=8,
                                   bbox=LABEL_BG)

        ax_m.scatter([seg_lon[-1]], [seg_lat[-1]], marker="o", s=70, facecolor="#c0392b",
                     edgecolor="white", linewidth=1.2, zorder=6)
        ann_end = ax_m.annotate(end_label, (seg_lon[-1], seg_lat[-1]), textcoords="offset points",
                                 **text_pos(label_key("map", pnum, "end"), (8, 10)), **font_kw,
                                 fontsize=8.5, fontweight="bold", color="#c0392b", zorder=8,
                                 bbox=LABEL_BG)

        # ---- POI labels: collision-aware placement -----------------------
        # The old approach cycled through a fixed set of vertical offsets
        # blindly, with no idea whether a label actually landed on top of
        # another label, a marker, or the map edge — POIs bunched close
        # together along the track routinely ended up with overlapping or
        # clipped labels. This instead measures each candidate label's real
        # rendered bounding box (via a live renderer, now that the axes'
        # final aspect-corrected transform is already locked in above) and
        # walks a ring of offsets — alternating left/right of the point,
        # stepping further away vertically — looking for one that clears
        # every marker and every label placed so far. When two+ POIs sit
        # close enough together that no offset avoids overlap entirely
        # (e.g. a cluster of named junctions within a couple hundred
        # metres), it keeps whichever candidate has the least total overlap
        # rather than blindly taking the first one tried.
        mid_x = (xmin + xmax) / 2
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        map_disp_bbox = ax_m.get_window_extent(renderer=renderer)

        marker_r_px = 8  # keep labels clear of marker glyphs, not just each other

        def marker_obstacle(lon, lat):
            dxpx, dypx = ax_m.transData.transform((lon, lat))
            return Bbox.from_extents(dxpx - marker_r_px, dypx - marker_r_px,
                                      dxpx + marker_r_px, dypx + marker_r_px)

        def overlap_area(b1, b2):
            dx = min(b1.x1, b2.x1) - max(b1.x0, b2.x0)
            dy = min(b1.y1, b2.y1) - max(b1.y0, b2.y0)
            return max(dx, 0.0) * max(dy, 0.0)

        obstacle_boxes = [
            marker_obstacle(seg_lon[0], seg_lat[0]),
            marker_obstacle(seg_lon[-1], seg_lat[-1]),
            ann_start.get_window_extent(renderer=renderer),
            ann_end.get_window_extent(renderer=renderer),
        ]
        for (_km, _name, plat, plon, _pele) in page_pois:
            obstacle_boxes.append(marker_obstacle(plon, plat))

        vsteps = [10, 24, 38, 54, 70, -10, -24, -38, -54, -70]
        off_panel_penalty = 1e7  # dwarfs any real overlap area, but finite so "least-bad" logic still orders correctly

        def place_poi_label(label, ha, dx, dy, va="baseline"):
            return ax_m.annotate(
                label, (px, py), xytext=(dx, dy), textcoords="offset points",
                fontsize=8, color="#1a1a2e", ha=ha, va=va, zorder=8, **font_kw,
                bbox=LABEL_BG,
                arrowprops=dict(arrowstyle="-", color="#333333", linewidth=0.5,
                                 connectionstyle="arc3,rad=0.0", shrinkA=0, shrinkB=0),
            )

        # Dragged labels go first, so the automatic search below treats
        # them as fixed obstacles instead of landing on top of them.
        auto_pois = []
        for (km, name, plat, plon, pele) in page_pois:
            px, py = plon, plat
            ax_m.scatter([px], [py], marker="o", s=32, facecolor="#c0392b", edgecolor="white",
                         linewidth=1.0, zorder=7)
            label = f"{name}\n{km:.1f} km - {pele:.0f} m"
            off = offsets.get(label_key("map", pnum, km))
            if off:
                ann = place_poi_label(label, "left", off[0], -off[1], va="top")
                obstacle_boxes.append(ann.get_window_extent(renderer=renderer))
            else:
                auto_pois.append((km, name, plat, plon, pele))

        for (km, name, plat, plon, pele) in auto_pois:
            px, py = plon, plat
            label = f"{name}\n{km:.1f} km - {pele:.0f} m"

            sides = ["right", "left"] if px <= mid_x else ["left", "right"]
            chosen = None
            best = None  # (penalty, ann, box) — least-bad candidate, kept only if nothing is fully clear
            for side in sides:
                dx, ha = (12, "left") if side == "right" else (-12, "right")
                for dy in vsteps:
                    ann = place_poi_label(label, ha, dx, dy)
                    box = ann.get_window_extent(renderer=renderer)
                    fits_panel = (box.x0 >= map_disp_bbox.x0 and box.x1 <= map_disp_bbox.x1 and
                                  box.y0 >= map_disp_bbox.y0 and box.y1 <= map_disp_bbox.y1)
                    penalty = sum(overlap_area(box, ob) for ob in obstacle_boxes)
                    if not fits_panel:
                        penalty += off_panel_penalty
                    if penalty == 0.0:
                        if best is not None:
                            best[1].remove()  # drop the earlier least-bad candidate
                        chosen = (ann, box)
                        break
                    if best is None or penalty < best[0]:
                        if best is not None:
                            best[1].remove()
                        best = (penalty, ann, box)
                    else:
                        ann.remove()
                if chosen:
                    break
            if chosen is None:
                chosen = (best[1], best[2])
            obstacle_boxes.append(chosen[1])

        sb_len = 5 if (xmax - xmin) * km_per_deg_lon < 20 else 10
        sb_len_deg = sb_len / km_per_deg_lon
        sb_x0 = xmax - sb_len_deg - (xmax - xmin) * 0.06
        sb_y0 = ymin + (ymax - ymin) * 0.05
        ax_m.plot([sb_x0, sb_x0 + sb_len_deg], [sb_y0, sb_y0], color="black", linewidth=2.4, zorder=9,
                  path_effects=[pe.Stroke(linewidth=3.6, foreground="white"), pe.Normal()])
        ax_m.plot([sb_x0, sb_x0], [sb_y0 - (ymax - ymin) * 0.01, sb_y0 + (ymax - ymin) * 0.01], color="black", linewidth=2.4, zorder=9)
        ax_m.plot([sb_x0 + sb_len_deg, sb_x0 + sb_len_deg], [sb_y0 - (ymax - ymin) * 0.01, sb_y0 + (ymax - ymin) * 0.01], color="black", linewidth=2.4, zorder=9)
        ax_m.text(sb_x0 + sb_len_deg / 2, sb_y0 + (ymax - ymin) * 0.018, f"{sb_len} km", ha="center", fontsize=8.5,
                  bbox=LABEL_BG, zorder=9)

        nx0, ny0 = xmin + (xmax - xmin) * 0.05, ymax - (ymax - ymin) * 0.12
        ax_m.annotate("N", xy=(nx0, ny0 + (ymax - ymin) * 0.05), xytext=(nx0, ny0),
                      ha="center", fontsize=10.5, fontweight="bold", color="black",
                      bbox=LABEL_BG,
                      arrowprops=dict(arrowstyle="-|>", color="black", linewidth=1.8))

        ax_m.text(0.01, 0.01, ctx["attribution"],
                  transform=ax_m.transAxes, fontsize=6.5, color="white", va="bottom", ha="left",
                  bbox=LABEL_BG_DARK)

        cax = inset_axes(ax_m, width="1.8%", height="28%", loc="upper right",
                          bbox_to_anchor=(-0.012, -0.02, 1, 1), bbox_transform=ax_m.transAxes, borderpad=0)
        cbar = fig.colorbar(lc, cax=cax)
        cbar.set_label("grade (%)", fontsize=7.5, color="white")
        cbar.ax.tick_params(labelsize=6.5, colors="white")
        cbar.outline.set_edgecolor("white")

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
    ax_f = fig.add_subplot(gs[3]); ax_f.axis("off")
    ax_f.patch.set_alpha(0.0)
    ax_f.set_xlim(0, 1); ax_f.set_ylim(0, 1)
    if is_vector:
        ax_f.plot([0, 1], [1.0, 1.0], color="#1a1a2e", linewidth=0.7, transform=ax_f.transAxes, clip_on=False)
        ax_f.text(0, 0.15, f"Roadbook - Sector {sector_code} - Page {pnum} of {n_pages}",
                  fontsize=7.5, color="#666666")
        ax_f.text(1.0, 0.15, f"Generated {date.today().isoformat()} - GPX: {start_name} - {end_name} ({update_tag})",
                  fontsize=7.5, color="#666666", ha="right")

    return fig, map_rect


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
        cmap=cmap, norm=norm, basemap_bounds=basemap_bounds, basemap_imgs=basemap_imgs,
        race_title=args.race_title or data.get("race_title", "Roadbook"),
        pois_all=pois_all, attribution=attribution, style=style,
    )

    slug = data_pkl.stem.replace("_data", "")
    out_dir = Path(args.out_dir) if args.out_dir else WORK_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    raster_pdf = out_dir / f"{slug}_raster.pdf"
    vector_pdf = out_dir / f"{slug}_vector.pdf"

    map_rects = {}
    with PdfPages(raster_pdf) as pdf_r, PdfPages(vector_pdf) as pdf_v:
        for page in range(n_pages):
            fig_r, _ = build_page("raster", page, ctx)
            pdf_r.savefig(fig_r)
            plt.close(fig_r)

            fig_v, map_rect = build_page("vector", page, ctx)
            pdf_v.savefig(fig_v, transparent=True)
            plt.close(fig_v)
            map_rects[page + 1] = map_rect

    map_rects_path = basemap_dir / "map_rects.json"
    with open(map_rects_path, "w") as f:
        json.dump(map_rects, f, indent=2)

    print(f"Rendered {n_pages} pages (grad_vmax={grad_vmax:.1f}%)")
    print(f"  raster -> {raster_pdf}  (low-res preview only)")
    print(f"  vector -> {vector_pdf}")
    print(f"  map rects -> {map_rects_path}  (used by merge_layers.py to place full-res JPGs)")


if __name__ == "__main__":
    main()
