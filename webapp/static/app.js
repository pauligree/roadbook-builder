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
  sheetIdx: 0,       // page view: index into data.sheets (maps, part tables, final table)
  hoverPage: -1,
  job: null,
};

// ---- options ------------------------------------------------------------
const form = $("opts");
const STORE_KEY = "roadbook.options";

// Fields marked data-style only change how things are drawn: they redraw
// the preview locally and travel to the render as options.style, without
// re-running /api/analyze.
const isStyleField = (name) => form.elements[name]?.dataset?.style !== undefined;

function readOptions(file = state.file) {
  const o = {};
  for (const [k, v] of new FormData(form).entries()) if (!isStyleField(k)) o[k] = v;
  o.pages = +form.elements.pages.value;  // read directly: disabled fields skip FormData
  o.zoom = +o.zoom; o.waypoint_radius_m = +o.waypoint_radius_m; o.simplify_m = +o.simplify_m; o.overlap_km = +o.overlap_km;
  o.same_scale = form.elements.same_scale.checked;
  o.print_scale = +o.print_scale || null;
  o.saturation = +o.saturation; o.lighten = +o.lighten; o.hillshade = +o.hillshade;
  o.part_tables = form.elements.part_tables.checked;
  o.final_table = form.elements.final_table.checked;
  // custom split, once this file has one (until then the equal split
  // is analyzed and seeds it, see renderSplitList)
  if (hiddenStore[file]?.length) o.hidden_pois = hiddenStore[file];
  o.race_title = titleStore[file] || "";  // per track, not the form's (shared) value
  if (o.split_mode === "waypoints" && splitStore[file]) {
    o.splits = fileSplits(file);
    if (!o.splits.length) o.pages = 1;  // no page ends chosen = one page
  }
  delete o.split_mode;
  return o;
}

// ---- custom page split, per GPX file -----------------------------------
// {file: [km, ...]} — distances of the waypoints chosen as page ends.
const SPLITS_KEY = "roadbook.splits";
let splitStore = {};
try { splitStore = JSON.parse(localStorage.getItem(SPLITS_KEY) || "{}"); } catch (_) {}
const fileSplits = (file = state.file) => splitStore[file] || [];
function setFileSplits(kms) {
  splitStore[state.file] = [...new Set(kms.map((k) => +k.toFixed(3)))].sort((a, b) => a - b);
  try { localStorage.setItem(SPLITS_KEY, JSON.stringify(splitStore)); } catch (_) {}
}
const splitMode = () => form.elements.split_mode.value;

// ---- hidden waypoints, per GPX file -------------------------------------
// {file: [km, ...]} — the server drops the waypoint nearest each km
// (parse_gpx.hide_pois), so they vanish from map, profile and tables.
const HIDDEN_KEY = "roadbook.hiddenPois";
let hiddenStore = {};
try { hiddenStore = JSON.parse(localStorage.getItem(HIDDEN_KEY) || "{}"); } catch (_) {}
function setHidden(kms) {
  if (kms.length) hiddenStore[state.file] = [...new Set(kms.map((k) => +k.toFixed(3)))].sort((a, b) => a - b);
  else delete hiddenStore[state.file];
  try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(hiddenStore)); } catch (_) {}
}

// ---- race title, per GPX file -------------------------------------------
// {file: title}. A form-wide title stuck to every track selected later
// (one track's title ended up on another's PDF), so it lives here.
const TITLE_KEY = "roadbook.raceTitle";
let titleStore = {};
try { titleStore = JSON.parse(localStorage.getItem(TITLE_KEY) || "{}"); } catch (_) {}
function setTitle(file, title) {
  if (!file) return;
  if (title) titleStore[file] = title; else delete titleStore[file];
  try { localStorage.setItem(TITLE_KEY, JSON.stringify(titleStore)); } catch (_) {}
}
function syncTitleField() {
  const el = form.elements.race_title;
  el.value = titleStore[state.file] || "";
  el.placeholder = state.data && !el.value ? `auto: ${state.data.meta.race_title}` : "auto from filename";
}

function renderPoiList() {
  const d = state.data;
  $("poiField").hidden = !d;
  if (!d) return;
  const hidden = d.hidden_pois || [];  // absent from a server started before this feature
  const all = [...d.pois.map((p) => ({ ...p, on: true })), ...hidden.map((p) => ({ ...p, on: false }))]
    .sort((a, b) => a.km - b.km);
  $("poiList").innerHTML = all.length ? all.map((p) =>
    `<label${p.on ? "" : ' class="off"'}><input type="checkbox" data-km="${p.km}"${p.on ? " checked" : ""}>` +
    `<span class="km">${p.km.toFixed(1)} km</span><span>${escapeHtml(p.name)}</span></label>`).join("")
    : `<small class="note">No waypoints on this track.</small>`;
  const n = hidden.length;
  $("poiNote").textContent = n ? `${n} hidden on this track` : "";
  $("poiShowAll").disabled = !n;
}

$("poiList").addEventListener("change", () => {
  setHidden([...$("poiList").querySelectorAll("input:not(:checked)")].map((c) => +c.dataset.km));
  scheduleAnalyze(60);
});
$("poiShowAll").onclick = () => { setHidden([]); scheduleAnalyze(0); };

// Waypoints a page may end at: all but the very start/finish of the track.
const splitCandidates = (d) => d.pois.filter((p) => p.km > 0.5 && p.km < d.stats.total_km - 0.5);

// First switch to custom on a file: start from the waypoints nearest to the
// current equal-distance page ends, so the split doesn't jump.
function seedSplits(d) {
  if (splitStore[state.file] || !d) return;
  const cands = splitCandidates(d);
  const seeds = d.pages.slice(0, -1).map((pg) =>
    cands.reduce((best, p) => (!best || Math.abs(p.km - pg.d1) < Math.abs(best.km - pg.d1) ? p : best), null));
  setFileSplits(seeds.filter(Boolean).map((p) => p.km));
}

function renderSplitList() {
  const d = state.data, custom = splitMode() === "waypoints";
  const fixedScale = !!form.elements.print_scale.value;
  $("splitField").hidden = !d || (!custom && !fixedScale && +form.elements.pages.value < 2);
  form.elements.pages.disabled = custom || fixedScale;  // page count from the splits / the scale
  form.elements.same_scale.disabled = fixedScale;
  const list = $("splitList");
  list.hidden = !custom || !d;
  if (list.hidden) return;
  if (!splitStore[state.file]) { seedSplits(d); scheduleAnalyze(0); return; }
  const cands = splitCandidates(d);
  if (!cands.length) { list.innerHTML = `<small class="note">No waypoints on this track.</small>`; return; }
  // each saved km ticks only its nearest waypoint (kms shift slightly
  // when the waypoint radius changes)
  const ticked = new Set(fileSplits().map((k) =>
    cands.reduce((best, p) => (Math.abs(p.km - k) < Math.abs(best.km - k) ? p : best))));
  const pageEnding = (km) => d.pages.findIndex((pg) => Math.abs(pg.d1 - km) < 0.01);
  list.innerHTML = cands.map((p) => {
    const on = ticked.has(p);
    const i = on ? pageEnding(p.km) : -1;
    return `<label><input type="checkbox" data-km="${p.km}"${on ? " checked" : ""}>` +
      `<span class="km">${p.km.toFixed(1)} km</span><span>${escapeHtml(p.name)}</span>` +
      `${i >= 0 ? `<span class="pg">end p.${i + 1}</span>` : ""}</label>`;
  }).join("");
}

const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

$("splitList").addEventListener("change", () => {
  setFileSplits([...$("splitList").querySelectorAll("input:checked")].map((c) => +c.dataset.km));
  scheduleAnalyze(60);
});

// Same keys as render_pages.DEFAULT_STYLE
function readStyle(file = state.file) {
  const e = form.elements;
  return {
    label_font: e.label_font.value || null,
    track_width: +e.track_width.value,
    profile_width: +e.profile_width.value,
    profile_smooth_m: +e.profile_smooth_m.value,
    profile_cum_gain: e.profile_cum_gain.checked,
    grade_colors: [e.c_desc.value, e.c_flat.value, e.c_climb.value],
    grade_max: +e.grade_max.value || null,
    grade_bins: e.grade_bins.value ? e.grade_bins.value.split(",").map(Number) : null,
    label_offsets: file ? fileOffsets(file) : {},
  };
}

const COLOR_PRESETS = {
  default: ["Blue → purple → red", ["#0000ff", "#800080", "#ff0000"]],
  traffic: ["Green → yellow → red", ["#1a9850", "#e6c229", "#d73027"]],
  teal: ["Teal → grey → orange", ["#00897b", "#8d8d8d", "#ef6c00"]],
  cool: ["Cyan → navy → magenta", ["#00b4d8", "#1d3557", "#e5007e"]],
  mono: ["Black & white print", ["#b0b0b0", "#606060", "#000000"]],
};

// The whole form as stored (localStorage and race presets): every field
// but the API key, which stays on the server.
function snapshotOptions() {
  // race_title is per track (titleStore), not part of a reusable option set
  const { api_key, race_title, ...rest } = Object.fromEntries(new FormData(form).entries());
  return { ...rest, pages: form.elements.pages.value,  // disabled in custom split: FormData skips it
    part_tables: form.elements.part_tables.checked, final_table: form.elements.final_table.checked,
    same_scale: form.elements.same_scale.checked, profile_cum_gain: form.elements.profile_cum_gain.checked, _v: 2 };
}

function applyOptions(saved) {
  if (!saved._v) delete saved.provider;  // saved before the server-chosen default existed
  for (const [k, v] of Object.entries(saved)) {
    const el = form.elements[k];
    if (!el || k === "api_key" || k === "race_title") continue;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  }
}

function restoreOptions() {
  try {
    applyOptions(JSON.parse(localStorage.getItem(STORE_KEY) || "{}"));
  } catch (_) { /* storage unavailable: defaults are fine */ }
}

function saveOptions() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(snapshotOptions())); } catch (_) {}
}

// ---- race presets (server-side, presets/<name>.json) -------------------
// A preset is a full option set; per-track things (custom splits, moved
// labels) stay with the GPX file, not the preset.
const presetUrl = (name) => `/api/presets/${encodeURIComponent(name)}`;

async function loadPresets(selected = $("presetSel").value) {
  const names = await api("/api/presets");
  const sel = $("presetSel");
  sel.innerHTML = `<option value="">— none —</option>`;
  for (const n of names) sel.add(new Option(n, n));
  sel.value = names.includes(selected) ? selected : "";
  $("presetDel").disabled = !sel.value;
}

function presetNote(msg, warn = false) {
  $("presetNote").textContent = msg; $("presetNote").className = "note" + (warn ? " warn" : "");
}

$("presetSel").onchange = async () => {
  const name = $("presetSel").value;
  $("presetDel").disabled = !name;
  if (!name) return;
  try {
    applyOptions(await api(presetUrl(name)));
  } catch (err) { presetNote(err.message, true); return; }
  $("presetName").value = name;
  presetNote(`Loaded “${name}”.`);
  syncPresetFromColors(); syncOptionLabels(); saveOptions(); updateBatch();
  renderSplitList(); draw(); scheduleAnalyze(0);
};

$("presetSave").onclick = async () => {
  const name = $("presetName").value.trim();
  if (!name) { presetNote("Type a name for the preset first.", true); $("presetName").focus(); return; }
  try {
    const res = await fetch(presetUrl(name), {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(snapshotOptions()),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    await loadPresets(name);
    presetNote(`Saved “${name}”.`);
  } catch (err) { presetNote(err.message, true); }
};
$("presetName").addEventListener("keydown", (e) => { if (e.key === "Enter") $("presetSave").click(); });

// Two clicks like the file rows' ×: "Delete" arms, "Delete?" within 3 s deletes.
{
  const del = $("presetDel");
  let timer = null;
  const disarm = () => { clearTimeout(timer); del.classList.remove("armed"); del.textContent = "Delete"; };
  del.onblur = disarm;
  del.onclick = async () => {
    const name = $("presetSel").value;
    if (!name) return;
    if (!del.classList.contains("armed")) {
      del.classList.add("armed"); del.textContent = "Delete?";
      timer = setTimeout(disarm, 3000);
      return;
    }
    disarm();
    try {
      const res = await fetch(presetUrl(name), { method: "DELETE" });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      await loadPresets("");
      presetNote(`Moved “${name}” to the Trash.`);
    } catch (err) { presetNote(err.message, true); }
  };
}

// ---- dragged label offsets, per GPX file -------------------------------
// {file: {labelKey: [dx, dy]}} in points, label top-left relative to the
// labelled point, y down — the format render_pages.py's --style expects.
const LABELS_KEY = "roadbook.labels";
let labelStore = {};
try { labelStore = JSON.parse(localStorage.getItem(LABELS_KEY) || "{}"); } catch (_) {}

function fileOffsets(file = state.file) { return (labelStore[file] ||= {}); }
function saveLabels() {
  try { localStorage.setItem(LABELS_KEY, JSON.stringify(labelStore)); } catch (_) {}
  updateLabelCount();
}
function updateLabelCount() {
  const n = state.file ? Object.keys(fileOffsets()).length : 0;
  $("labelCount").textContent = n ? `${n} label${n > 1 ? "s" : ""} moved on this track` : "";
}
// Must match render_pages.label_key()
const labelKey = (where, pnum, which) => `${where}:${pnum}:${typeof which === "string" ? which : which.toFixed(2)}`;

function syncOptionLabels() {
  const o = readOptions();
  $("pagesOut").textContent = (o.splits || o.print_scale) && state.data ? state.data.n_pages : o.pages;
  $("zoomOut").textContent = o.zoom;
  $("radiusOut").textContent = o.waypoint_radius_m;
  $("simplifyOut").textContent = o.simplify_m ? `${o.simplify_m} m` : "off";
  $("overlapOut").textContent = o.overlap_km;
  $("saturationOut").textContent = Math.round(o.saturation * 100);
  $("lightenOut").textContent = Math.round(o.lighten * 100);
  $("hillshadeOut").textContent = o.hillshade ? `${Math.round(o.hillshade * 100)} %` : "off";
  const prov = state.providers?.find((p) => p.id === o.provider);
  $("keyFields").hidden = !prov?.needs_key;
  if (prov?.needs_key) {
    form.elements.style_id.placeholder = prov.default_style || "";
    form.elements.api_key.placeholder = prov.has_key ? "********************  (saved on server)" : "or set env var on server";
  }

  state.style = readStyle();
  $("trackOut").textContent = state.style.track_width.toFixed(1);
  $("profileOut").textContent = state.style.profile_width.toFixed(1);
  $("smoothOut").textContent = state.style.profile_smooth_m ? `${state.style.profile_smooth_m} m` : "off";
  $("gradeOut").textContent = state.style.grade_max
    ? `±${state.style.grade_max} %`
    : `auto${state.data ? ` (±${state.data.grade_vmax.toFixed(0)} %)` : ""}`;
  const bins = state.style.grade_bins;
  $("scalePreview").style.background = bins
    ? `linear-gradient(90deg, ${binColors().map((c, i, all) => `${c} ${i / all.length * 100}% ${(i + 1) / all.length * 100}%`).join(", ")})`
    : `linear-gradient(90deg, ${state.style.grade_colors.join(", ")})`;
  form.elements.grade_max.disabled = !!bins;  // classes have fixed edges
}

function syncPresetFromColors() {
  const cur = readStyle().grade_colors.join();
  const hit = Object.entries(COLOR_PRESETS).find(([, [, c]]) => c.join() === cur);
  form.elements.preset.value = hit ? hit[0] : "custom";
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
    // stay on the same kind of sheet for the same part after re-analyzing
    const prev = state.data?.sheets?.[state.sheetIdx];
    state.data = data;
    state.page = Math.min(state.page, data.n_pages - 1);
    const same = prev && data.sheets.findIndex((s) => s.kind === prev.kind && s.page === (prev.kind === "final" ? null : state.page) && s.chunk === prev.chunk);
    state.sheetIdx = same >= 0 ? same : mapSheetIdx(state.page);
    $("renderBtn").disabled = !!state.job;
    syncOptionLabels();
    updateLabelCount();
    updateNotes();
    renderSplitList();
    renderPoiList();
    renderStrip();
    renderSummary();
    syncTitleField();
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

// ---- file rows with a delete button -------------------------------------
// × arms the button ("Delete?"), a second click within 3 s deletes. The
// server moves the file to the Trash, so a slip is recoverable.
function fileRow(item, deleteUrl, onDeleted) {
  const row = document.createElement("div");
  row.className = "row";
  const del = document.createElement("button");
  del.className = "del"; del.type = "button"; del.textContent = "×"; del.title = "Move to Trash";
  let timer = null;
  const disarm = () => { clearTimeout(timer); del.classList.remove("armed"); del.textContent = "×"; };
  del.onclick = async (e) => {
    e.preventDefault(); e.stopPropagation();
    if (!del.classList.contains("armed")) {
      del.classList.add("armed"); del.textContent = "Delete?";
      timer = setTimeout(disarm, 3000);
      return;
    }
    disarm(); del.disabled = true;
    try {
      const res = await fetch(deleteUrl, { method: "DELETE" });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      await onDeleted();
    } catch (err) {
      del.disabled = false; del.title = "Delete failed: " + err.message; del.classList.add("failed");
    }
  };
  del.onblur = disarm;
  row.append(item, del);
  return row;
}

// ---- left pane: files ---------------------------------------------------
async function loadFiles() {
  state.files = await api("/api/files");
  renderBatchList();
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
    list.appendChild(fileRow(b, `/api/files?path=${encodeURIComponent(f.path)}`, async () => {
      delete labelStore[f.path]; delete splitStore[f.path]; delete hiddenStore[f.path];
      setTitle(f.path, "");
      try {
        localStorage.setItem(LABELS_KEY, JSON.stringify(labelStore));
        localStorage.setItem(SPLITS_KEY, JSON.stringify(splitStore));
        localStorage.setItem(HIDDEN_KEY, JSON.stringify(hiddenStore));
      } catch (_) {}
      if (state.file === f.path) {
        state.file = null; state.data = null;
        $("renderBtn").disabled = true; $("summary").textContent = ""; $("strip").innerHTML = "";
        $("pagesNote").textContent = ""; $("zoomNote").textContent = ""; $("splitField").hidden = true; $("poiField").hidden = true;
        $("empty").textContent = "Select a GPX track on the left."; $("empty").hidden = false;
        if (state.view === "final") showFinal(); else draw();
      }
      await loadFiles();
    }));
  }
}

function selectFile(path) {
  state.file = path; state.page = 0; state.data = null;
  syncTitleField();
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
  if (d.splits) {
    pn.textContent = `${d.n_pages} page${d.n_pages > 1 ? "s" : ""} · split at ${d.splits.map((k) => k.toFixed(1)).join(", ")} km`;
    pn.className = "note";
  } else if (splitMode() === "waypoints") {
    pn.textContent = "Tick waypoints below to end pages there.";
    pn.className = "note";
  } else if (d.print_scale) {
    pn.textContent = `${d.n_pages} page${d.n_pages > 1 ? "s" : ""} needed at 1:${d.print_scale.toLocaleString("en")} · ` +
      `≈ ${(d.stats.total_km / d.n_pages).toFixed(1)} km per page`;
    pn.className = "note";
  } else if (d.pages_forced_single) {
    pn.textContent = "Filename contains “backup route” → always 1 page.";
    pn.className = "note warn";
  } else {
    pn.textContent = `≈ ${(d.stats.total_km / d.n_pages).toFixed(1)} km per page`;
    pn.className = "note";
  }
  const sn = $("simplifyNote"), s = d.stats;
  sn.dataset.text ??= sn.textContent;
  sn.textContent = s.points_full && s.points < s.points_full
    ? `${s.points.toLocaleString("en")} of ${s.points_full.toLocaleString("en")} track points ` +
      `(${Math.round(100 * s.points / s.points_full)} %). Km and D+/D- stay exact.`
    : `${s.points.toLocaleString("en")} track points. ` + sn.dataset.text;
  const pg = d.pages[state.page];
  const ppi = pg ? Math.round(mosaicPx(pg.bounds, o.zoom, d.tile_px).w / mapBox(pg).w) : 0;
  const zn = $("zoomNote");
  zn.textContent = `${ZOOM_FEEL[o.zoom] || ""} · ${d.tiles_total} tiles · ≈ ${ppi} ppi printed`;
  zn.className = "note" + (d.tiles_total > 600 ? " warn" : "");
  // print scale per map page (1:N, rounded like a map's), see api.ground_scale
  const nice = (n) => { const m = 10 ** Math.floor(Math.log10(n) - 1); return Math.round(n / m) * m; };
  const scales = [...new Set(d.pages.map((p) => nice(p.scale)))];
  const overflow = d.pages.filter((p) => !p.fits).map((p) => p.page);
  $("scaleNote").className = "note scale-note" + (overflow.length ? " warn" : "");
  if (overflow.length) {
    $("scaleNote").textContent = `Track doesn't fit the map on page ${overflow.join(", ")} at this scale — ` +
      "add page ends there or pick a smaller scale.";
  } else $("scaleNote").textContent = scales.length === 1
    ? `Map scale ≈ 1:${scales[0].toLocaleString("en")}`
    : `Map scale ≈ 1:${Math.min(...scales).toLocaleString("en")} – 1:${Math.max(...scales).toLocaleString("en")}`;
}

function renderSummary() {
  const d = state.data;
  const s = d.stats;
  $("summary").textContent =
    `${d.meta.race_title} · ${s.total_km.toFixed(1)} km · D+ ${Math.round(s.gain)} m · ` +
    `D- ${Math.round(s.loss)} m · ${d.pois.length} waypoints · ${d.n_pages} page${d.n_pages > 1 ? "s" : ""}`;
}

const mapSheetIdx = (page) => Math.max(0, state.data.sheets.findIndex((s) => s.kind === "map" && s.page === page));
const currentSheet = () => state.data.sheets[Math.min(state.sheetIdx, state.data.sheets.length - 1)];

function renderStrip() {
  const strip = $("strip");
  strip.innerHTML = "";
  const d = state.data; if (!d) return;
  const cur = currentSheet();
  const chip = (html, active, onclick) => {
    const c = document.createElement("button");
    c.className = "chip" + (active ? " active" : "");
    c.innerHTML = html; c.onclick = onclick;
    strip.appendChild(c);
    return c;
  };
  d.pages.forEach((p, i) => {
    const c = chip(`<b>Part ${p.page}</b><span>${p.d0.toFixed(1)}–${p.d1.toFixed(1)} km · +${Math.round(p.gain)} m</span>`,
      cur.kind !== "final" && i === state.page,
      () => { setPage(i); if (state.view === "overview") setView("page"); });
    c.onmouseenter = () => { state.hoverPage = i; if (state.view === "overview") draw(); };
    c.onmouseleave = () => { state.hoverPage = -1; if (state.view === "overview") draw(); };
  });
  const fin = d.sheets.findIndex((s) => s.kind === "final");
  if (fin >= 0) {
    chip(`<b>All points</b><span>waypoint table · ${d.sheets.filter((s) => s.kind === "final").reduce((a, s) => a + s.rows.length, 0)} rows</span>`,
      cur.kind === "final", () => { setView("page"); setSheetIdx(fin); });
  }
  $("pageLabel").textContent = `${state.sheetIdx + 1} / ${d.sheets.length}`;
}

// a part: jump to its map page
function setPage(i) {
  const d = state.data; if (!d) return;
  state.page = Math.max(0, Math.min(d.n_pages - 1, i));
  state.sheetIdx = mapSheetIdx(state.page);
  renderStrip(); updateNotes(); draw();
}

// any sheet, in document order (‹ › and arrow keys)
function setSheetIdx(i) {
  const d = state.data; if (!d) return;
  state.sheetIdx = Math.max(0, Math.min(d.sheets.length - 1, i));
  const s = currentSheet();
  if (s.page !== null) state.page = s.page;
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

const hexRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const gradeVmax = () => state.style.grade_max || state.data.grade_vmax;

// Drawn profile elevations: the preview track smoothed like
// render_pages.smooth_elevation (point-weighted mean over +-window/2).
function profileEle() {
  const t = state.data.track, w = state.style.profile_smooth_m;
  if (!w) return t.ele;
  const cs = [0]; t.ele.forEach((e, i) => cs.push(cs[i] + e));
  const out = [];
  let lo = 0, hi = 0;
  for (let i = 0; i < t.km.length; i++) {
    while (t.km[lo] < t.km[i] - w / 2000) lo++;
    while (hi < t.km.length && t.km[hi] <= t.km[i] + w / 2000) hi++;
    out.push((cs[hi] - cs[lo]) / (hi - lo));
  }
  return out;
}
const interpKm = (km, arr) => {
  const k = state.data.track.km;
  const i = k.findIndex((v) => v >= km);
  if (i < 0) return arr[arr.length - 1];
  if (i === 0) return arr[0];
  return arr[i - 1] + (arr[i] - arr[i - 1]) * (km - k[i - 1]) / (k[i] - k[i - 1]);
};

// colour at t in 0..1 along the descent -> flat -> climb stops
function scaleColor(t) {
  const stops = state.style.grade_colors.map(hexRgb);
  t = Math.max(0, Math.min(1, t)) * (stops.length - 1);
  const i = Math.min(Math.floor(t), stops.length - 2); t -= i;
  const c = stops[i].map((v, k) => Math.round(v + (stops[i + 1][k] - v) * t));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

// Class edges and colours, as render_pages.binned_scale: bins [5, 10, 20]
// -> edges -20 -10 -5 5 10 20, seven classes sampled evenly along the scale.
const binEdges = () => { const b = state.style.grade_bins; return [...b.map((v) => -v).reverse(), ...b]; };
const binColors = () => { const n = state.style.grade_bins.length * 2 + 1; return [...Array(n)].map((_, i) => scaleColor(i / (n - 1))); };

function gradeColor(g) {
  if (state.style.grade_bins) {
    const k = binEdges().filter((e) => g >= e).length;
    return binColors()[k];
  }
  const vmax = gradeVmax();
  return scaleColor((g + vmax) / (2 * vmax));
}

// Canvas font for labels. Matplotlib's default (DejaVu Sans) is rarely
// installed for the browser; Verdana is its closest common relative.
function fontCss(size, bold, family) {
  const fam = family ? `"${family}", ` : `"DejaVu Sans", Verdana, `;
  return `${bold ? "700 " : ""}${size}px ${fam}-apple-system, Helvetica, sans-serif`;
}

// textwrap.fill(name, width=11) capped at 2 lines + "...", as render_pages does for profile labels
function wrapProfileName(name) {
  const lines = [];
  let cur = "";
  for (let w of name.split(/\s+/).filter(Boolean)) {
    while (w.length > 11) {           // textwrap breaks over-long words
      if (cur) { lines.push(cur); cur = ""; }
      lines.push(w.slice(0, 11)); w = w.slice(11);
    }
    if (!cur) cur = w;
    else if ((cur + " " + w).length <= 11) cur += " " + w;
    else { lines.push(cur); cur = w; }
  }
  if (cur) lines.push(cur);
  if (lines.length > 2) { lines.length = 2; lines[1] += "..."; }
  return lines.join("\n");
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

function drawGradeLine(pts, idx, width, casing) {
  const t = state.data.track;
  ctx.lineCap = "round"; ctx.lineJoin = "round";
  ctx.strokeStyle = "#1a1a1a"; ctx.lineWidth = casing;
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

function label(text, x, y, { size = 11, color = "#1a1a2e", bold = false, align = "left", bg = "rgba(255,255,255,.78)", mono = false, font, italic = false } = {}) {
  ctx.font = (italic ? "italic " : "") + (font !== undefined ? fontCss(size, bold, font)
    : `${bold ? "700 " : ""}${size}px ${mono ? "ui-monospace, Menlo, monospace" : "-apple-system, Helvetica, sans-serif"}`);
  const lines = String(text).split("\n");
  const w = Math.max(...lines.map((l) => ctx.measureText(l).width));
  const lh = size * 1.18, h = lh * lines.length;
  const x0 = align === "left" ? x : align === "right" ? x - w : x - w / 2;
  if (bg) { ctx.fillStyle = bg; ctx.fillRect(x0 - 2, y - 1, w + 4, h + 2); }
  ctx.fillStyle = color; ctx.textBaseline = "top"; ctx.textAlign = "left";
  lines.forEach((l, i) => ctx.fillText(l, x0, y + i * lh));
  return { x0: x0 - 2, y0: y - 1, x1: x0 + w + 2, y1: y + h + 1 };
}

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
  const tw = state.style.track_width / 1.4;  // overview widths scale with the chosen track width
  drawGradeLine((i) => P(t.lon[i], t.lat[i]), all, 2.4 * tw, 2.4 * tw + 1.6);
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
  drawGradeLine((i) => [X(t.km[i]), Y(t.ele[i])], all, 1.4 * tw, 1.4 * tw + 1.2);
  ctx.fillStyle = col.muted; ctx.font = "11px -apple-system, sans-serif"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
  ctx.fillText(`${Math.round(emax)} m`, px0 - 4, Y(emax)); ctx.fillText(`${Math.round(emin)} m`, px0 - 4, Y(emin));
  ctx.textAlign = "left"; ctx.textBaseline = "top";
  ctx.fillText(`${tot.toFixed(1)} km`, px1 - 40, py1 + 3);
}

function outlineIfActive(key, r) {
  const active = state.drag?.key === key, hover = state.hoverLabel === key;
  if (!active && !hover) return;
  ctx.save();
  ctx.strokeStyle = COLORS().accent; ctx.lineWidth = active ? 1.5 : 1; ctx.setLineDash(active ? [] : [3, 2]);
  ctx.strokeRect(r.x0 - 1, r.y0 - 1, r.x1 - r.x0 + 2, r.y1 - r.y0 + 2);
  ctx.restore();
}

// ---- map label placement (mirrors render_pages.build_page) --------------
// Same candidate ring and costs as the PDF; see the comment block there.
const LABEL_VSTEPS = [10, 24, 38, 54, 70, -10, -24, -38, -54, -70];
const LABEL_TRACK_COST = 25, LABEL_DIST_COST = 2, LABEL_CROSS_COST = 600;

// scale bar length, as render_pages: longest round length up to ~40 % of the width
const scaleBarKm = (b, kmLon) => Math.max(...[0.5, 1, 2, 5, 10, 20, 50].filter((l) => l <= (b.east - b.west) * kmLon * 0.4), 0.5);

// Rects of the scale bar, north arrow, colour bar and attribution, as drawn in drawPage.
function mapFurniture(b, mx, my, mw, mh, kmLon, pt) {
  const d = state.data;
  ctx.font = `${pt(8.5)}px -apple-system, Helvetica, sans-serif`;
  const sbW = scaleBarKm(b, kmLon) / kmLon / (b.east - b.west) * mw, sbx = mx + mw * 0.94 - sbW, sby = my + mh * 0.95;
  const sbText = ctx.measureText(`${scaleBarKm(b, kmLon)} km${d.print_scale ? `  ·  1:${d.print_scale.toLocaleString("en")}` : ""}`).width;
  const sbMid = sbx + sbW / 2, sbHalf = Math.max(sbW, sbText + 4) / 2;
  const nx = mx + mw * 0.05, ny = my + mh * 0.07;
  const cbx = mx + mw * (1 - 0.012 - 0.018), cby = my + mh * 0.02, cbh = mh * 0.28;
  ctx.font = `${pt(6.5)}px -apple-system, Helvetica, sans-serif`;
  const attrW = ctx.measureText(d.attribution).width + 4;
  return [
    { x0: sbMid - sbHalf, y0: sby - pt(18), x1: sbMid + sbHalf, y1: sby + pt(3) },
    { x0: nx - pt(7), y0: ny - pt(15), x1: nx + pt(7), y1: ny + pt(13) },
    { x0: cbx - pt(24), y0: cby, x1: cbx + mw * 0.018, y1: cby + cbh },
    { x0: mx + mw * 0.01, y0: my + mh * 0.99 - pt(10), x1: mx + mw * 0.01 + attrW, y1: my + mh * 0.99 },
  ];
}

// track points thinned to >= `gap` px apart (render_pages samples every ~3 px at 100 dpi)
function thinTrack(pts, gap) {
  const out = [];
  for (const q of pts) {
    const last = out[out.length - 1];
    if (!last || Math.hypot(q[0] - last[0], q[1] - last[1]) >= gap) out.push(q);
  }
  return out;
}

function placeLabels(items, { mx, my, mw, mh, leaders, attach, putLabel, measure, placed, pt, obstacles, track, u }) {
  const area = (a, b) => Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)) *
    Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));
  const orient = (a, b, c) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  const crosses = (p1, p2, q1, q2) => orient(p1, p2, q1) * orient(p1, p2, q2) < 0 && orient(q1, q2, p1) * orient(q1, q2, p2) < 0;
  const through = (a, b, r) => {  // sampled, label rect shrunk 1 px
    for (let k = 0; k < 12; k++) {
      const t = 0.05 + 0.9 * k / 11, x = a[0] + (b[0] - a[0]) * t, y = a[1] + (b[1] - a[1]) * t;
      if (x > r.x0 + 1 && x < r.x1 - 1 && y > r.y0 + 1 && y < r.y1 - 1) return true;
    }
    return false;
  };
  const midX = mx + mw / 2;
  for (const it of items) {
    const { w, h } = measure(it.text, it.opts.size, it.opts.bold), a = [it.ax, it.ay];
    let best = null;
    for (const side of it.ax <= midX ? [1, -1] : [-1, 1]) for (const dx of it.dxs) for (const dy of LABEL_VSTEPS) {
      // va="baseline" in matplotlib: the point sits on the last line's
      // baseline, descenders (~0.34 em) hang below it
      const lx = side > 0 ? it.ax + pt(dx) : it.ax - pt(dx) - w, ly = it.ay - pt(dy) + 0.34 * it.opts.size - h;
      const r = { x0: lx, y0: ly, x1: lx + w, y1: ly + h }, tip = attach(r, it.ax, it.ay);
      let c = LABEL_DIST_COST * Math.hypot(pt(dx), pt(dy)) * u;
      for (const q of placed) c += area(q, r) * u * u + LABEL_CROSS_COST * through(a, tip, q);
      for (const q of obstacles) c += area(q, r) * u * u;
      for (const [x, y] of track) if (x >= r.x0 && x <= r.x1 && y >= r.y0 && y <= r.y1) c += LABEL_TRACK_COST;
      for (const [p, q] of leaders) c += LABEL_CROSS_COST * (crosses(a, tip, p, q) + through(p, q, r));
      if (r.x0 < mx || r.x1 > mx + mw || r.y0 < my || r.y1 > my + mh) c += 1e7;
      if (!best || c < best.c) best = { c, r };
    }
    const r = putLabel(it.key, it.text, it.ax, it.ay, best.r.x0 + 2, best.r.y0 + 1, it.opts, it.leader);
    leaders.push([a, attach(r, it.ax, it.ay)]);
  }
}

function drawPage(W, H) {
  const d = state.data, pg = d.pages[state.page], o = readOptions();
  state.hits = [];
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
  const sheet = currentSheet(), tableSheet = sheet.kind !== "map";
  label(tableSheet ? sheet.subtitle : pg.subtitle, hx1, cellY(hr, 0.40), { size: pt(10.5), bold: true, color: "#2f3e8a", bg: null, align: "right" });
  label(tableSheet ? sheet.stats
        : `Part ${pg.page}/${d.n_pages} (${pg.d0.toFixed(1)}-${pg.d1.toFixed(1)} km)  |  Distance ${pg.length_km.toFixed(1)} km  |  ` +
          `D+ ${Math.round(pg.gain)} m  |  D- ${Math.round(pg.loss)} m  |  Elev min/max ${Math.round(pg.min_ele)}/${Math.round(pg.max_ele)} m`,
        hx, cellY(hr, -0.05), { size: pt(8), color: "#333", bg: null, mono: true });
  if (m.version || m.update_tag) label(`Rev. ${m.version} - ${m.update_tag}`, hx1, cellY(hr, 1.0), { size: pt(7.5), color: "#888", bg: null, align: "right" });
  ctx.strokeStyle = "#1a1a2e"; ctx.lineWidth = pt(1.2);
  ctx.beginPath(); ctx.moveTo(hx, cellY(hr, -0.28)); ctx.lineTo(hx1, cellY(hr, -0.28)); ctx.stroke();

  // footer (render_pages.draw_footer): map pages first, then the table page(s)
  const footer = (num) => {
    const fr = rows[3];
    const total = d.sheets.length;
    ctx.strokeStyle = "#1a1a2e"; ctx.lineWidth = pt(0.7);
    ctx.beginPath(); ctx.moveTo(FX(fr.x0), FY(fr.y1)); ctx.lineTo(FX(fr.x1), FY(fr.y1)); ctx.stroke();
    label(`Roadbook - Sector ${m.sector_code} - Page ${num} of ${total}`, FX(fr.x0), cellY(fr, 0.85), { size: pt(7.5), color: "#666", bg: null });
    label(`Generated ${new Date().toISOString().slice(0, 10)} - GPX: ${m.start_name} - ${m.end_name} (${m.update_tag})`,
          FX(fr.x1), cellY(fr, 0.85), { size: pt(7.5), color: "#666", bg: null, align: "right" });
  };

  if (tableSheet) {
    drawTable(sheet.rows, { x0: FX(rows[1].x0), x1: FX(rows[1].x1), y0: FY(rows[1].y1), y1: FY(rows[2].y0) }, pt);
    footer(state.sheetIdx + 1);
    return;
  }

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
  // neighbouring parts' track, dashed + faded as render_pages draws it
  ctx.save();
  ctx.setLineDash([pt(2 * state.style.track_width), pt(1.5 * state.style.track_width)]);
  ctx.strokeStyle = "rgba(26,26,26,.45)"; ctx.lineWidth = pt(state.style.track_width); ctx.lineCap = "round";
  for (const [a, z] of pg.overlap_km > 0 ? [[pg.d0 - pg.overlap_km, pg.d0], [pg.d1, pg.d1 + pg.overlap_km]] : []) {
    ctx.beginPath();
    let k = 0;
    for (let i = 0; i < t.km.length; i++) if (t.km[i] >= a && t.km[i] <= z) {
      const [x, y] = MP(t.lon[i], t.lat[i]); k++ ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.stroke();
  }
  ctx.restore();
  drawGradeLine((i) => MP(t.lon[i], t.lat[i]), idx, pt(state.style.track_width), pt(state.style.track_width + 0.9));

  // markers + labels. Dragged labels (offsets in points, see labelKey) are
  // placed first; automatic ones then avoid them — same order as render_pages.
  // Labels are drawn after the map clip is released: the PDF doesn't clip them either.
  const LF = state.style.label_font, ptpx = s / 72, offs = fileOffsets(), pnum = pg.page;
  const placed = [], deferred = [];
  const measure = (text, size, bold) => {
    ctx.font = fontCss(size, bold, LF);
    const lines = text.split("\n");
    return { w: Math.max(...lines.map((l) => ctx.measureText(l).width)) + 4, h: size * 1.18 * lines.length + 2 };
  };
  // one draggable label: rect at text top-left (tx, ty), registered for hit-testing
  const putLabel = (key, text, ax, ay, tx, ty, opts, leader) => {
    const { w, h } = measure(text, opts.size, opts.bold);
    const r = { x0: tx - 2, y0: ty - 1, x1: tx - 2 + w, y1: ty - 1 + h };
    placed.push(r);
    state.hits.push({ key, ...r, tx, ty, ax, ay, ptpx });
    deferred.push(() => {
      if (leader) {
        const lx = ax < r.x0 ? r.x0 : ax > r.x1 ? r.x1 : ax, ly = ay < r.y0 ? r.y0 : ay > r.y1 ? r.y1 : ay;
        ctx.strokeStyle = leader; ctx.lineWidth = pt(0.5);
        ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(lx, ly); ctx.stroke();
      }
      label(text, tx, ty, { ...opts, font: LF });
      outlineIfActive(key, r);
    });
    return r;
  };
  const fromOffset = (key, ax, ay) => offs[key] && [ax + offs[key][0] * ptpx, ay + offs[key][1] * ptpx];

  const first = idx[0], last = idx[idx.length - 1];
  const isStart = state.page === 0, isEnd = state.page === d.n_pages - 1;
  const [sx0, sy0] = MP(t.lon[first], t.lat[first]), [ex0, ey0] = MP(t.lon[last], t.lat[last]);
  marker(sx0, sy0, pt(4.5), "#1b8a3a"); marker(ex0, ey0, pt(4.5), "#c0392b");

  const pois = pagePois(pg);
  for (const p of pois) { const [x, y] = MP(p.lon, p.lat); marker(x, y, pt(3.2), "#c0392b"); }

  const items = [
    { key: labelKey("map", pnum, "start"), ax: sx0, ay: sy0, text: isStart ? "START" : `${pg.d0.toFixed(1)} km\n(from part ${pnum - 1})`,
      opts: { size: pt(8.5), bold: true, color: "#1b8a3a" }, dxs: [8, 24], leader: "#1b8a3a" },
    { key: labelKey("map", pnum, "end"), ax: ex0, ay: ey0, text: isEnd ? "FINISH" : `${pg.d1.toFixed(1)} km\n(continues part ${pnum + 1})`,
      opts: { size: pt(8.5), bold: true, color: "#c0392b" }, dxs: [8, 24], leader: "#c0392b" },
    ...pois.map((p) => {
      const [x, y] = MP(p.lon, p.lat);
      return { key: labelKey("map", pnum, p.km), ax: x, ay: y, text: `${p.name}\n${p.km.toFixed(1)} km - ${Math.round(p.ele)} m`,
               opts: { size: pt(8) }, dxs: [12, 30], leader: "#333" };
    }),
  ];
  const leaders = [];  // [anchor, attach] of labels placed so far
  const attach = (r, x, y) => [Math.min(Math.max(x, r.x0), r.x1), Math.min(Math.max(y, r.y0), r.y1)];
  for (const it of items) {
    const at = fromOffset(it.key, it.ax, it.ay);
    if (!at) continue;
    const r = putLabel(it.key, it.text, it.ax, it.ay, at[0], at[1], it.opts, it.leader);
    leaders.push([[it.ax, it.ay], attach(r, it.ax, it.ay)]);
  }
  placeLabels(items.filter((it) => !offs[it.key]), {
    mx, my, mw, mh, leaders, attach, putLabel, measure, placed, pt,
    // obstacles besides placed labels: markers and the map furniture drawn below
    obstacles: [
      ...[[sx0, sy0], [ex0, ey0], ...pois.map((p) => MP(p.lon, p.lat))]
        .map(([x, y]) => ({ x0: x - pt(5.8), y0: y - pt(5.8), x1: x + pt(5.8), y1: y + pt(5.8) })),
      ...mapFurniture(b, mx, my, mw, mh, kmLon, pt),
    ],
    track: thinTrack(idx.map((i) => MP(t.lon[i], t.lat[i])), pt(2.16)),
    u: 100 / 72 / ptpx,  // canvas px -> render_pages px (100 dpi), so the costs match
  });

  // scale bar, north arrow, colorbar, attribution
  const sbKm = scaleBarKm(b, kmLon);
  const sbW = sbKm / kmLon / (b.east - b.west) * mw;
  const sbx = mx + mw * 0.94 - sbW, sby = my + mh * 0.95;
  ctx.strokeStyle = "#fff"; ctx.lineWidth = pt(3.6); ctx.beginPath(); ctx.moveTo(sbx, sby); ctx.lineTo(sbx + sbW, sby); ctx.stroke();
  ctx.strokeStyle = "#000"; ctx.lineWidth = pt(2.4); ctx.beginPath(); ctx.moveTo(sbx, sby); ctx.lineTo(sbx + sbW, sby); ctx.stroke();
  label(`${sbKm} km${d.print_scale ? `  ·  1:${d.print_scale.toLocaleString("en")}` : ""}`,
        sbx + sbW / 2, sby - pt(16), { size: pt(8.5), align: "center" });
  const nx = mx + mw * 0.05, ny = my + mh * 0.07;
  ctx.fillStyle = "#000"; ctx.beginPath(); ctx.moveTo(nx, ny - pt(14)); ctx.lineTo(nx - pt(4), ny - pt(4)); ctx.lineTo(nx + pt(4), ny - pt(4)); ctx.fill();
  label("N", nx, ny, { size: pt(10.5), bold: true, align: "center" });
  const cbx = mx + mw * (1 - 0.012 - 0.018), cby = my + mh * 0.02, cbh = mh * 0.28;
  // colour bar with ticks on the map side, as render_pages
  const cbw = mw * 0.018, tick = (txt, y) => label(txt, cbx - 3, y - pt(4), { size: pt(6.5), align: "right", bg: "rgba(255,255,255,.75)" });
  if (state.style.grade_bins) {
    const cols = binColors(), hh = cbh / cols.length;
    cols.forEach((c, i) => { ctx.fillStyle = c; ctx.fillRect(cbx, cby + cbh - (i + 1) * hh, cbw, hh); });
    binEdges().forEach((e, i) => tick(`${e > 0 ? "+" : ""}${e}`, cby + cbh - (i + 1) * hh));
  } else {
    const grad = ctx.createLinearGradient(0, cby + cbh, 0, cby);
    state.style.grade_colors.forEach((c, i, all) => grad.addColorStop(i / (all.length - 1), c));
    ctx.fillStyle = grad; ctx.fillRect(cbx, cby, cbw, cbh);
    tick(`+${gradeVmax().toFixed(0)}`, cby); tick(`-${gradeVmax().toFixed(0)}`, cby + cbh);
  }
  ctx.strokeStyle = "#fff"; ctx.lineWidth = 1; ctx.strokeRect(cbx, cby, cbw, cbh);
  label(d.attribution, mx + mw * 0.01, my + mh * 0.99 - pt(9), { size: pt(6.5), color: "#fff", bg: "rgba(0,0,0,.55)" });
  ctx.restore();
  ctx.strokeStyle = "#1a1a2e"; ctx.lineWidth = pt(1); ctx.strokeRect(mx, my, mw, mh);
  deferred.forEach((f) => f());
  deferred.length = 0;

  // elevation profile (render_pages shifts it right by 0.045 and narrows by
  // 0.055, or 0.095 to make room for the cumulative D+ axis)
  const er = rows[2], cumGain = state.style.profile_cum_gain;
  const ex = FX(er.x0 + 0.045), ew = (er.x1 - er.x0 - (cumGain ? 0.095 : 0.055)) * PW * s;
  const ey = FY(er.y1), eh = (er.y1 - er.y0) * PH * s;
  const pele = profileEle(), segEle = idx.map((i) => pele[i]);
  const ylo = Math.min(Math.min(...segEle) - 100, 900), yhi = Math.max(...segEle) + 340;
  const onLine = (p) => state.style.profile_smooth_m ? interpKm(p.km, pele) : p.ele;
  const EX = (km) => ex + (km - pg.d0) / (pg.d1 - pg.d0) * ew, EY = (e) => ey + eh - (e - ylo) / (yhi - ylo) * eh;
  ctx.strokeStyle = "#333"; ctx.lineWidth = 0.8;
  ctx.beginPath(); ctx.moveTo(ex, ey); ctx.lineTo(ex, ey + eh); ctx.lineTo(ex + ew, ey + eh); ctx.stroke();
  ctx.save(); ctx.beginPath(); ctx.rect(ex, ey - 20, ew, eh + 20); ctx.clip();
  for (const p of pois) {
    ctx.strokeStyle = "#ddd"; ctx.lineWidth = pt(0.4);
    ctx.beginPath(); ctx.moveTo(EX(p.km), ey); ctx.lineTo(EX(p.km), ey + eh); ctx.stroke();
  }
  if (cumGain) {  // dashed D+ from start, behind the profile, own scale (as render_pages' twin axis)
    const cum = idx.map((i) => t.cum_up[i]), clo = Math.min(...cum), chi = Math.max(...cum);
    const CY = (v) => ey + eh - (v - clo) / (chi + Math.max(chi - clo, 50) * 0.35 - clo) * eh;
    ctx.save(); ctx.setLineDash([pt(3.2), pt(1.6)]); ctx.strokeStyle = "#666"; ctx.lineWidth = pt(0.8);
    ctx.beginPath(); idx.forEach((i, k) => k ? ctx.lineTo(EX(t.km[i]), CY(t.cum_up[i])) : ctx.moveTo(EX(t.km[i]), CY(t.cum_up[i])));
    ctx.stroke(); ctx.restore();
    ctx.fillStyle = "#666"; ctx.font = `${pt(6.5)}px -apple-system, sans-serif`; ctx.textAlign = "left"; ctx.textBaseline = "middle";
    ctx.fillText(`${Math.round(clo)}`, ex + ew + 4, CY(clo)); ctx.fillText(`${Math.round(chi)} m D+`, ex + ew + 4, CY(chi));
  }
  drawGradeLine((i) => [EX(t.km[i]), EY(pele[i])], idx, pt(state.style.profile_width), pt(state.style.profile_width + 0.6));
  for (const p of pois) marker(EX(p.km), EY(onLine(p)), pt(2), "#2c3e50");
  ctx.restore();
  // profile labels: default centred just above the point (xytext=(0, 5), va="bottom")
  for (const p of pois) {
    const key = labelKey("prof", pnum, p.km), text = wrapProfileName(p.name), opts = { size: pt(6.5) };
    const ax = EX(p.km), ay = EY(onLine(p));
    const at = fromOffset(key, ax, ay);
    if (at) putLabel(key, text, ax, ay, at[0], at[1], opts, "#888");
    else {
      const { w, h } = measure(text, opts.size);
      putLabel(key, text, ax, ay, ax - (w - 4) / 2, ay - pt(5) - (h - 2), opts, null);
    }
  }
  deferred.forEach((f) => f());
  ctx.fillStyle = "#333"; ctx.font = `${pt(7)}px -apple-system, sans-serif`; ctx.textBaseline = "middle"; ctx.textAlign = "right";
  for (const e of [ylo, (ylo + yhi) / 2, yhi]) ctx.fillText(`${Math.round(e / 100) * 100}`, ex - 4, EY(e));
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  ctx.fillText(pg.d0.toFixed(1), ex, ey + eh + 3); ctx.fillText(`${pg.d1.toFixed(1)} km`, ex + ew, ey + eh + 3);

  footer(state.sheetIdx + 1);
}

// Mirrors render_pages.TABLE_COLUMNS / build_table_page.
const TABLE_COLUMNS = [
  ["name", "Point", 0.31, null], ["km", "km\nfrom start", 0.085, 1], ["dkm_prev", "km from\nprevious", 0.085, 1],
  ["up_prev", "D+ from\nprevious", 0.075, 0], ["down_prev", "D- from\nprevious", 0.075, 0],
  ["up_next", "D+ to\nnext", 0.075, 0], ["down_next", "D- to\nnext", 0.075, 0],
  ["up_total", "D+ from\nstart", 0.11, 0], ["down_total", "D- from\nstart", 0.11, 0],
];

function drawTable(rows, box, pt) {
  const W = box.x1 - box.x0, H = box.y1 - box.y0;
  const headH = pt(0.42 * 72);
  const rowH = Math.min(pt(0.26 * 72), (H - headH - H * 0.01) / Math.max(rows.length, 1));
  const size = Math.max(pt(5), Math.min(pt(8.5), rowH * 0.55));
  const font = state.style.label_font || "";
  const xs = [0]; for (const c of TABLE_COLUMNS) xs.push(xs[xs.length - 1] + c[2]);
  const X = (f) => box.x0 + f * W, pad = 0.006;
  const cell = (text, i, y, opts) => {
    const left = i === 0;
    ctx.save(); ctx.beginPath(); ctx.rect(X(xs[i]), y - rowH, X(xs[i + 1]) - X(xs[i]), rowH * 2); ctx.clip();
    label(text, left ? X(xs[i] + pad) : X(xs[i + 1] - pad), y, { bg: null, align: left ? "left" : "right", ...opts });
    ctx.restore();
  };

  ctx.fillStyle = "#1a1a2e"; ctx.fillRect(box.x0, box.y0, W, headH);
  TABLE_COLUMNS.forEach(([, head], i) => {
    const n = head.split("\n").length, sz = pt(7.5);
    cell(head, i, box.y0 + headH / 2 - n * sz * 1.18 / 2, { size: sz, bold: true, color: "#fff" });
  });
  rows.forEach((r, j) => {
    const y = box.y0 + headH + j * rowH;
    if (j % 2) { ctx.fillStyle = "#f0f2f6"; ctx.fillRect(box.x0, y, W, rowH); }
    ctx.strokeStyle = "#d5d9e0"; ctx.lineWidth = pt(0.4);
    ctx.beginPath(); ctx.moveTo(box.x0, y + rowH); ctx.lineTo(box.x1, y + rowH); ctx.stroke();
    TABLE_COLUMNS.forEach(([key, , , dec], i) => {
      const v = r[key];
      const text = dec === null ? v : v === null ? "–" : v.toFixed(dec);
      cell(text, i, y + rowH / 2 - size * 1.18 / 2, { size, bold: i < 2, font, italic: r.boundary, color: r.boundary ? "#5a6170" : "#1a1a2e" });
    });
  });
  const yEnd = box.y0 + headH + rows.length * rowH;
  ctx.strokeStyle = "#b8bec9"; ctx.lineWidth = pt(0.5);
  for (const k of [2, 5, 7]) { ctx.beginPath(); ctx.moveTo(X(xs[k]), box.y0); ctx.lineTo(X(xs[k]), yEnd); ctx.stroke(); }
  if (!rows.length) label("No waypoints.", box.x0 + W / 2, box.y0 + headH + H * 0.05, { size: pt(9), color: "#666", bg: null, align: "center" });
}

// ---- final PDF + render jobs ------------------------------------------
async function loadOutputs() {
  state.outputs = await api("/api/output");
  const list = $("outputList");
  list.innerHTML = "";
  for (const o of state.outputs.slice(0, 15)) {
    const a = document.createElement("a");
    a.href = `/output/${encodeURIComponent(o.name)}`; a.target = "_blank"; a.textContent = o.name; a.title = o.name;
    list.appendChild(fileRow(a, `/api/output/${encodeURIComponent(o.name)}`, async () => {
      await loadOutputs();
      if (state.view === "final") { $("pdfFrame").removeAttribute("src"); showFinal(); }
    }));
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
  if (!state.file || state.job) return;
  const box = $("progress");
  box.hidden = false; box.className = "progress";
  setBusy(true);
  try {
    state.job = await api("/api/render", { file: state.file, options: { ...readOptions(), style: readStyle() } });
    pollJob();
  } catch (e) {
    box.className = "progress error"; $("progressMsg").textContent = e.message;
    setBusy(false);
  }
}

async function pollJob() {
  const job = state.job = await api(`/api/jobs/${state.job.id}`);
  const finished = job.status === "done" || job.status === "error";
  let frac = job.step / 4, msg = `${job.file}: ${job.message}`;
  if (job.kind === "batch") {
    // 4 steps per track, plus one for the merge
    const units = job.n * 4 + (job.merge ? 1 : 0);
    const done = job.file === "all tracks" ? job.n * 4 : Math.max(0, job.index - 1) * 4 + job.step;
    frac = finished ? 1 : done / units;
    msg = finished ? job.message : `[${job.index}/${job.n}] ${msg}`;
    if (finished) msg += job.failed.map((f) => `\n✗ ${f.file}: ${f.error}`).join("");
  }
  $("barFill").style.width = `${Math.min(100, frac * 100 - (finished ? 0 : 8))}%`;
  $("progressMsg").textContent = msg;
  if (finished) {
    $("progress").className = "progress " + (job.status === "done" && job.failed?.length ? "error" : job.status);
    state.job = null;
    setBusy(false);
    await loadOutputs();
    if (job.kind !== "batch" && job.status === "done") setView("final");
    else if (state.view === "final") showFinal();
    return;
  }
  setTimeout(pollJob, 700);
}

// One render job at a time from this page (the server queues anyway).
function setBusy(busy) {
  $("renderBtn").disabled = busy || !state.file;
  updateBatch(busy);
}

// ---- batch render --------------------------------------------------------
// Ticked tracks, remembered per browser; rendered in the file list's order.
const BATCH_KEY = "roadbook.batch";
let batchSel = new Set();
try { batchSel = new Set(JSON.parse(localStorage.getItem(BATCH_KEY) || "[]")); } catch (_) {}
function saveBatch() { try { localStorage.setItem(BATCH_KEY, JSON.stringify([...batchSel])); } catch (_) {} }
const batchFiles = () => state.files.filter((f) => batchSel.has(f.path));

// Must match run_pipeline.merge_sectors()
const mergedName = (title) =>
  `${(title || "roadbook").replace(/[^a-zA-Z0-9]+/g, "_").replace(/^_+|_+$/g, "").toLowerCase() || "roadbook"}_ALL.pdf`;

function renderBatchList() {
  const list = $("batchList");
  list.innerHTML = "";
  for (const folder of [...new Set(state.files.map((f) => f.folder))]) {
    const inFolder = state.files.filter((f) => f.folder === folder);
    const head = document.createElement("label");
    head.className = "folder";
    const all = document.createElement("input");
    all.type = "checkbox";
    all.checked = inFolder.every((f) => batchSel.has(f.path));
    all.indeterminate = !all.checked && inFolder.some((f) => batchSel.has(f.path));
    all.onchange = () => {
      for (const f of inFolder) all.checked ? batchSel.add(f.path) : batchSel.delete(f.path);
      saveBatch(); renderBatchList();
    };
    head.append(all, folder + "/");
    list.appendChild(head);
    for (const f of inFolder) {
      const row = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox"; cb.checked = batchSel.has(f.path);
      cb.onchange = () => { cb.checked ? batchSel.add(f.path) : batchSel.delete(f.path); saveBatch(); renderBatchList(); };
      const name = document.createElement("span");
      name.textContent = f.name.replace(/\.gpx$/i, ""); name.title = f.name;
      row.append(cb, name);
      list.appendChild(row);
    }
  }
  updateBatch(!!state.job);
}

function updateBatch(busy = !!state.job) {
  const n = batchFiles().length, merge = $("batchMerge").checked && n > 1;
  $("batchBtn").disabled = busy || !n;
  $("batchBtn").textContent = n ? `Render ${n} track${n > 1 ? "s" : ""}${merge ? " + merge" : ""}` : "Render tracks";
  $("batchNote").textContent = merge ? `Merged PDF → output/${mergedName(titleStore[batchFiles()[0]?.path] || "")}` : "";
}

async function startBatch() {
  const files = batchFiles();
  if (!files.length || state.job) return;
  const box = $("progress");
  box.hidden = false; box.className = "progress";
  $("barFill").style.width = "0";
  setBusy(true);
  try {
    state.job = await api("/api/batch", {
      merge: $("batchMerge").checked,
      items: files.map((f) => ({ file: f.path, options: { ...readOptions(f.path), style: readStyle(f.path) } })),
    });
    pollJob();
  } catch (e) {
    box.className = "progress error"; $("progressMsg").textContent = e.message;
    setBusy(false);
  }
}

// ---- wiring ------------------------------------------------------------
form.addEventListener("input", (e) => {
  const name = e.target.name;
  if (name === "preset" && COLOR_PRESETS[e.target.value]) {
    const [c1, c2, c3] = COLOR_PRESETS[e.target.value][1];
    form.elements.c_desc.value = c1; form.elements.c_flat.value = c2; form.elements.c_climb.value = c3;
  } else if (/^c_/.test(name)) syncPresetFromColors();
  if (name === "race_title") setTitle(state.file, e.target.value.trim());
  syncOptionLabels(); saveOptions(); updateBatch();
  if (isStyleField(name)) { draw(); return; }  // style: local redraw only
  if (["saturation", "lighten", "hillshade"].includes(name)) return;  // final basemap only, nothing to preview
  if (name === "pages") renderSplitList();
  // title only changes text; everything else changes page split / bounds
  scheduleAnalyze(name === "race_title" ? 300 : 120);
});
$("renderBtn").onclick = startRender;
$("batchBtn").onclick = startBatch;
$("batchMerge").onchange = () => updateBatch();
$("batchAll").onclick = () => { for (const f of state.files) batchSel.add(f.path); saveBatch(); renderBatchList(); };
$("batchNone").onclick = () => { batchSel.clear(); saveBatch(); renderBatchList(); };
$("prevPage").onclick = () => setSheetIdx(state.sheetIdx - 1);
$("nextPage").onclick = () => setSheetIdx(state.sheetIdx + 1);
for (const t of document.querySelectorAll(".tab")) t.onclick = () => setView(t.dataset.view);
window.addEventListener("resize", draw);
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select")) return;
  if (state.view !== "page") return;
  if (e.key === "ArrowLeft") setSheetIdx(state.sheetIdx - 1);
  if (e.key === "ArrowRight") setSheetIdx(state.sheetIdx + 1);
});

// ---- canvas interaction: overview page boxes, draggable page labels ------
// Pointer events so the same code works with mouse, pen and touch (iPad).
const canvasXY = (e) => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
function labelAt(x, y) {
  const hits = state.hits || [];
  for (let i = hits.length - 1; i >= 0; i--) {
    const h = hits[i];
    if (x >= h.x0 && x <= h.x1 && y >= h.y0 && y <= h.y1) return h;
  }
  return null;
}

canvas.addEventListener("pointermove", (e) => {
  if (!state.data) return;
  const [x, y] = canvasXY(e);
  if (state.view === "overview") {
    const hits = overviewRects.filter((q) => x >= q.x0 && x <= q.x1 && y >= q.y0 && y <= q.y1);
    hits.sort((a, b) => (a.x1 - a.x0) * (a.y1 - a.y0) - (b.x1 - b.x0) * (b.y1 - b.y0));
    const hp = hits.length ? hits[0].i : -1;
    canvas.style.cursor = hp >= 0 ? "pointer" : "default";
    if (hp !== state.hoverPage) { state.hoverPage = hp; draw(); }
  } else if (state.view === "page") {
    const dr = state.drag;
    if (dr) {
      // new text top-left -> offset in points from the labelled point
      fileOffsets()[dr.key] = [+((x - dr.gx - dr.ax) / dr.ptpx).toFixed(1), +((y - dr.gy - dr.ay) / dr.ptpx).toFixed(1)];
      draw();
      return;
    }
    const h = labelAt(x, y);
    canvas.style.cursor = h ? "grab" : "default";
    if ((h?.key || null) !== (state.hoverLabel || null)) { state.hoverLabel = h?.key || null; draw(); }
  }
});

canvas.addEventListener("pointerdown", (e) => {
  if (state.view !== "page" || !state.data) return;
  const [x, y] = canvasXY(e);
  const h = labelAt(x, y);
  if (!h) return;
  state.drag = { key: h.key, gx: x - h.tx, gy: y - h.ty, ax: h.ax, ay: h.ay, ptpx: h.ptpx };
  canvas.setPointerCapture(e.pointerId);
  canvas.classList.add("dragging");
  e.preventDefault();
  draw();
});

function endDrag() {
  if (!state.drag) return;
  state.drag = null;
  canvas.classList.remove("dragging");
  saveLabels();
  draw();
}
canvas.addEventListener("pointerup", endDrag);
canvas.addEventListener("pointercancel", endDrag);
canvas.addEventListener("pointerleave", () => { if (state.hoverLabel && !state.drag) { state.hoverLabel = null; draw(); } });

canvas.addEventListener("dblclick", (e) => {
  if (state.view !== "page") return;
  const h = labelAt(...canvasXY(e));
  if (h && fileOffsets()[h.key]) { delete fileOffsets()[h.key]; saveLabels(); draw(); }
});

canvas.addEventListener("click", () => {
  if (state.view === "overview" && state.hoverPage >= 0) { setPage(state.hoverPage); setView("page"); }
});

$("resetPageLabels").onclick = () => {
  if (!state.data) return;
  const pnum = state.data.pages[state.page].page, offs = fileOffsets();
  for (const k of Object.keys(offs)) if (k.startsWith(`map:${pnum}:`) || k.startsWith(`prof:${pnum}:`)) delete offs[k];
  saveLabels(); draw();
};
$("resetAllLabels").onclick = () => { if (state.file) { labelStore[state.file] = {}; saveLabels(); draw(); } };

const drop = $("drop");
$("upload").onchange = (e) => uploadFiles(e.target.files);
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); uploadFiles(e.dataTransfer.files); });

(async function init() {
  state.providers = await api("/api/providers");
  form.elements.provider.innerHTML = state.providers.map((p) => `<option value="${p.id}">${p.id}${p.needs_key ? " (API key)" : ""}</option>`).join("");
  form.elements.provider.value = state.providers.find((p) => p.default)?.id || state.providers[0].id;
  const fonts = await api("/api/fonts");
  form.elements.label_font.innerHTML = fonts.map((f) =>
    `<option value="${f === "DejaVu Sans" ? "" : f}">${f === "DejaVu Sans" ? "DejaVu Sans (default)" : f}</option>`).join("");
  form.elements.preset.innerHTML = Object.entries(COLOR_PRESETS).map(([k, [name]]) => `<option value="${k}">${name}</option>`).join("")
    + `<option value="custom">Custom</option>`;
  restoreOptions();
  loadPresets().catch((err) => presetNote(err.message, true));
  syncPresetFromColors();
  syncOptionLabels();
  setView("overview");
  await Promise.all([loadFiles(), loadOutputs()]);
})();
