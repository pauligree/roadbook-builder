// Roadbook Builder — web mockup frontend. No build step, no dependencies.
// Talks to webapp/server.py; the schematic preview is drawn entirely from
// /api/analyze (no tiles), mirroring render_pages.py's layout geometry.

const $ = (id) => document.getElementById(id);
const canvas = $("canvas");
const ctx = canvas.getContext("2d");

const state = {
  files: [],
  outputs: [],
  file: null,        // relative path of the selected GPX
  data: null,        // last /api/analyze result
  view: "overview",  // overview | page | final
  page: 0,
  hoverPage: -1,
  job: null,
};

// ---- options ------------------------------------------------------------
const form = $("opts");
const STORE_KEY = "roadbook.options";

function readOptions() {
  const f = new FormData(form);
  const o = Object.fromEntries(f.entries());
  o.pages = +o.pages; o.zoom = +o.zoom; o.waypoint_radius_m = +o.waypoint_radius_m;
  return o;
}

function restoreOptions() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    for (const [k, v] of Object.entries(saved)) if (form.elements[k] && k !== "api_key") form.elements[k].value = v;
  } catch (_) { /* storage unavailable: defaults are fine */ }
}

function saveOptions() {
  try {
    const { api_key, ...rest } = readOptions();
    localStorage.setItem(STORE_KEY, JSON.stringify(rest));
  } catch (_) {}
}

function syncOptionLabels() {
  const o = readOptions();
  $("pagesOut").textContent = o.pages;
  $("zoomOut").textContent = o.zoom;
  $("radiusOut").textContent = o.waypoint_radius_m;
  const prov = state.providers?.find((p) => p.id === o.provider);
  $("keyFields").hidden = !prov?.needs_key;
  if (prov?.needs_key) form.elements.style_id.placeholder = prov.default_style || "";
}

// ---- API ----------------------------------------------------------------
async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

let analyzeSeq = 0;
let analyzeTimer = null;
function scheduleAnalyze(delay = 150) {
  clearTimeout(analyzeTimer);
  analyzeTimer = setTimeout(runAnalyze, delay);
}

async function runAnalyze() {
  if (!state.file) return;
  const seq = ++analyzeSeq;
  try {
    const data = await api("/api/analyze", { file: state.file, options: readOptions() });
    if (seq !== analyzeSeq) return; // stale response
    state.data = data;
    state.page = Math.min(state.page, data.n_pages - 1);
    $("renderBtn").disabled = !!state.job;
    updateNotes();
    renderStrip();
    renderSummary();
    draw();
  } catch (e) {
    if (seq === analyzeSeq) showError(e.message);
  }
}

function showError(msg) {
  state.data = null;
  $("summary").textContent = "";
  $("empty").textContent = "Error: " + msg;
  $("empty").hidden = false;
  draw();
}

// ---- left pane: files ---------------------------------------------------
async function loadFiles() {
  state.files = await api("/api/files");
  const list = $("fileList");
  list.innerHTML = "";
  let folder = null;
  for (const f of state.files) {
    if (f.folder !== folder) {
      folder = f.folder;
      const h = document.createElement("div");
      h.className = "folder"; h.textContent = folder + "/";
      list.appendChild(h);
    }
    const b = document.createElement("button");
    b.className = "file" + (f.path === state.file ? " active" : "");
    b.textContent = f.name.replace(/\.gpx$/i, "");
    b.title = f.name;
    b.onclick = () => selectFile(f.path);
    list.appendChild(b);
  }
}

function selectFile(path) {
  state.file = path; state.page = 0; state.data = null;
  for (const b of document.querySelectorAll(".file")) b.classList.toggle("active", b.title === path.split("/").pop());
  $("empty").hidden = true;
  if (state.view === "final") showFinal();
  runAnalyze();
}

async function uploadFiles(fileList) {
  let last = null;
  for (const file of fileList) {
    const fd = new FormData(); fd.append("file", file);
    const res = await fetch("/api/upload", { method: "POST", body: fd });
    if (res.ok) last = (await res.json()).path;
  }
  await loadFiles();
  if (last) selectFile(last);
}

// ---- notes / summary / strip -------------------------------------------
const ZOOM_FEEL = {
  10: "very coarse overview", 11: "coarse overview", 12: "overview, large labels",
  13: "decluttered, biggest native labels", 14: "more trail/contour detail",
  15: "noticeably denser labels", 16: "close-up style, small packed labels", 17: "close-up style, tiny labels",
};

function updateNotes() {
  const d = state.data; if (!d) return;
  const o = readOptions();
  const pn = $("pagesNote");
  if (d.pages_forced_single) {
    pn.textContent = "Filename contains “backup route” → always 1 page.";
    pn.className = "note warn";
  } else {
    pn.textContent = `≈ ${(d.stats.total_km / d.n_pages).toFixed(1)} km per page`;
    pn.className = "note";
  }
  const pg = d.pages[state.page];
  const ppi = pg ? Math.round(mosaicPx(pg.bounds, o.zoom, d.tile_px).w / mapBox(pg).w) : 0;
  const zn = $("zoomNote");
  zn.textContent = `${ZOOM_FEEL[o.zoom] || ""} · ${d.tiles_total} tiles · ≈ ${ppi} ppi printed`;
  zn.className = "note" + (d.tiles_total > 600 ? " warn" : "");
}

function renderSummary() {
  const d = state.data;
  const s = d.stats;
  $("summary").textContent =
    `${d.meta.race_title} · ${s.total_km.toFixed(1)} km · D+ ${Math.round(s.gain)} m · ` +
    `D- ${Math.round(s.loss)} m · ${d.pois.length} waypoints · ${d.n_pages} page${d.n_pages > 1 ? "s" : ""}`;
}

function renderStrip() {
  const strip = $("strip");
  strip.innerHTML = "";
  const d = state.data; if (!d) return;
  d.pages.forEach((p, i) => {
    const c = document.createElement("button");
    c.className = "chip" + (i === state.page ? " active" : "");
    c.innerHTML = `<b>Part ${p.page}</b><span>${p.d0.toFixed(1)}–${p.d1.toFixed(1)} km · +${Math.round(p.gain)} m</span>`;
    c.onclick = () => { setPage(i); if (state.view === "overview") setView("page"); };
    c.onmouseenter = () => { state.hoverPage = i; if (state.view === "overview") draw(); };
    c.onmouseleave = () => { state.hoverPage = -1; if (state.view === "overview") draw(); };
    strip.appendChild(c);
  });
  $("pageLabel").textContent = `${state.page + 1} / ${d.n_pages}`;
}

function setPage(i) {
  const d = state.data; if (!d) return;
  state.page = Math.max(0, Math.min(d.n_pages - 1, i));
  renderStrip(); updateNotes(); draw();
}

function setView(v) {
  state.view = v;
  for (const t of document.querySelectorAll(".tab")) t.classList.toggle("active", t.dataset.view === v);
  const final = v === "final";
  $("pdfFrame").hidden = !final;
  canvas.hidden = final;
  $("pageNav").style.visibility = v === "page" ? "visible" : "hidden";
  if (final) showFinal(); else { $("empty").hidden = !!state.file; draw(); }
}

// ---- geometry helpers ---------------------------------------------------
const COLORS = () => {
  const cs = getComputedStyle(document.documentElement);
  return { ink: cs.getPropertyValue("--ink").trim(), muted: cs.getPropertyValue("--muted").trim(),
           line: cs.getPropertyValue("--line").trim(), accent: cs.getPropertyValue("--accent").trim() };
};

function gradeColor(g) {
  const d = state.data, vmax = d.grade_vmax, stops = d.grade_colors;
  let t = Math.max(0, Math.min(1, (g + vmax) / (2 * vmax))) * (stops.length - 1);
  const i = Math.min(Math.floor(t), stops.length - 2); t -= i;
  const c = stops[i].map((v, k) => Math.round(255 * (v + (stops[i + 1][k] - v) * t)));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

function mosaicPx(b, zoom, tilePx) {
  // tile-grid pixel size of the fetched mosaic (Web Mercator), as fetch_basemap builds it
  const n = 2 ** zoom;
  const my = (lat) => (1 - Math.log(Math.tan(lat * Math.PI / 180) + 1 / Math.cos(lat * Math.PI / 180)) / Math.PI) / 2 * n;
  return { w: (b.east - b.west) / 360 * n * tilePx, h: (my(b.south) - my(b.north)) * tilePx };
}

// GridSpec rows in figure fractions, same maths as matplotlib's GridSpec
// (top=0.985, bottom=0.012, left=0.025, right=0.975, hspace=0.10 in render_pages.py)
function gridRows() {
  const r = state.data.layout.height_ratios;
  const top = 0.985, bottom = 0.012, hspace = 0.10, n = r.length;
  const cell = (top - bottom) / (n + hspace * (n - 1));
  const sum = r.reduce((a, b) => a + b, 0);
  let y = top;
  return r.map((ri) => {
    const h = cell * n * ri / sum;
    const row = { x0: 0.025, x1: 0.975, y1: y, y0: y - h };
    y -= h + hspace * cell;
    return row;
  });
}

// Map axes box in inches after set_aspect(1/cos(lat0), adjustable="box", anchor="C")
function mapBox(pg) {
  const [PW, PH] = state.data.layout.page_size_in;
  const row = gridRows()[1];
  const b = pg.bounds;
  const lat0 = (b.south + b.north) / 2;
  const ratio = (b.north - b.south) / Math.cos(lat0 * Math.PI / 180) / (b.east - b.west);
  const cw = (row.x1 - row.x0) * PW, ch = (row.y1 - row.y0) * PH;
  let w = cw, h = cw * ratio;
  if (h > ch) { h = ch; w = ch / ratio; }
  return { w, h, x: row.x0 * PW + (cw - w) / 2, y: (1 - row.y1) * PH + (ch - h) / 2 }; // top-left origin, inches
}

function pageTrack(pg) {
  const t = state.data.track, idx = [];
  for (let i = 0; i < t.km.length; i++) if (t.km[i] >= pg.d0 - 0.1 && t.km[i] <= pg.d1 + 0.1) idx.push(i);
  return idx;
}

function pagePois(pg) {
  // same merge rule as render_pages.build_page: POIs < 0.3 km apart share one label
  const out = [];
  for (const p of state.data.pois.filter((p) => p.km >= pg.d0 - 0.05 && p.km <= pg.d1 + 0.05)) {
    const prev = out[out.length - 1];
    if (prev && Math.abs(p.km - prev.km) < 0.3) {
      out[out.length - 1] = { ...p, km: (prev.km + p.km) / 2, name: prev.name + " / " + p.name, ele: (prev.ele + p.ele) / 2 };
    } else out.push(p);
  }
  return out;
}

function drawGradeLine(pts, idx, width) {
  const t = state.data.track;
  ctx.lineCap = "round"; ctx.lineJoin = "round";
  ctx.strokeStyle = "#1a1a1a"; ctx.lineWidth = width + 1.6;
  ctx.beginPath();
  idx.forEach((i, k) => { const [x, y] = pts(i); k ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.stroke();
  ctx.lineWidth = width;
  for (let k = 1; k < idx.length; k++) {
    const [x0, y0] = pts(idx[k - 1]), [x1, y1] = pts(idx[k]);
    ctx.strokeStyle = gradeColor(t.grade[idx[k - 1]]);
    ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
  }
}

function marker(x, y, r, fill) {
  ctx.beginPath(); ctx.arc(x, y, r, 0, 2 * Math.PI);
  ctx.fillStyle = fill; ctx.fill();
  ctx.lineWidth = Math.max(1, r * 0.3); ctx.strokeStyle = "#fff"; ctx.stroke();
}

function label(text, x, y, { size = 11, color = "#1a1a2e", bold = false, align = "left", bg = "rgba(255,255,255,.78)", mono = false } = {}) {
  ctx.font = `${bold ? "700 " : ""}${size}px ${mono ? "ui-monospace, Menlo, monospace" : "-apple-system, Helvetica, sans-serif"}`;
  const lines = String(text).split("\n");
  const w = Math.max(...lines.map((l) => ctx.measureText(l).width));
  const lh = size * 1.18, h = lh * lines.length;
  const x0 = align === "left" ? x : align === "right" ? x - w : x - w / 2;
  if (bg) { ctx.fillStyle = bg; ctx.fillRect(x0 - 2, y - 1, w + 4, h + 2); }
  ctx.fillStyle = color; ctx.textBaseline = "top"; ctx.textAlign = "left";
  lines.forEach((l, i) => ctx.fillText(l, x0, y + i * lh));
  return { x0: x0 - 2, y0: y - 1, x1: x0 + w + 2, y1: y + h + 1 };
}

const overlaps = (a, b) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;

// ---- drawing ------------------------------------------------------------
function resizeCanvas() {
  const r = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(r.width * dpr));
  canvas.height = Math.max(1, Math.round(r.height * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { W: r.width, H: r.height };
}

let overviewRects = []; // page rects in canvas px, for hover/click hit-testing

function draw() {
  if (state.view === "final") return;
  const { W, H } = resizeCanvas();
  ctx.clearRect(0, 0, W, H);
  if (!state.data) return;
  if (state.view === "overview") drawOverview(W, H); else drawPage(W, H);
}

function drawOverview(W, H) {
  const d = state.data, col = COLORS(), t = d.track;
  const pad = 28, profH = Math.min(150, H * 0.24);
  const mapH = H - profH - pad * 1.5;

  // extent: union of all tile-snapped page boxes
  let west = 180, east = -180, south = 90, north = -90;
  for (const p of d.pages) {
    west = Math.min(west, p.bounds.west); east = Math.max(east, p.bounds.east);
    south = Math.min(south, p.bounds.south); north = Math.max(north, p.bounds.north);
  }
  const k = Math.cos((south + north) / 2 * Math.PI / 180);
  const sx = (W - 2 * pad) / ((east - west) * k), sy = (mapH - pad) / (north - south);
  const s = Math.min(sx, sy);
  const ox = (W - (east - west) * k * s) / 2, oy = pad + (mapH - pad - (north - south) * s) / 2;
  const P = (lon, lat) => [ox + (lon - west) * k * s, oy + (north - lat) * s];

  // page boxes
  overviewRects = d.pages.map((p, i) => {
    const [x0, y0] = P(p.bounds.west, p.bounds.north), [x1, y1] = P(p.bounds.east, p.bounds.south);
    return { i, x0, y0, x1, y1 };
  });
  const hue = (i) => `hsl(${(i * 67) % 360} 70% 55%`;
  for (const r of overviewRects) {
    const hot = r.i === state.hoverPage;
    ctx.fillStyle = hue(r.i) + (hot ? " / .22)" : " / .08)");
    ctx.fillRect(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0);
    ctx.strokeStyle = hue(r.i) + " / .9)"; ctx.lineWidth = hot ? 2.5 : 1.2;
    ctx.setLineDash(hot ? [] : [5, 4]);
    ctx.strokeRect(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0);
    ctx.setLineDash([]);
    label(`${r.i + 1}`, r.x0 + 5, r.y0 + 4, { size: 13, bold: true, color: "#fff", bg: hue(r.i) + " / .9)" });
  }

  const all = t.km.map((_, i) => i);
  drawGradeLine((i) => P(t.lon[i], t.lat[i]), all, 2.4);
  for (const p of d.pois) { const [x, y] = P(p.lon, p.lat); marker(x, y, 3, "#c0392b"); }
  const n = t.km.length - 1;
  marker(...P(t.lon[0], t.lat[0]), 6, "#1b8a3a");
  marker(...P(t.lon[n], t.lat[n]), 6, "#c0392b");

  // profile strip with page split
  const px0 = pad + 34, px1 = W - pad, py0 = H - profH - pad * 0.5, py1 = H - pad * 0.9;
  const emin = d.stats.min_ele, emax = d.stats.max_ele, tot = d.stats.total_km;
  const X = (km) => px0 + (km / tot) * (px1 - px0);
  const Y = (e) => py1 - ((e - emin) / Math.max(1, emax - emin)) * (py1 - py0 - 14);
  d.pages.forEach((p, i) => {
    ctx.fillStyle = hue(i) + (i === state.hoverPage ? " / .25)" : " / .1)");
    ctx.fillRect(X(p.d0), py0, X(p.d1) - X(p.d0), py1 - py0);
    label(`${i + 1}`, (X(p.d0) + X(p.d1)) / 2, py0 + 2, { size: 11, bold: true, color: col.muted, bg: null, align: "center" });
  });
  drawGradeLine((i) => [X(t.km[i]), Y(t.ele[i])], all, 1.4);
  ctx.fillStyle = col.muted; ctx.font = "11px -apple-system, sans-serif"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
  ctx.fillText(`${Math.round(emax)} m`, px0 - 4, Y(emax)); ctx.fillText(`${Math.round(emin)} m`, px0 - 4, Y(emin));
  ctx.textAlign = "left"; ctx.textBaseline = "top";
  ctx.fillText(`${tot.toFixed(1)} km`, px1 - 40, py1 + 3);
}

function drawPage(W, H) {
  const d = state.data, pg = d.pages[state.page], o = readOptions();
  const [PW, PH] = d.layout.page_size_in;
  const s = Math.min((W - 40) / PW, (H - 40) / PH);          // px per inch
  const ox = (W - PW * s) / 2, oy = (H - PH * s) / 2;
  const pt = (v) => v / 72 * s;                              // font points -> px
  const FX = (fx) => ox + fx * PW * s, FY = (fy) => oy + (1 - fy) * PH * s;

  // paper
  ctx.fillStyle = "rgba(0,0,0,.18)"; ctx.fillRect(ox + 3, oy + 4, PW * s, PH * s);
  ctx.fillStyle = "#fff"; ctx.fillRect(ox, oy, PW * s, PH * s);

  const rows = gridRows();
  const cellY = (row, f) => FY(row.y0 + f * (row.y1 - row.y0));   // axes-fraction y -> px

  // header
  const hr = rows[0], hx = FX(hr.x0), hx1 = FX(hr.x1);
  const m = d.meta;
  label(m.race_title, hx, cellY(hr, 1.0), { size: pt(13), bold: true, bg: null });
  label(`Sector ${m.sector_code}: ${m.start_name} -> ${m.end_name}`, hx, cellY(hr, 0.40), { size: pt(10.5), bold: true, color: "#16213e", bg: null });
  label(`Part ${pg.page}/${d.n_pages} (${pg.d0.toFixed(1)}-${pg.d1.toFixed(1)} km)  |  Distance ${pg.length_km.toFixed(1)} km  |  ` +
        `D+ ${Math.round(pg.gain)} m  |  D- ${Math.round(pg.loss)} m  |  Elev min/max ${Math.round(pg.min_ele)}/${Math.round(pg.max_ele)} m`,
        hx, cellY(hr, -0.05), { size: pt(8), color: "#333", bg: null, mono: true });
  if (m.version || m.update_tag) label(`Rev. ${m.version} - ${m.update_tag}`, hx1, cellY(hr, 1.0), { size: pt(7.5), color: "#888", bg: null, align: "right" });
  ctx.strokeStyle = "#1a1a2e"; ctx.lineWidth = pt(1.2);
  ctx.beginPath(); ctx.moveTo(hx, cellY(hr, -0.28)); ctx.lineTo(hx1, cellY(hr, -0.28)); ctx.stroke();

  // map panel
  const mb = mapBox(pg);
  const mx = ox + mb.x * s, my = oy + mb.y * s, mw = mb.w * s, mh = mb.h * s;
  const b = pg.bounds;
  const MP = (lon, lat) => [mx + (lon - b.west) / (b.east - b.west) * mw, my + (b.north - lat) / (b.north - b.south) * mh];

  ctx.save();
  ctx.beginPath(); ctx.rect(mx, my, mw, mh); ctx.clip();
  ctx.fillStyle = "#eef1ea"; ctx.fillRect(mx, my, mw, mh);
  ctx.strokeStyle = "rgba(120,140,110,.18)"; ctx.lineWidth = 1;          // "basemap" hatch
  for (let x = -mh; x < mw; x += 14) { ctx.beginPath(); ctx.moveTo(mx + x, my + mh); ctx.lineTo(mx + x + mh, my); ctx.stroke(); }
  const mp = mosaicPx(b, o.zoom, d.tile_px);
  label(`BASEMAP · ${o.provider} z${o.zoom} · ${b.tiles} tiles · ${Math.round(mp.w)}×${Math.round(mp.h)} px`,
        mx + mw / 2, my + 6, { size: 11, color: "#7a8a70", bg: "rgba(238,241,234,.9)", align: "center", bold: true });

  // 2/5 km grid, as render_pages
  const lat0 = (b.south + b.north) / 2;
  const kmLon = 111.32 * Math.cos(lat0 * Math.PI / 180), kmLat = 111.32;
  const step = (b.east - b.west) * kmLon < 15 ? 2 : 5;
  ctx.strokeStyle = "rgba(255,255,255,.9)"; ctx.lineWidth = pt(0.5);
  for (let gx = Math.floor(b.west / (step / kmLon)) * step / kmLon; gx < b.east; gx += step / kmLon) {
    const [x] = MP(gx, b.north); ctx.beginPath(); ctx.moveTo(x, my); ctx.lineTo(x, my + mh); ctx.stroke();
  }
  for (let gy = Math.floor(b.south / (step / kmLat)) * step / kmLat; gy < b.north; gy += step / kmLat) {
    const [, y] = MP(b.west, gy); ctx.beginPath(); ctx.moveTo(mx, y); ctx.lineTo(mx + mw, y); ctx.stroke();
  }

  const t = d.track, idx = pageTrack(pg);
  drawGradeLine((i) => MP(t.lon[i], t.lat[i]), idx, pt(1.4));

  // markers + labels (greedy collision avoidance, like render_pages)
  const placed = [];
  const first = idx[0], last = idx[idx.length - 1];
  const isStart = state.page === 0, isEnd = state.page === d.n_pages - 1;
  const [sx0, sy0] = MP(t.lon[first], t.lat[first]), [ex0, ey0] = MP(t.lon[last], t.lat[last]);
  marker(sx0, sy0, pt(4.5), "#1b8a3a"); marker(ex0, ey0, pt(4.5), "#c0392b");
  placed.push(label(isStart ? "START" : `${pg.d0.toFixed(1)} km\n(from part ${pg.page - 1})`, sx0 + pt(8), sy0 - pt(18), { size: pt(8.5), bold: true, color: "#1b8a3a" }));
  placed.push(label(isEnd ? "FINISH" : `${pg.d1.toFixed(1)} km\n(continues part ${pg.page + 1})`, ex0 + pt(8), ey0 - pt(18), { size: pt(8.5), bold: true, color: "#c0392b" }));

  const pois = pagePois(pg);
  const midX = mx + mw / 2;
  for (const p of pois) { const [x, y] = MP(p.lon, p.lat); marker(x, y, pt(3.2), "#c0392b"); }
  for (const p of pois) {
    const [x, y] = MP(p.lon, p.lat);
    const text = `${p.name}\n${p.km.toFixed(1)} km - ${Math.round(p.ele)} m`;
    const size = pt(8);
    ctx.font = `${size}px -apple-system, Helvetica, sans-serif`;
    const w = Math.max(...text.split("\n").map((l) => ctx.measureText(l).width)) + 4, h = size * 2.36 + 2;
    let best = null;
    for (const side of x <= midX ? [1, -1] : [-1, 1]) {
      for (const dy of [10, 24, 38, 54, 70, -10, -24, -38, -54, -70]) {
        const lx = side > 0 ? x + pt(12) : x - pt(12) - w, ly = y - pt(dy) - h / 2;
        const r = { x0: lx, y0: ly, x1: lx + w, y1: ly + h };
        const off = r.x0 < mx || r.x1 > mx + mw || r.y0 < my || r.y1 > my + mh;
        const hits = placed.filter((q) => overlaps(q, r)).length + (off ? 100 : 0);
        if (!best || hits < best.hits) best = { r, hits, side };
        if (hits === 0) break;
      }
      if (best.hits === 0) break;
    }
    const r = best.r;
    ctx.strokeStyle = "#333"; ctx.lineWidth = pt(0.5);
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(best.side > 0 ? r.x0 : r.x1, (r.y0 + r.y1) / 2); ctx.stroke();
    placed.push(label(text, r.x0 + 2, r.y0 + 1, { size }));
  }

  // scale bar, north arrow, colorbar, attribution
  const sbKm = (b.east - b.west) * kmLon < 20 ? 5 : 10;
  const sbW = sbKm / kmLon / (b.east - b.west) * mw;
  const sbx = mx + mw * 0.94 - sbW, sby = my + mh * 0.95;
  ctx.strokeStyle = "#fff"; ctx.lineWidth = pt(3.6); ctx.beginPath(); ctx.moveTo(sbx, sby); ctx.lineTo(sbx + sbW, sby); ctx.stroke();
  ctx.strokeStyle = "#000"; ctx.lineWidth = pt(2.4); ctx.beginPath(); ctx.moveTo(sbx, sby); ctx.lineTo(sbx + sbW, sby); ctx.stroke();
  label(`${sbKm} km`, sbx + sbW / 2, sby - pt(16), { size: pt(8.5), align: "center" });
  const nx = mx + mw * 0.05, ny = my + mh * 0.07;
  ctx.fillStyle = "#000"; ctx.beginPath(); ctx.moveTo(nx, ny - pt(14)); ctx.lineTo(nx - pt(4), ny - pt(4)); ctx.lineTo(nx + pt(4), ny - pt(4)); ctx.fill();
  label("N", nx, ny, { size: pt(10.5), bold: true, align: "center" });
  const cbx = mx + mw * (1 - 0.012 - 0.018), cby = my + mh * 0.02, cbh = mh * 0.28;
  const grad = ctx.createLinearGradient(0, cby + cbh, 0, cby);
  d.grade_colors.forEach((c, i) => grad.addColorStop(i / (d.grade_colors.length - 1), `rgb(${c.map((v) => v * 255).join(",")})`));
  ctx.fillStyle = grad; ctx.fillRect(cbx, cby, mw * 0.018, cbh);
  ctx.strokeStyle = "#fff"; ctx.lineWidth = 1; ctx.strokeRect(cbx, cby, mw * 0.018, cbh);
  label(`+${d.grade_vmax.toFixed(0)}%`, cbx - 3, cby, { size: pt(6.5), align: "right", bg: "rgba(0,0,0,.5)", color: "#fff" });
  label(`-${d.grade_vmax.toFixed(0)}%`, cbx - 3, cby + cbh - pt(8), { size: pt(6.5), align: "right", bg: "rgba(0,0,0,.5)", color: "#fff" });
  label(d.attribution, mx + mw * 0.01, my + mh * 0.99 - pt(9), { size: pt(6.5), color: "#fff", bg: "rgba(0,0,0,.55)" });
  ctx.restore();
  ctx.strokeStyle = "#1a1a2e"; ctx.lineWidth = pt(1); ctx.strokeRect(mx, my, mw, mh);

  // elevation profile (render_pages shifts it right by 0.045 and narrows by 0.055)
  const er = rows[2];
  const ex = FX(er.x0 + 0.045), ew = (er.x1 - er.x0 - 0.055) * PW * s;
  const ey = FY(er.y1), eh = (er.y1 - er.y0) * PH * s;
  const ylo = Math.min(pg.min_ele - 100, 900), yhi = pg.max_ele + 340;
  const EX = (km) => ex + (km - pg.d0) / (pg.d1 - pg.d0) * ew, EY = (e) => ey + eh - (e - ylo) / (yhi - ylo) * eh;
  ctx.strokeStyle = "#333"; ctx.lineWidth = 0.8;
  ctx.beginPath(); ctx.moveTo(ex, ey); ctx.lineTo(ex, ey + eh); ctx.lineTo(ex + ew, ey + eh); ctx.stroke();
  ctx.save(); ctx.beginPath(); ctx.rect(ex, ey - 20, ew, eh + 20); ctx.clip();
  for (const p of pois) {
    ctx.strokeStyle = "#ddd"; ctx.lineWidth = pt(0.4);
    ctx.beginPath(); ctx.moveTo(EX(p.km), ey); ctx.lineTo(EX(p.km), ey + eh); ctx.stroke();
  }
  drawGradeLine((i) => [EX(t.km[i]), EY(t.ele[i])], idx, pt(0.9));
  for (const p of pois) {
    const short = p.name.length > 22 ? p.name.slice(0, 20) + "..." : p.name;
    label(short, EX(p.km), EY(p.ele) - pt(14), { size: pt(6.5), align: "center" });
    marker(EX(p.km), EY(p.ele), pt(2), "#2c3e50");
  }
  ctx.restore();
  ctx.fillStyle = "#333"; ctx.font = `${pt(7)}px -apple-system, sans-serif`; ctx.textBaseline = "middle"; ctx.textAlign = "right";
  for (const e of [ylo, (ylo + yhi) / 2, yhi]) ctx.fillText(`${Math.round(e / 100) * 100}`, ex - 4, EY(e));
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  ctx.fillText(pg.d0.toFixed(1), ex, ey + eh + 3); ctx.fillText(`${pg.d1.toFixed(1)} km`, ex + ew, ey + eh + 3);

  // footer
  const fr = rows[3];
  ctx.strokeStyle = "#1a1a2e"; ctx.lineWidth = pt(0.7);
  ctx.beginPath(); ctx.moveTo(FX(fr.x0), FY(fr.y1)); ctx.lineTo(FX(fr.x1), FY(fr.y1)); ctx.stroke();
  label(`Roadbook - Sector ${m.sector_code} - Page ${pg.page} of ${d.n_pages}`, FX(fr.x0), cellY(fr, 0.85), { size: pt(7.5), color: "#666", bg: null });
  label(`Generated ${new Date().toISOString().slice(0, 10)} - GPX: ${m.start_name} - ${m.end_name} (${m.update_tag})`,
        FX(fr.x1), cellY(fr, 0.85), { size: pt(7.5), color: "#666", bg: null, align: "right" });
}

// ---- final PDF + render jobs ------------------------------------------
async function loadOutputs() {
  state.outputs = await api("/api/output");
  const list = $("outputList");
  list.innerHTML = "";
  for (const o of state.outputs.slice(0, 15)) {
    const a = document.createElement("a");
    a.href = `/output/${encodeURIComponent(o.name)}`; a.target = "_blank"; a.textContent = o.name; a.title = o.name;
    list.appendChild(a);
  }
}

function showFinal() {
  const frame = $("pdfFrame"), empty = $("empty");
  const name = state.data ? `${state.data.slug}_roadbook.pdf` : null;
  const hit = name && state.outputs.find((o) => o.name === name);
  if (hit) {
    const src = `/output/${encodeURIComponent(hit.name)}?t=${Math.round(hit.mtime)}`;
    if (frame.getAttribute("src") !== src) frame.src = src;
    frame.hidden = false; empty.hidden = true;
  } else {
    frame.hidden = true; empty.hidden = false;
    empty.textContent = state.file ? "Not rendered yet — press “Render roadbook”." : "Select a GPX track on the left.";
  }
}

async function startRender() {
  if (!state.file) return;
  const box = $("progress");
  box.hidden = false; box.className = "progress";
  $("renderBtn").disabled = true;
  try {
    state.job = await api("/api/render", { file: state.file, options: readOptions() });
    pollJob();
  } catch (e) {
    box.className = "progress error"; $("progressMsg").textContent = e.message;
    $("renderBtn").disabled = false;
  }
}

async function pollJob() {
  const job = state.job = await api(`/api/jobs/${state.job.id}`);
  $("barFill").style.width = `${Math.min(100, (job.step / 4) * 100 - (job.status === "done" ? 0 : 8))}%`;
  $("progressMsg").textContent = `${job.file}: ${job.message}`;
  if (job.status === "done" || job.status === "error") {
    $("progress").className = "progress " + job.status;
    $("renderBtn").disabled = false;
    state.job = null;
    await loadOutputs();
    if (job.status === "done") setView("final");
    return;
  }
  setTimeout(pollJob, 700);
}

// ---- wiring ------------------------------------------------------------
form.addEventListener("input", (e) => {
  syncOptionLabels(); saveOptions();
  // title only changes text; everything else changes page split / bounds
  scheduleAnalyze(e.target.name === "race_title" ? 300 : 120);
});
$("renderBtn").onclick = startRender;
$("prevPage").onclick = () => setPage(state.page - 1);
$("nextPage").onclick = () => setPage(state.page + 1);
for (const t of document.querySelectorAll(".tab")) t.onclick = () => setView(t.dataset.view);
window.addEventListener("resize", draw);
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select")) return;
  if (e.key === "ArrowLeft") setPage(state.page - 1);
  if (e.key === "ArrowRight") setPage(state.page + 1);
});

canvas.addEventListener("mousemove", (e) => {
  if (state.view !== "overview" || !state.data) return;
  const r = canvas.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  const hits = overviewRects.filter((q) => x >= q.x0 && x <= q.x1 && y >= q.y0 && y <= q.y1);
  hits.sort((a, b) => (a.x1 - a.x0) * (a.y1 - a.y0) - (b.x1 - b.x0) * (b.y1 - b.y0));
  const hp = hits.length ? hits[0].i : -1;
  canvas.style.cursor = hp >= 0 ? "pointer" : "default";
  if (hp !== state.hoverPage) { state.hoverPage = hp; draw(); }
});
canvas.addEventListener("click", () => {
  if (state.view === "overview" && state.hoverPage >= 0) { setPage(state.hoverPage); setView("page"); }
});

const drop = $("drop");
$("upload").onchange = (e) => uploadFiles(e.target.files);
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); uploadFiles(e.dataTransfer.files); });

(async function init() {
  state.providers = await api("/api/providers");
  form.elements.provider.innerHTML = state.providers.map((p) => `<option value="${p.id}">${p.id}${p.needs_key ? " (API key)" : ""}</option>`).join("");
  restoreOptions();
  syncOptionLabels();
  setView("overview");
  await Promise.all([loadFiles(), loadOutputs()]);
})();
