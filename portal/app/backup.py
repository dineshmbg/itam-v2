"""Database backup and restore (PostgreSQL custom-format dumps).

Backups are ordinary `pg_dump -Fc` files, so they can also be restored with `pg_restore` outside the portal. Each backup gets a SHA-256 recorded in
portal_backup; a restore first verifies the file, then takes a safety backup of the current data, and only then replaces it.
"""
import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import config, db

BACKUP_DIR = Path(os.environ.get("PORTAL_BACKUP_DIR") or config.PROJECT_ROOT / "backups")
# Optional second location (another disk or a network share) that every successful backup is also copied to, so one failed disk cannot
# take the database and all its backups together. Off unless set; a failed copy is reported in the backup's note, never fatal.
COPY_DIR = os.environ.get("PORTAL_BACKUP_COPY_DIR")
DDL = ["""CREATE TABLE IF NOT EXISTS portal_backup (
          backup_id SERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now(), kind TEXT NOT NULL, file TEXT NOT NULL, size_bytes BIGINT, sha256 TEXT,
          ok BOOLEAN NOT NULL DEFAULT TRUE, made_by TEXT, note TEXT)"""]


class BackupError(Exception):
    http_error = True


def pg_tool(name):
    exe = name + (".exe" if os.name == "nt" else "")
    if os.environ.get("PG_BIN") and (Path(os.environ["PG_BIN"]) / exe).exists():
        return str(Path(os.environ["PG_BIN"]) / exe)
    found = shutil.which(exe)
    if found:
        return found
    for base in (Path(r"C:\Program Files\PostgreSQL"), Path("/usr/lib/postgresql")):
        if base.exists():
            for v in sorted(base.iterdir(), reverse=True):
                for cand in (v / "bin" / exe,):
                    if cand.exists():
                        return str(cand)
    raise BackupError(f"{exe} was not found. Install PostgreSQL client tools or set PG_BIN to the folder that contains them.")


def _env():
    e = dict(os.environ)
    pw = db.password()
    if pw:
        e["PGPASSWORD"] = pw
    return e


def _conn_args():
    return ["-h", config.PG["host"], "-p", str(config.PG["port"]), "-U", config.PG["user"]]


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_backup(kind="MANUAL", by=None, note=None):
    if kind not in ("MANUAL", "AUTO", "PRE_RESTORE", "PRE_IMPORT"):
        raise BackupError("Unknown backup kind.")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{config.PG['dbname']}_{dt.datetime.now():%Y%m%d_%H%M%S}_{kind.lower()}.dump"
    path = BACKUP_DIR / name
    cmd = [pg_tool("pg_dump"), *_conn_args(), "-d", config.PG["dbname"], "-Fc", "-Z", "6", "--no-owner", "-f", str(path)]
    ok, err = True, None
    try:
        r = subprocess.run(cmd, env=_env(), capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            ok, err = False, (r.stderr or "pg_dump failed")[-400:]
    except subprocess.TimeoutExpired:
        ok, err = False, "pg_dump timed out"
    size = path.stat().st_size if path.exists() else 0
    sha = _sha(path) if ok and size else None
    if ok and not size:
        ok, err = False, "backup file is empty"
    if ok and COPY_DIR:
        try:
            Path(COPY_DIR).mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, Path(COPY_DIR) / name)
            copy_note = f"also copied to {COPY_DIR}"
        except OSError as e:
            copy_note = f"NOT copied to {COPY_DIR}: {e}"[:300]
        note = f"{note} | {copy_note}" if note else copy_note
    with db.write() as con:
        con.execute("INSERT INTO portal_backup (kind, file, size_bytes, sha256, ok, made_by, note) VALUES (%s,%s,%s,%s,%s,%s,%s)", (kind, name, size, sha, ok, by, note or err))
    if not ok:
        path.unlink(missing_ok=True)
        raise BackupError(err)
    return {"file": name, "size_bytes": size, "sha256": sha}


def list_backups():
    rows = db.query("SELECT backup_id AS id, at, kind, file, size_bytes, sha256, ok, made_by, note FROM portal_backup ORDER BY backup_id DESC LIMIT 200")
    for r in rows:
        r["exists"] = (BACKUP_DIR / r["file"]).exists()
    return rows


def _resolve(file):
    if not re.fullmatch(r"[\w.-]+\.dump", file or "") or "/" in file or "\\" in file:
        raise BackupError("Invalid backup name.")
    p = BACKUP_DIR / file
    if not p.exists():
        raise BackupError("That backup file is no longer on disk.")
    return p


def verify(file):
    """Checksum matches the record and pg_restore can read the archive. Returns table count."""
    p = _resolve(file)
    rec = db.one("SELECT sha256 FROM portal_backup WHERE file = %s", [file])
    if rec and rec["sha256"] and _sha(p) != rec["sha256"]:
        raise BackupError("Checksum mismatch: the file changed after it was created.")
    r = subprocess.run([pg_tool("pg_restore"), "--list", str(p)], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise BackupError("The archive cannot be read: " + (r.stderr or "")[-200:])
    return {"ok": True, "tables": len(re.findall(r"^\d+; \d+ \d+ TABLE DATA ", r.stdout, re.M))}


def prune(keep_auto):
    """Keep the newest `keep_auto` automatic backups. Manual and pre-restore backups are never removed automatically."""
    old = db.query("SELECT backup_id, file FROM portal_backup WHERE kind = 'AUTO' AND ok ORDER BY backup_id DESC OFFSET %s", [keep_auto])
    for r in old:
        (BACKUP_DIR / r["file"]).unlink(missing_ok=True)
        with db.write() as con:
            con.execute("UPDATE portal_backup SET note = coalesce(note || ' | ', '') || 'file removed by retention' WHERE backup_id = %s", (r["backup_id"],))
    return len(old)


def restore(file, by):
    """Replace the live data with a backup. A safety backup of the current data is taken first; the restore itself runs in one transaction."""
    p = _resolve(file)
    verify(file)
    safety = run_backup("PRE_RESTORE", by, f"safety copy before restoring {file}")
    cmd = [pg_tool("pg_restore"), *_conn_args(), "-d", config.PG["dbname"], "--clean", "--if-exists", "--no-owner", "--single-transaction", str(p)]
    r = subprocess.run(cmd, env=_env(), capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise BackupError("Restore failed and was rolled back; current data is unchanged. " + (r.stderr or "")[-300:])
    with db.write() as con:      # the restored data predates the safety copy, so its record has to be re-added
        con.execute("INSERT INTO portal_backup (kind, file, size_bytes, sha256, made_by, note) SELECT 'PRE_RESTORE', %s, %s, %s, %s, %s WHERE NOT EXISTS (SELECT 1 FROM portal_backup WHERE file = %s)",
                    (safety["file"], safety["size_bytes"], safety["sha256"], by, f"safety copy before restoring {file}", safety["file"]))
    return {"restored": file, "safety_backup": safety["file"]}
