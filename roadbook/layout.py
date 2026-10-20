"""
layout.py — A4 page geometry shared by fetch_basemap.py (which sizes each
page's basemap to fill the map panel) and render_pages.py (which lays the
page out). Kept free of matplotlib so fetching doesn't import it.
"""
PAGE_SIZE_IN = (8.27, 11.69)             # A4 portrait
HEIGHT_RATIOS = [0.62, 7.88, 1.3, 0.2]   # header / map / profile / footer
GRID = dict(hspace=0.10, top=0.985, bottom=0.012, left=0.025, right=0.975)


def map_panel_size_in():
    """(width, height) in inches of the GridSpec cell holding the map —
    same arithmetic as matplotlib's GridSpecBase.get_grid_positions."""
    n = len(HEIGHT_RATIOS)
    tot_h = (GRID["top"] - GRID["bottom"]) * PAGE_SIZE_IN[1]
    cell_h = tot_h / (n + GRID["hspace"] * (n - 1))
    h = HEIGHT_RATIOS[1] * cell_h * n / sum(HEIGHT_RATIOS)
    w = (GRID["right"] - GRID["left"]) * PAGE_SIZE_IN[0]
    return w, h


def map_panel_aspect():
    """width / height of the map panel."""
    w, h = map_panel_size_in()
    return w / h
