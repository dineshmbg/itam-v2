"""Engineer documents (cancelled cheque, medical card, ESIC card, gate pass, other): stored in PostgreSQL so backups, restores and release rollbacks
cover them. Who may see/upload: an administrator, a Team Leader/SI, and the engineer themselves (their own only). Nothing is ever erased -
"remove" and "replace" mark the old row superseded and keep its bytes. File contents never leave through any list, register or export."""
import datetime as dt
import hashlib
import re

from . import db

MAX_BYTES = 10 * 1024 * 1024
SOON_DAYS = 30
TYPES = {"CANCELLED_CHEQUE": "Cancelled cheque", "MEDICAL_CARD": "Medical card", "ESIC_CARD": "ESIC card", "GATE_PASS": "Gate pass", "OTHER": "Other"}
SIGNATURES = [(b"%PDF-", "application/pdf", "pdf"), (b"\xff\xd8\xff", "image/jpeg", "jpg"), (b"\x89PNG\r\n\x1a\n", "image/png", "png")]

DDL = [
    """CREATE TABLE IF NOT EXISTS portal_engineer_doc (
         doc_id BIGSERIAL PRIMARY KEY, engineer_key TEXT NOT NULL, doc_type TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
         filename TEXT NOT NULL, mime TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, content BYTEA NOT NULL,
         expiry_date DATE, uploaded_by TEXT NOT NULL, uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         superseded_at TIMESTAMPTZ, superseded_by TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_engdoc_eng ON portal_engineer_doc (engineer_key, uploaded_at DESC)",
]


class DocError(Exception):
    http_error = True

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def can_access(user, key):
    """Never for the read-only demo account: it has a public password, and these are bank and ID documents."""
    if user.get("read_only"):
        return False
    return user["role"] == "ADMIN" or bool(user.get("lead_tools")) or (bool(user.get("engineer_key")) and user["engineer_key"] == key)


def can_remove(user):
    return not user.get("read_only") and (user["role"] == "ADMIN" or bool(user.get("lead_tools")))


def _status(expiry):
    if not expiry:
        return None
    left = (expiry - dt.date.today()).days
    return "EXPIRED" if left < 0 else "SOON" if left <= SOON_DAYS else None


def listing(key):
    rows = db.query("""SELECT doc_id, doc_type, label, filename, mime, size_bytes, expiry_date, uploaded_by, uploaded_at, superseded_at, superseded_by
                       FROM portal_engineer_doc WHERE engineer_key = %s ORDER BY uploaded_at DESC, doc_id DESC""", [key])
    for r in rows:
        r["type_label"] = TYPES.get(r["doc_type"], r["doc_type"])
        r["expiry_status"] = None if r["superseded_at"] else _status(r["expiry_date"])
    return {"types": [{"id": k, "label": v} for k, v in TYPES.items()], "max_mb": MAX_BYTES // (1024 * 1024), "docs": rows}


def _detect(data):
    for sig, mime, ext in SIGNATURES:
        if data.startswith(sig):
            return mime, ext
    raise DocError("Only PDF, JPG or PNG files are accepted.", 415)


def _name(raw, ext):
    base = re.split(r"[\\/]", raw or "")[-1]
    stem = re.sub(r"[^\w.\- ()]", "_", base.rsplit(".", 1)[0]).strip(" .") or "document"
    return f"{stem[:100]}.{ext}"


def add(key, doc_type, label, expiry, filename, data, by):
    doc_type = (doc_type or "").strip().upper()
    if doc_type not in TYPES:
        raise DocError("Choose a document type.")
    label = re.sub(r"\s+", " ", (label or "").strip()).upper()[:60]
    if doc_type == "OTHER" and not label:
        raise DocError("Describe what this document is (for example EPFO or ID proof).")
    if doc_type != "OTHER":
        label = ""
    if not data:
        raise DocError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise DocError(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB.", 413)
    try:
        exp = dt.date.fromisoformat(expiry) if expiry else None
    except ValueError:
        raise DocError("The expiry date is not a valid date.")
    mime, ext = _detect(data)
    fname = _name(filename, ext)
    with db.write() as con:
        if not con.execute("SELECT 1 FROM portal_engineer WHERE engineer_key = %s", (key,)).fetchone():
            raise DocError("No such engineer.", 404)
        con.execute("""UPDATE portal_engineer_doc SET superseded_at = now(), superseded_by = %s
                       WHERE engineer_key = %s AND doc_type = %s AND label = %s AND superseded_at IS NULL""", (by, key, doc_type, label))
        row = con.execute("""INSERT INTO portal_engineer_doc (engineer_key, doc_type, label, filename, mime, size_bytes, sha256, content, expiry_date, uploaded_by)
                             VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING doc_id""",
                          (key, doc_type, label, fname, mime, len(data), hashlib.sha256(data).hexdigest(), data, exp, by)).fetchone()
    return {"doc_id": row[0], "filename": fname, "type": doc_type, "size": len(data)}


def fetch(key, doc_id):
    r = db.one("SELECT filename, mime, content, doc_type FROM portal_engineer_doc WHERE doc_id = %s AND engineer_key = %s", [doc_id, key])
    if not r:
        raise DocError("No such document.", 404)
    return r["filename"], r["mime"], bytes(r["content"]), r["doc_type"]


def remove(key, doc_id, by):
    with db.write() as con:
        n = con.execute("UPDATE portal_engineer_doc SET superseded_at = now(), superseded_by = %s WHERE doc_id = %s AND engineer_key = %s AND superseded_at IS NULL",
                        (by, doc_id, key)).rowcount
    if not n:
        raise DocError("No such current document.", 404)
