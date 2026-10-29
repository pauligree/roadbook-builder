"""
config.py — project paths and config.local.json, shared by every step.

WORK_DIR holds the regenerable intermediates (parsed GPX cache, basemap
JPGs, un-merged PDFs). It defaults to <project>/work; to put it elsewhere
(e.g. off a synced folder, or on a bigger disk), first match wins:
    ROADBOOK_WORK_DIR=/some/dir      environment variable
    {"work_dir": "/some/dir"}        top-level key in config.local.json
Relative paths resolve against the project root; ~ is expanded. The
environment variable also reaches render_pages.py, which run_pipeline.py
starts as a subprocess.
"""
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_CONFIG_PATH = PROJECT_ROOT / "config.local.json"


def _load_local_config():
    try:
        return json.loads(LOCAL_CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


LOCAL_CONFIG = _load_local_config()


def _work_dir():
    raw = os.environ.get("ROADBOOK_WORK_DIR") or LOCAL_CONFIG.get("work_dir") or "work"
    return (PROJECT_ROOT / Path(raw).expanduser()).resolve()


WORK_DIR = _work_dir()
