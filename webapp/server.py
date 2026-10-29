"""
server.py — local web app for Roadbook Builder (Phase 1a mockup, see PLAN.md).

Run from the project root:
    .venv/bin/python -m webapp.server
then open http://127.0.0.1:8000

Endpoints:
    GET  /api/files              GPX files in input/ and input-TORX/
    POST /api/upload             multipart .gpx upload -> saved into input/
    POST /api/analyze            {file, options} -> schematic preview data
    POST /api/render             {file, options} -> {job}
    POST /api/batch              {items: [{file, options}], merge} -> {job}
    GET  /api/jobs/{job}         render progress / result
    GET  /api/output             finished roadbook PDFs
    GET  /output/{name}          serve a finished PDF
    DELETE /api/files?path=...   move a GPX to the Trash
    DELETE /api/output/{name}    move a finished PDF to the Trash
    GET  /api/presets            saved per-race option sets (names)
    GET  /api/presets/{name}     one preset's options
    PUT  /api/presets/{name}     save options as a preset (overwrites)
    DELETE /api/presets/{name}   move a preset to the Trash
"""
import json
import re
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from roadbook import api, fetch_basemap

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
OUTPUT_DIR = PROJECT_ROOT / "output"
UPLOAD_DIR = PROJECT_ROOT / "input"
SOURCE_DIRS = [PROJECT_ROOT / "input", PROJECT_ROOT / "input-TORX"]
PRESET_DIR = PROJECT_ROOT / "presets"

app = FastAPI(title="Roadbook Builder")


class JobRequest(BaseModel):
    file: str
    options: dict = {}


class BatchRequest(BaseModel):
    items: list[JobRequest]  # in sector order; options per file (own splits, labels)
    merge: bool = True


def resolve_gpx(rel: str) -> Path:
    """Map a client-supplied relative path to a GPX inside SOURCE_DIRS only."""
    p = (PROJECT_ROOT / rel).resolve()
    if p.suffix.lower() != ".gpx" or not p.is_file() or not any(p.is_relative_to(d) for d in SOURCE_DIRS):
        raise HTTPException(404, f"GPX not found: {rel}")
    return p


def output_file(name: str) -> Path:
    p = (OUTPUT_DIR / name).resolve()
    if p.parent != OUTPUT_DIR.resolve() or p.suffix != ".pdf" or not p.is_file():
        raise HTTPException(404)
    return p


def preset_file(name: str, must_exist=True) -> Path:
    """presets/<name>.json; names are plain text, no path parts."""
    if not re.fullmatch(r"[\w .()-]{1,80}", name) or name.strip(" .") != name:
        raise HTTPException(400, "Preset names: letters, digits, spaces and . ( ) - _ only")
    p = PRESET_DIR / f"{name}.json"
    if must_exist and not p.is_file():
        raise HTTPException(404, f"No preset {name!r}")
    return p


def move_to_trash(p: Path):
    """Deletes are recoverable: move into the macOS Trash (~/.Trash),
    renaming on a clash like Finder does. Elsewhere, delete outright."""
    trash = Path.home() / ".Trash"
    if not trash.is_dir():
        p.unlink()
        return
    dest = trash / p.name
    if dest.exists():
        dest = trash / f"{p.stem} {time.strftime('%H.%M.%S')}{p.suffix}"
    shutil.move(str(p), dest)


def parse_options(d) -> api.RenderOptions:
    try:
        return api.RenderOptions.from_dict(d)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/files")
def list_files():
    out = []
    for d in SOURCE_DIRS:
        for f in sorted(d.glob("*.gpx")) if d.is_dir() else []:
            out.append(dict(path=str(f.relative_to(PROJECT_ROOT)), name=f.name, folder=d.name,
                            size=f.stat().st_size))
    return out


@app.delete("/api/files")
def delete_file(path: str):
    move_to_trash(resolve_gpx(path))
    return dict(deleted=path)


@app.get("/api/providers")
def providers():
    def has_key(k):  # never send the key itself to the browser
        try:
            return bool(fetch_basemap.resolve_api_key(k, None))
        except fetch_basemap.BasemapFetchError:
            return False
    return [dict(id=k, needs_key=v["needs_key"], default_style=v.get("default_style"),
                 has_key=v["needs_key"] and has_key(k),
                 default=k == fetch_basemap.default_provider())
            for k, v in fetch_basemap.PROVIDERS.items()]


@app.get("/api/fonts")
def fonts():
    return api.available_fonts()


@app.post("/api/upload")
async def upload(file: UploadFile):
    name = re.sub(r"[^\w .()-]+", "_", Path(file.filename or "track.gpx").name)
    if not name.lower().endswith(".gpx"):
        raise HTTPException(400, "Only .gpx files")
    UPLOAD_DIR.mkdir(exist_ok=True)
    dest = UPLOAD_DIR / name
    dest.write_bytes(await file.read())
    return dict(path=str(dest.relative_to(PROJECT_ROOT)), name=name)


@app.post("/api/analyze")
def analyze(req: JobRequest):
    gpx = resolve_gpx(req.file)
    try:
        return api.analyze(gpx, parse_options(req.options))
    except ValueError as e:
        raise HTTPException(422, str(e))


# ---- render jobs ---------------------------------------------------------
# One render at a time: keeps tile-server load polite (see README §8) and
# matplotlib out of concurrent trouble. Jobs queue on the lock.
_jobs = {}
_render_lock = threading.Lock()


def _run_job(job_id, gpx, opts):
    job = _jobs[job_id]

    def progress(step, message):
        job.update(step=step, message=message)
        job["log"].append(message)

    with _render_lock:
        job.update(status="running", message="Starting")
        try:
            pdf = api.render(gpx, opts, progress=progress)
            job.update(status="done", step=5, message="Done", pdf=pdf.name)
        except Exception as e:
            traceback.print_exc()
            job.update(status="error", message=str(e))


def check_key(opts):
    try:  # fail before queueing, not after the parse step
        fetch_basemap.resolve_api_key(opts.provider, opts.api_key)
    except fetch_basemap.BasemapFetchError:
        env = fetch_basemap.PROVIDERS[opts.provider]["key_env_var"]
        raise HTTPException(400, f"{opts.provider} needs an API key: enter it under Map provider, "
                                 f"or set {env} before starting the server.")


def _run_batch(job_id, items, merge):
    """Render each (gpx, opts) in order; one failing track doesn't stop the
    rest (like the CLI's directory mode). Then merge what succeeded."""
    job = _jobs[job_id]
    with _render_lock:
        job.update(status="running", message="Starting")
        ok = []
        for i, (gpx, opts) in enumerate(items, 1):
            job.update(index=i, file=gpx.name, step=0, message="Starting")

            def progress(step, message):
                job.update(step=step, message=message)
                job["log"].append(f"{gpx.name}: {message}")

            try:
                job["pdfs"].append(api.render(gpx, opts, progress=progress).name)
                ok.append(gpx)
            except Exception as e:
                traceback.print_exc()
                job["failed"].append(dict(file=gpx.name, error=str(e)))
        if merge and len(ok) > 1:
            job.update(file="all tracks", step=4, message=f"Merging {len(ok)} roadbooks")
            try:
                job["merged"] = api.merge(ok, items[0][1].race_title).name
            except Exception as e:
                traceback.print_exc()
                job["failed"].append(dict(file="merged PDF", error=str(e)))
        n_bad = len(job["failed"])
        job.update(status="error" if not ok else "done", step=4,
                   message=f"{len(ok)} of {len(items)} rendered" + (f", {n_bad} failed" if n_bad else "")
                           + (f" · merged → {job['merged']}" if job["merged"] else ""))


@app.post("/api/batch")
def batch(req: BatchRequest):
    if not req.items:
        raise HTTPException(400, "Choose at least one track")
    items = [(resolve_gpx(it.file), parse_options(it.options)) for it in req.items]
    if len({g for g, _ in items}) != len(items):
        raise HTTPException(400, "A track is listed twice")
    for _, opts in items:
        check_key(opts)
    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = dict(id=job_id, kind="batch", file=items[0][0].name, status="queued",
                         index=0, n=len(items), step=0, total=4, message="Queued", log=[],
                         pdfs=[], failed=[], merged=None, merge=req.merge and len(items) > 1)
    threading.Thread(target=_run_batch, args=(job_id, items, req.merge), daemon=True).start()
    return _jobs[job_id]


@app.post("/api/render")
def render(req: JobRequest):
    gpx = resolve_gpx(req.file)
    opts = parse_options(req.options)
    check_key(opts)
    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = dict(id=job_id, file=gpx.name, status="queued", step=0, total=4,
                         message="Queued", log=[], pdf=None)
    threading.Thread(target=_run_job, args=(job_id, gpx, opts), daemon=True).start()
    return _jobs[job_id]


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Unknown job")
    return _jobs[job_id]


@app.get("/api/output")
def list_output():
    pdfs = sorted(OUTPUT_DIR.glob("*.pdf"), key=lambda f: f.stat().st_mtime, reverse=True)
    return [dict(name=f.name, size=f.stat().st_size, mtime=f.stat().st_mtime) for f in pdfs]


@app.get("/output/{name}")
def output_pdf(name: str):
    return FileResponse(output_file(name), media_type="application/pdf")


@app.delete("/api/output/{name}")
def delete_output(name: str):
    move_to_trash(output_file(name))
    return dict(deleted=name)


@app.get("/api/presets")
def list_presets():
    return sorted((f.stem for f in PRESET_DIR.glob("*.json")), key=str.lower) if PRESET_DIR.is_dir() else []


@app.get("/api/presets/{name}")
def get_preset(name: str):
    return json.loads(preset_file(name).read_text())


@app.put("/api/presets/{name}")
def save_preset(name: str, options: dict):
    p = preset_file(name, must_exist=False)
    options.pop("api_key", None)  # keys stay in env / config.local.json, never in presets
    PRESET_DIR.mkdir(exist_ok=True)
    p.write_text(json.dumps(options, indent=2))
    return dict(saved=name)


@app.delete("/api/presets/{name}")
def delete_preset(name: str):
    move_to_trash(preset_file(name))
    return dict(deleted=name)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
