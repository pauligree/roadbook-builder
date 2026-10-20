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


def body_height_in():
    """Height in inches from the top of the map cell to the bottom of the
    profile cell — the area the waypoint-table page uses for its table."""
    n = len(HEIGHT_RATIOS)
    tot_h = (GRID["top"] - GRID["bottom"]) * PAGE_SIZE_IN[1]
    cell_h = tot_h / (n + GRID["hspace"] * (n - 1))
    return (HEIGHT_RATIOS[1] + HEIGHT_RATIOS[2]) * cell_h * n / sum(HEIGHT_RATIOS) + GRID["hspace"] * cell_h


# waypoint table geometry (inches / points), shared by render_pages and
# the web preview
TABLE_HEAD_IN = 0.42
TABLE_ROW_IN = 0.26          # row height when the list is short
TABLE_FONT_PT = (5.0, 8.5)   # smallest / largest row font


def table_rows_per_page():
    """Most rows one table page holds at the smallest font."""
    min_row_in = TABLE_FONT_PT[0] / 0.55 / 72
    return max(1, int((body_height_in() * 0.99 - TABLE_HEAD_IN) / min_row_in))
