"""
parse_gpx.py — Step 1 of the roadbook pipeline.

Parses a GPX track, computes cumulative distance / elevation gain-loss /
smoothed gradient, matches nearby <wpt> waypoints onto the track (so a GPX
file that ships POIs for a whole race only keeps the ones relevant to this
track), and tries to extract a human sector title from the filename.

Saves everything needed by the later steps into work/<slug>_data.pkl.
Unlike the original prototype (which cached this in /tmp and lost it between
sessions), this saves inside the project's own work/ folder so it persists.

Usage:
    python -m roadbook.parse_gpx input/mytrack.gpx
    python -m roadbook.parse_gpx input/mytrack.gpx --waypoint-radius-m 80
"""
import argparse
import math
import pickle
import re
from pathlib import Path

import gpxpy
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = PROJECT_ROOT / "work"

EARTH_RADIUS_M = 6371000.0


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def cumulative_distance(lats, lons):
    dist = np.zeros(len(lats))
    for i in range(1, len(lats)):
        dist[i] = dist[i - 1] + haversine(lats[i - 1], lons[i - 1], lats[i], lons[i])
    return dist


def guess_title_from_filename(gpx_path: Path):
    """Best-effort parse of names like:
    'TOR450 Tor des Glaciers T1 V7.0 START - Rif. Sella - UP2025.gpx'
    Falls back gracefully for filenames that don't follow this pattern.
    """
    stem = gpx_path.stem
    parts = [p.strip() for p in stem.split(" - ")]
    m = re.search(r"(T\d+)\s+V([\d.]+)\s+(.*)$", parts[0]) if parts else None
    if m:
        sector_code = m.group(1)
        version = m.group(2)
        start_name = m.group(3).strip()
        race_title = parts[0][: m.start()].strip() or stem
        end_name = parts[1] if len(parts) > 1 else "?"
        update_tag = parts[2] if len(parts) > 2 else ""
        return dict(
            race_title=race_title, sector_code=sector_code, version=version,
            start_name=start_name, end_name=end_name, update_tag=update_tag,
        )
    # Fallback: no recognizable pattern — just use the filename as the title.
    return dict(
        race_title=stem, sector_code="", version="", start_name="Start",
        end_name="Finish", update_tag="",
    )


def parse_gpx(gpx_path: Path, waypoint_radius_m: float = 60.0):
    with open(gpx_path, encoding="utf-8") as f:
        gpx = gpxpy.parse(f)

    if not gpx.tracks or not gpx.tracks[0].segments:
        raise ValueError(f"No track points found in {gpx_path}")

    points = gpx.tracks[0].segments[0].points
    for seg in gpx.tracks[0].segments[1:]:
        points += seg.points
    for trk in gpx.tracks[1:]:
        for seg in trk.segments:
            points += seg.points

    lats = np.array([p.latitude for p in points])
    lons = np.array([p.longitude for p in points])
    eles = np.array([p.elevation if p.elevation is not None else 0.0 for p in points])
    dist = cumulative_distance(lats, lons)

    d_ele = np.diff(eles)
    gain = float(d_ele[d_ele > 0].sum())
    loss = float(-d_ele[d_ele < 0].sum())

    # Match GPX waypoints onto the track (keep only those actually near it —
    # handles GPX files that ship POIs for an entire multi-stage race).
    pois = []
    for w in gpx.waypoints:
        dlat = lats - w.latitude
        dlon = (lons - w.longitude) * math.cos(math.radians(w.latitude))
        idx = int(np.argmin(dlat ** 2 + dlon ** 2))
        d_m = haversine(w.latitude, w.longitude, lats[idx], lons[idx])
        if d_m < waypoint_radius_m:
            wele = w.elevation if w.elevation is not None else eles[idx]
            pois.append((dist[idx] / 1000.0, w.name or "Waypoint", w.latitude, w.longitude, wele))
    pois.sort(key=lambda p: p[0])

    meta = guess_title_from_filename(gpx_path)

    data = dict(
        lats=lats, lons=lons, eles=eles, dist=dist,
        total_dist_km=dist[-1] / 1000.0, gain=gain, loss=loss,
        min_ele=float(eles.min()), max_ele=float(eles.max()),
        pois=pois, source_gpx=str(gpx_path), **meta,
    )
    return data


def slug_for(gpx_path: Path) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", gpx_path.stem).strip("_").lower()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gpx_path", help="Path to the .gpx file")
    ap.add_argument("--waypoint-radius-m", type=float, default=60.0,
                     help="Max distance (m) from the track for a GPX waypoint to be kept (default: 60)")
    ap.add_argument("--out", default=None, help="Output pickle path (default: work/<slug>_data.pkl)")
    args = ap.parse_args()

    gpx_path = Path(args.gpx_path).resolve()
    data = parse_gpx(gpx_path, waypoint_radius_m=args.waypoint_radius_m)

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else WORK_DIR / f"{slug_for(gpx_path)}_data.pkl"
    with open(out_path, "wb") as f:
        pickle.dump(data, f)

    print(f"Parsed: {data['race_title']} — sector {data['sector_code']}: "
          f"{data['start_name']} -> {data['end_name']}")
    print(f"  Distance {data['total_dist_km']:.2f} km | D+ {data['gain']:.0f} m | "
          f"D- {data['loss']:.0f} m | Elev {data['min_ele']:.0f}-{data['max_ele']:.0f} m")
    print(f"  {len(data['pois'])} waypoints matched within {args.waypoint_radius_m:.0f} m of the track")
    print(f"Saved -> {out_path}")


if __name__ == "__main__":
    main()
