# Roadbook Builder — App plan

Goal: turn the CLI pipeline (`roadbook/*.py`) into an app with a simple
frontend, a live schematic preview of the selected options, and better final
rendering. Phase 2 brings it to iPad / iPhone.

_Recorded 2026-10-19. Revised same day: Phase 1 starts as a **web mockup
(Python backend + browser frontend)** instead of a native SwiftUI app._

---

## Stack decision

| Stage | Frontend | Core | Why |
|---|---|---|---|
| **1a — mockup (now)** | Plain HTML/CSS/JS, no build step | Existing Python pipeline behind a FastAPI server (`webapp/`) | Fastest path to a working app; zero rewrite of the core; runs locally at `http://127.0.0.1:8000` |
| 1b — Mac app | Wrap the same web UI (pywebview, or Tauri) **or** SwiftUI if the mockup proves the UX | Python embedded (python-build-standalone / PyInstaller) | Decide after mockup feedback |
| 2 — iPad/iPhone | Same web UI as PWA, or SwiftUI | Python as render **service** (option A) or native Swift port (option B) | See Phase 2 |

The web mockup is not throwaway: the UI ports directly to a pywebview/Tauri
Mac shell and to a PWA on iPad/iPhone, and `roadbook/api.py` becomes the
render service in Phase 2 option A.

---

## Phase 0 — prep

- [ ] `git init` (project has no version control yet)
- [x] Library API `roadbook/api.py`: `RenderOptions`, `analyze()` (JSON, no
      tiles), `render()` with progress callback. CLI keeps working.
- [ ] Configurable cache dir instead of hardcoded `work/`
- [ ] Persistent tile cache keyed `provider/z/x/y`

## Phase 1a — web mockup

- [x] FastAPI server: list/upload GPX, `analyze`, background render jobs with
      progress, serve PDFs
- [x] Single-page UI, three panes: GPX list · schematic preview · options
- [x] Schematic preview (instant, no tiles):
  - **Overview**: whole track coloured by grade, tile-snapped page boxes
    (exactly what the final render will cover), POIs, start/finish
  - **Page**: A4 wireframe using real `HEIGHT_RATIOS`, aspect-correct map
    panel, track segment, POI labels, profile, header/footer text, tile count
- [x] Render button → progress → final PDF in browser + download
- [x] Uploads (drag-drop), page strip, option persistence
- [ ] Presets per race (save/load option sets)
- [ ] Batch render + "merge all" from UI

## Phase 1b — Mac app

- Wrap web UI in native window (pywebview first, cheapest), bundle Python.
- API keys in macOS Keychain. Drag-drop GPX. Signed + notarized DMG.

## Final rendering improvements (Python core, in parallel)

Priority order:

1. **Smart page split** — today equal-distance (`np.linspace` in
   `page_bboxes`). Fit each page's bbox to the map panel aspect (full-bleed,
   no side margins), consistent scale across pages, ~0.5 km overlap, snap
   breaks near waypoints.
2. **Fixed print scale** option (1:25k / 1:50k) + scale label; page count
   derived automatically.
3. **Better label collision avoidance** — more candidate positions, leader
   lines when crowded.
4. **Basemap tuning** — optional desaturate/lighten so track pops, hillshade.
5. **Gradient legend + binned colours** (0–5 / 5–10 / 10–20 / 20 %+),
   readable in print and B&W.
6. **Profile upgrades** — smoothed elevation line, aid stations / cut-offs,
   cumulative D+.
7. **Page formats** — A4/A5/landscape, embedded custom font, cover +
   overview page for the merged race book.
8. **Mobile output** — per-page PNG/WebP + phone-friendly PDF.

## Phase 2 — iPad / iPhone

| Option | How | Pro | Con |
|---|---|---|---|
| **A. Render service** (recommended first) | `roadbook/api.py` behind FastAPI on a home Mac or small VPS; app/PWA uploads GPX + options, receives PDF | No core rewrite, identical output | Needs network to generate (fine — prep work; PDFs view offline) |
| B. Native Swift core | Port parse/tiles/render to Swift (CoreGraphics PDF) | Fully offline, App Store friendly | ~1300 lines rewrite; CoreGraphics has no OCG layer API → layered PDF stays desktop-only |

Mobile extras: iPad Pencil annotation on preview, Files import; iPhone as
viewer (page swipe, GPS position dot on georeferenced page).

## Milestones

1. M0 — library API + `analyze` JSON ✅
2. M1 — web mockup with live schematic preview + render ✅ (first cut)
3. M2 — feedback round on mockup UX; presets, batch, merge
4. M3 — rendering improvements 1–5
5. Gate — Mac shell (pywebview vs SwiftUI) and Phase 2 option A vs B
6. M4 — iPad/iPhone

## Open questions

- Distribution: personal/team only, or public/App Store? (signing, Mapbox /
  MapTiler print terms, whether a render server is acceptable)
- Layered (Illustrator OCG) PDF needed on mobile, or desktop only?
