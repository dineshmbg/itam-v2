"""Remove assets from AMC: a controlled, audited request instead of a bare status edit.

A removal is a change to the AMC cover, approved verbally by a named portal user and recorded here. Applying it ARCHIVES the asset
(is_current = 0 plus the archive lock - the same undoable mechanism as the register's Archive): the asset leaves the Assets register and every
count, PM list and cover alert, and its engineer's counts, but nothing is deleted. What the asset looked like (rate, cover, PM state, status)
is saved on the request line first, so a reinstatement can put it back and the billing credit can be worked out.
Each request is one transaction: either every asset in it is removed or none is.
"""
import datetime as dt
import re

from . import db, edit, pm
from .edit import Conflict, Invalid, NotFound
import itam_locks as L  # noqa: E402  (tools/ is on sys.path - see edit.py)

DDL = [
    """CREATE TABLE IF NOT EXISTS amc_removal (
         removal_no TEXT PRIMARY KEY, quarter_label TEXT NOT NULL, seq INT NOT NULL, effective_date DATE NOT NULL,
         requested_by TEXT NOT NULL, approved_by TEXT NOT NULL, approved_on DATE NOT NULL,       -- approved_by: the portal user who gave the verbal approval
         status TEXT NOT NULL DEFAULT 'APPLIED' CHECK (status IN ('APPLIED','REVERSED')), note TEXT,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (quarter_label, seq))""",
    """CREATE TABLE IF NOT EXISTS amc_removal_line (
         line_id BIGSERIAL PRIMARY KEY, removal_no TEXT NOT NULL REFERENCES amc_removal(removal_no), asset_key TEXT NOT NULL, reason TEXT NOT NULL,
         snapshot JSONB NOT NULL, override_note TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_amc_removal_line_asset ON amc_removal_line (asset_key)",
]

REASONS = {"DECOMMISSIONED": "Decommissioned / retired", "SURPLUS": "Surplus", "TRANSFERRED": "Transferred to another site", "REPLACED": "Replaced",
           "WARRANTY": "Moved to warranty", "LOST_WRITTEN_OFF": "Lost, stolen or written off", "END_OF_LIFE": "End of life / support",
           "SCOPE_CHANGE": "Contract scope change", "WRONGLY_INCLUDED": "Wrongly included (data correction)"}
SNAP = ("asset_status", "cover_type", "cover_expiry_date", "rate_value", "pm_status")
TODAY = edit.TODAY


def _date(raw, quarter_start):
    try:
        d = dt.date.fromisoformat(str(raw)[:10]) if raw not in (None, "") else quarter_start
    except ValueError:
        raise Invalid("Enter a valid date.", {"effective_date": "enter a valid date (YYYY-MM-DD)"}) from None
    if d < quarter_start:
        raise Invalid(f"The effective date cannot be before the running quarter starts ({quarter_start}).", {"effective_date": f"not before {quarter_start}"})
    if d > TODAY():
        raise Invalid("A future effective date is not supported yet. Use today or an earlier date.", {"effective_date": "not after today"})
    return d


def _ineligible(row):
    """Why this asset cannot be removed from AMC, or None."""
    if row["is_current"] != 1:
        return "it is archived or not current"
    if row["record_level"] != "ASSET":
        return "it is a component line, not a whole asset (remove its parent)"
    if row["asset_class"] == "LAPTOP":
        return "laptops are outside AMC scope"
    if row.get("cover_type") != "AMC":
        return "it is not covered by AMC"
    if row.get("asset_status") == "REMOVED_FROM_AMC":
        return "it is already removed from AMC"
    return None


def preflight(key):
    """What the removal dialog shows for one asset before anything is changed. Read only."""
    row = db.one("SELECT * FROM asset WHERE asset_key = %s", [key])
    if not row:
        raise NotFound(f"Asset '{key}' was not found.")
    calls = db.one("SELECT count(*) AS n FROM svc_call WHERE asset_key = %s AND is_current = 1 AND call_status = 'OPEN'", [key])["n"]
    rma = db.one("SELECT count(*) AS n FROM oem_rma WHERE asset_key = %s AND is_current = 1 AND coalesce(return_status, 'PENDING') <> 'RETURNED'", [key])["n"]
    comps = db.one("SELECT count(*) AS n FROM asset WHERE parent_asset_key = %s AND is_current = 1", [key])["n"]
    q = pm.quarter(TODAY())
    return {"key": key, "blocked": _ineligible(row), "open_calls": calls, "open_rma": rma, "components": comps,
            "asset": {f: row.get(f) for f in ("asset_key", "asset_class", "asset_type", "make", "model", "serial_no", "engineer_name", "location_code", *SNAP)},
            "quarter": {"label": q["label"], "start": q["start"], "end": q["end"]}, "default_date": q["start"],
            "reasons": [{"value": k, "label": v} for k, v in REASONS.items()],
            "approvers": [{"username": r["username"], "display_name": r["display_name"]} for r in db.query("SELECT username, display_name FROM portal_user WHERE active ORDER BY display_name")]}


def remove(keys, body, editor, ip):
    """Remove the assets `keys` from AMC in one request. body: reason (a REASONS key, or {asset_key: reason}), approved_by (portal username),
    effective_date, note, override_note (needed when an asset still has open calls / RMA cases). Returns {"removal_no", "assets"}."""
    body = body or {}
    keys = list(dict.fromkeys(str(k).strip().upper() for k in (keys or []) if str(k).strip()))
    if not keys:
        raise Invalid("Choose at least one asset.", {"keys": "required"})
    reasons = body.get("reason")
    reasons = reasons if isinstance(reasons, dict) else {k: reasons for k in keys}
    reasons = {k: str(reasons.get(k) or "").upper() for k in keys}
    if any(r not in REASONS for r in reasons.values()):
        raise Invalid("Give a reason for every asset.", {"reason": "required"})
    note = re.sub(r"\s+", " ", str(body.get("note") or "").strip())[:300] or None
    override = re.sub(r"\s+", " ", str(body.get("override_note") or "").strip())[:300] or None
    q = pm.quarter(TODAY())
    when = _date(body.get("effective_date"), q["start"])
    approver = str(body.get("approved_by") or "").strip()
    with db.write() as con:
        if not approver or not edit._exists(con, "SELECT 1 FROM portal_user WHERE username = %s AND active", (approver,)):
            raise Invalid("Choose the portal user who gave the verbal approval.", {"approved_by": "required - pick an active portal user"})
        rows = {r["asset_key"]: r for r in edit._rows(con, "SELECT * FROM asset WHERE asset_key = ANY(%s) ORDER BY asset_key FOR UPDATE", (keys,))}
        problems, warnings = [], []
        for k in keys:
            r = rows.get(k)
            why = "it was not found" if not r else _ineligible(r)
            if why:
                problems.append(f"{k}: {why}")
                continue
            if edit._exists(con, "SELECT 1 FROM asset WHERE parent_asset_key = %s AND is_current = 1", (k,)):
                problems.append(f"{k}: it has component lines - they cannot be removed yet (coming in a later step)")
            calls = edit._rows(con, "SELECT count(*) AS n FROM svc_call WHERE asset_key = %s AND is_current = 1 AND call_status = 'OPEN'", (k,))[0]["n"]
            rma = edit._rows(con, "SELECT count(*) AS n FROM oem_rma WHERE asset_key = %s AND is_current = 1 AND coalesce(return_status, 'PENDING') <> 'RETURNED'", (k,))[0]["n"]
            if (calls or rma) and not (override and len(override) >= 5):
                warnings.append(f"{k}: {calls} open call(s), {rma} OEM RMA case(s) not returned")
        if problems:
            raise Conflict("Nothing was removed. " + "; ".join(problems))
        if warnings:
            raise Conflict("Nothing was removed. " + "; ".join(warnings) + ". Give an override note (at least 5 characters) to remove them anyway.")
        con.execute("SELECT pg_advisory_xact_lock(hashtext('amc_removal'))")          # two requests at once must not take the same number
        seq = edit._rows(con, "SELECT coalesce(max(seq), 0) + 1 AS n FROM amc_removal WHERE quarter_label = %s", (q["label"],))[0]["n"]
        fy = q["start"].year if q["start"].month >= 4 else q["start"].year - 1
        no = f"AMCR-{fy}{q['label'][:2]}-{seq:03d}"
        con.execute("INSERT INTO amc_removal (removal_no, quarter_label, seq, effective_date, requested_by, approved_by, approved_on, note) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (no, q["label"], seq, when, editor, approver, TODAY(), note))
        for k in keys:
            r = rows[k]
            snap = {f: edit._jd(r.get(f)) for f in SNAP}
            con.execute("INSERT INTO amc_removal_line (removal_no, asset_key, reason, snapshot, override_note) VALUES (%s,%s,%s,%s::jsonb,%s)",
                        (no, k, reasons[k], edit.json.dumps(snap), override))
            edit._update_row(con, "assets", k, {"asset_status": "REMOVED_FROM_AMC", "rate_value": None, "is_current": 0})
            con.execute("""INSERT INTO portal_lock (dataset, record_key, field, value, editor) VALUES ('assets',%s,%s,'true'::jsonb,%s)
                           ON CONFLICT (dataset, record_key, field) DO UPDATE SET editor = EXCLUDED.editor, edited_at = now()""", (k, L.ARCHIVED, editor))
            edit._audit(con, editor, ip, "assets", k, "AMC_REMOVE",
                        {"asset_status": {"old": r.get("asset_status"), "new": "REMOVED_FROM_AMC"}, "rate_value": {"old": edit._jd(r.get("rate_value")), "new": None},
                         "removal_no": no, "reason": reasons[k], "effective_date": when.isoformat(), "approved_by": approver}, note)
    return {"removal_no": no, "assets": keys}
