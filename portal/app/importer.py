"""Data import: upload a raw file as received, check it, then load it into the database.

The portal does not re-implement any conversion rule. It runs the same converters (tools/*.py) that were built for these files, so every rule
decided for the templates - column mapping, statuses, data-quality flags, manual-override protection, upper-case text, mass-removal stop - applies
whether a file is loaded from the command line or from here. Two steps, both explicit:
  1. CHECK  - the converter runs without touching the database and its report is shown.
  2. LOAD   - after a safety backup, the converter runs again and loads the database.
"""
import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from . import backup, config, db

TOOLS = config.PROJECT_ROOT / "tools"
UPLOADS = Path(os.environ.get("PORTAL_UPLOAD_DIR") or config.PROJECT_ROOT / "uploads")
MASTERS = config.PROJECT_ROOT / "masters"
MAX_BYTES = 60 * 1024 * 1024

KINDS = {
    "assets": {"label": "Asset master (IT inventory workbook)", "hint": "The multi-sheet IT-IMMDSS asset inventory, exactly as received.", "table": "asset"},
    "hr": {"label": "Employee master (HR / SAP manpower export)", "hint": "The monthly Ank-manpower export, exactly as received.", "table": "employee"},
    "cipl": {"label": "CIPL employee roster", "hint": "The CIPL Employee Details workbook. Aadhaar, bank and UAN columns are never read.", "table": "cipl_employee"},
    "calls": {"label": "Call tracker (calls, inward, outward)", "hint": "The CIPL Open - Closed calls tracker workbook.", "table": "svc_call"},
    "rma": {"label": "OEM RMA log", "hint": "The SD-WAN OEM workbook. Only its 'Call Log' sheet is read; its password sheet is never opened.", "table": "oem_rma"},
}

DDL = ["""CREATE TABLE IF NOT EXISTS portal_import (
          job_id TEXT PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now(), kind TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes BIGINT, as_of DATE,
          uploaded_by TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('UPLOADED','CHECKED','CHECK_FAILED','LOADED','LOAD_FAILED')), check_log TEXT, load_log TEXT,
          loaded_at TIMESTAMPTZ, loaded_by TEXT, safety_backup TEXT)"""]


class ImportError_(Exception):
    http_error = True

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _python():
    exe = getattr(sys, "_base_executable", None) or sys.executable     # the interpreter the virtual environment was made from (it has pandas)
    r = subprocess.run([exe, "-c", "import pandas, openpyxl, psycopg"], capture_output=True, timeout=60)
    if r.returncode != 0:
        raise ImportError_("The Python installation that runs the converters does not have pandas, openpyxl and psycopg. Install them there (pip install pandas openpyxl psycopg[binary]).", 500)
    return exe


def kinds():
    return [{"key": k, **{a: b for a, b in v.items() if a != "table"}} for k, v in KINDS.items()]


def save_upload(kind, filename, data, user):
    if kind not in KINDS:
        raise ImportError_("Choose what kind of file this is.")
    name = re.sub(r"[^\w .()-]", "_", Path(filename or "upload.xlsx").name)[:120]
    if not name.lower().endswith((".xlsx", ".xlsm")):
        raise ImportError_("Only Excel workbooks (.xlsx) can be imported.")
    if not data or len(data) > MAX_BYTES:
        raise ImportError_(f"The file is empty or larger than {MAX_BYTES // (1024 * 1024)} MB.")
    if data[:2] != b"PK":
        raise ImportError_("That file is not an Excel workbook (it is damaged or a different format).")
    job = uuid.uuid4().hex[:12]
    folder = UPLOADS / job
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    with db.write() as con:
        con.execute("INSERT INTO portal_import (job_id, kind, filename, sha256, size_bytes, uploaded_by, status) VALUES (%s,%s,%s,%s,%s,%s,'UPLOADED')", (job, kind, name, sha, len(data), user))
        dup = con.execute("SELECT at, kind FROM portal_import WHERE sha256 = %s AND status = 'LOADED' ORDER BY at DESC LIMIT 1", (sha,)).fetchone()
    return {"job_id": job, "filename": name, "size_bytes": len(data), "already_loaded": {"at": dup[0], "kind": dup[1]} if dup else None}


def _cmd(kind, path, out_dir, as_of, load, force):
    py = _python()
    a = ["--as-of", as_of] if as_of else []
    if kind == "assets":
        c = [py, str(TOOLS / "inventory_to_master.py"), "--raw", str(path), "--out-dir", str(out_dir), *a]
        return c + ([] if load else ["--no-load-db"]) + (["--force"] if force else [])
    if kind == "cipl":
        c = [py, str(TOOLS / "cipl_roster.py"), "--raw", str(path), "--out-dir", str(out_dir), *a]
        return c + ([] if load else ["--no-load-db"]) + (["--force"] if force else [])
    if kind in ("calls", "rma"):
        c = [py, str(TOOLS / "call_tracking.py"), "tracker" if kind == "calls" else "rma", "--raw", str(path), "--out-dir", str(out_dir), *a]
        return c + ([] if load else ["--no-load-db"]) + (["--force"] if force else [])
    return [py, str(TOOLS / "hr_export_to_template.py"), "--raw", str(path), "--out-dir", str(out_dir)]


def _run(cmd, timeout=600):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    pw = db.password()
    if pw:
        env["PGPASSWORD"] = pw
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(config.PROJECT_ROOT), env=env, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr.strip() else "")
    return r.returncode, out[-6000:]


def _job(job_id):
    j = db.one("SELECT * FROM portal_import WHERE job_id = %s", [job_id])
    if not j or not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise ImportError_("Import not found.", 404)
    return j


def check(job_id, as_of=None):
    j = _job(job_id)
    src = UPLOADS / job_id / j["filename"]
    if not src.exists():
        raise ImportError_("The uploaded file is no longer on disk. Upload it again.", 410)
    out_dir = UPLOADS / job_id / "check"
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    code, log = _run(_cmd(j["kind"], src, out_dir, as_of, load=False, force=False))
    status = "CHECKED" if code == 0 else "CHECK_FAILED"
    with db.write() as con:
        con.execute("UPDATE portal_import SET status = %s, check_log = %s, as_of = %s WHERE job_id = %s", (status, log, dt.date.fromisoformat(as_of) if as_of else None, job_id))
    return {"job_id": job_id, "ok": code == 0, "log": log, "outputs": sorted(p.name for p in out_dir.glob("*"))}


def load(job_id, user, as_of=None, force=False):
    j = _job(job_id)
    if j["status"] not in ("CHECKED", "LOAD_FAILED"):
        raise ImportError_("Check the file first - a file that has not passed the check cannot be loaded.", 409)
    src = UPLOADS / job_id / j["filename"]
    if not src.exists():
        raise ImportError_("The uploaded file is no longer on disk. Upload it again.", 410)
    safety = backup.run_backup("PRE_IMPORT", user, f"before importing {j['filename']}")
    MASTERS.mkdir(exist_ok=True)
    code, log = _run(_cmd(j["kind"], src, MASTERS, as_of or (j["as_of"].isoformat() if j["as_of"] else None), load=True, force=force))
    if code == 0 and j["kind"] == "hr":
        made = sorted(MASTERS.glob("Employee_Master_Upload_*.xlsx"), key=lambda p: p.stat().st_mtime)
        if not made:
            code, log = 1, log + "\nNo employee master workbook was produced."
        else:
            code, log2 = _run([_python(), str(TOOLS / "master_db.py"), "load", "--file", str(made[-1])])
            log += "\n" + log2
    status = "LOADED" if code == 0 else "LOAD_FAILED"
    with db.write() as con:
        con.execute("UPDATE portal_import SET status = %s, load_log = %s, loaded_at = now(), loaded_by = %s, safety_backup = %s WHERE job_id = %s", (status, log, user, safety["file"], job_id))
    return {"job_id": job_id, "ok": code == 0, "log": log, "safety_backup": safety["file"]}


def history(limit=50):
    return db.query("SELECT job_id, at, kind, filename, size_bytes, as_of, uploaded_by, status, loaded_at, loaded_by, safety_backup FROM portal_import ORDER BY at DESC LIMIT %s", [limit])


def detail(job_id):
    j = _job(job_id)
    return {k: j[k] for k in ("job_id", "kind", "filename", "status", "check_log", "load_log", "as_of", "safety_backup")}
