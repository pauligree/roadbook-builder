"""
merge_layers.py — Step 4 of the roadbook pipeline.

Merges the vector-only PDF from render_pages.py with the raw, full-resolution
basemap JPGs from fetch_basemap.py into one PDF with two real PDF Optional
Content Groups (OCGs) — the standard PDF layer mechanism that Illustrator,
Acrobat, and other OCG-aware viewers show as independently toggleable/
editable layers.

The basemap layer is built from page{N}_raw.jpg directly (via
fitz.Page.insert_image), NOT from render_pages.py's raster_pdf — that PDF is
only a low-res matplotlib preview. Embedding the raw JPG directly means the
basemap keeps its full fetched resolution instead of being downsampled to
whatever dpi matplotlib's savefig used. Placement uses
<basemap_dir>/map_rects.json (written by render_pages.py), which records the
exact map-panel rectangle — post aspect-fix and centering — for each page.

Usage:
    python -m roadbook.merge_layers work/tor450_t1_..._vector.pdf work/tor450_t1_..._basemaps output/TOR450_T1_roadbook.pdf
"""
import argparse
import json
from pathlib import Path

import pymupdf as fitz  # PyMuPDF


def _place_sector_pages(out, ocg_raster, ocg_vector, vector_path, basemap_dir):
    """Append one sector's pages to `out`, tagging the raster image and
    vector content of each page with the given (already-created) OCGs.
    Shared by merge() (one sector, its own fresh OCG pair) and
    merge_multi() (many sectors, one OCG pair shared across all of them) —
    the actual page-building logic is identical either way, only where the
    OCGs come from differs.

    NOTE: fitz.Document.insert_pdf() — the obvious way to concatenate
    already-merged single-sector PDFs — silently drops OCG registration:
    it copies the layer objects into the destination file but never adds
    them to the destination's catalog OCProperties, so the result has no
    toggleable layers in Illustrator/Acrobat even though the pages look
    fine on screen. Building every sector's pages straight from its raw
    vector-PDF + basemap JPGs (the same inputs merge() itself uses) avoids
    that trap entirely.
    """
    vector_doc = fitz.open(vector_path)
    basemap_dir = Path(basemap_dir)

    with open(basemap_dir / "map_rects.json") as f:
        map_rects = json.load(f)  # {"1": {"x0":.., "y0":.., "x1":.., "y1":..}, ...} (figure fractions)

    for i in range(len(vector_doc)):
        pnum = i + 1
        vrect = vector_doc[i].rect
        page = out.new_page(width=vrect.width, height=vrect.height)

        frac = map_rects[str(pnum)]
        # matplotlib figure fractions have a bottom-left origin (y up);
        # fitz page rects have a top-left origin (y down) — flip y here.
        img_rect = fitz.Rect(
            frac["x0"] * vrect.width,
            (1 - frac["y1"]) * vrect.height,
            frac["x1"] * vrect.width,
            (1 - frac["y0"]) * vrect.height,
        )
        jpg_path = basemap_dir / f"page{pnum}_raw.jpg"
        # insert_image() defaults to keep_proportion=True: if the fetched
        # mosaic's pixel aspect ratio doesn't exactly match img_rect's
        # (usually a tiny rounding difference, since both ultimately come
        # from the same tile geometry), it centers the image with the gap
        # absorbed on one axis rather than stretching it — consistent with
        # the true-aspect fix in render_pages.py, not fighting it.
        page.insert_image(img_rect, filename=str(jpg_path), oc=ocg_raster)

        page.show_pdf_page(vrect, vector_doc, i, oc=ocg_vector)

    vector_doc.close()


def merge(vector_path, basemap_dir, out_path,
          raster_layer_name="Basemap (raster)",
          vector_layer_name="Track, labels & text (vector)"):
    out = fitz.open()
    ocg_raster = out.add_ocg(raster_layer_name, on=True)
    ocg_vector = out.add_ocg(vector_layer_name, on=True)

    _place_sector_pages(out, ocg_raster, ocg_vector, vector_path, basemap_dir)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path, garbage=4, deflate=True)
    return out


def merge_multi(sectors, out_path,
                 raster_layer_name="Basemap (raster)",
                 vector_layer_name="Track, labels & text (vector)"):
    """Build one PDF spanning every sector in `sectors` — a list of
    (vector_pdf_path, basemap_dir) pairs, in output page order — sharing
    ONE raster/vector OCG pair across the whole document. That means a
    single Layers-panel toggle hides every sector's basemap (or every
    sector's track+labels) at once, rather than needing to flip N separate
    pairs. Used by run_pipeline.py's --merge flag."""
    out = fitz.open()
    ocg_raster = out.add_ocg(raster_layer_name, on=True)
    ocg_vector = out.add_ocg(vector_layer_name, on=True)

    for vector_path, basemap_dir in sectors:
        _place_sector_pages(out, ocg_raster, ocg_vector, vector_path, basemap_dir)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path, garbage=4, deflate=True)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vector_pdf")
    ap.add_argument("basemap_dir", help="Output folder of fetch_basemap.py (contains pageN_raw.jpg + map_rects.json)")
    ap.add_argument("out_pdf")
    args = ap.parse_args()

    out = merge(args.vector_pdf, args.basemap_dir, args.out_pdf)
    print(f"Merged {len(out)} pages -> {args.out_pdf}")
    print("Layers:", [v["name"] for v in out.get_ocgs().values()])


if __name__ == "__main__":
    main()
