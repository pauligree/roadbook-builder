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
    GET  /api/jobs/{job}         render progress / result
    GET  /api/output             finished roadbook PDFs
    GET  /output/{name}          serve a finished PDF
"""
import re
import threading
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

app = FastAPI(title="Roadbook Builder")


class JobRequest(BaseModel):
    file: str
    options: dict = {}


def resolve_gpx(rel: str) -> Path:
    """Map a client-supplied relative path to a GPX inside SOURCE_DIRS only."""
    p = (PROJECT_ROOT / rel).resolve()
    if p.suffix.lower() != ".gpx" or not p.is_file() or not any(p.is_relative_to(d) for d in SOURCE_DIRS):
        raise HTTPException(404, f"GPX not found: {rel}")
    return p


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


@app.get("/api/providers")
def providers():
    return [dict(id=k, needs_key=v["needs_key"], default_style=v.get("default_style"))
            for k, v in fetch_basemap.PROVIDERS.items()]


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


@app.post("/api/render")
def render(req: JobRequest):
    gpx = resolve_gpx(req.file)
    opts = parse_options(req.options)
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
    p = (OUTPUT_DIR / name).resolve()
    if p.parent != OUTPUT_DIR.resolve() or p.suffix != ".pdf" or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/pdf")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
