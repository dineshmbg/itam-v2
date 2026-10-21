"""Remove assets from AMC: a controlled, audited request instead of a bare status edit.

A removal is a change to the AMC cover, approved verbally by a named portal user and recorded here. Applying it ARCHIVES the asset
(is_current = 0 plus the archive lock - the same undoable mechanism as the register's Archive): the asset leaves the Assets register and every
count, PM list and cover alert, and its engineer's counts, but nothing is deleted. What the asset looked like (rate, cover, PM state, status)
is saved on the request line first, so a reinstatement can put it back and the billing credit can be worked out.
Each request is one transaction: either every asset in it is removed or none is.
"""
import datetime as dt
import re
from decimal import ROUND_HALF_UP, Decimal

from . import db, edit, pm
from .edit import Conflict, Invalid, NotFound
import itam_locks as L  # noqa: E402  (tools/ is on sys.path - see edit.py)

DDL = [
    """CREATE TABLE IF NOT EXISTS amc_removal (
         removal_no TEXT PRIMARY KEY, quarter_label TEXT NOT NULL, seq INT NOT NULL, effective_date DATE NOT NULL,
         requested_by TEXT NOT NULL, approved_by TEXT NOT NULL, approved_on DATE NOT NULL,       -- approved_by: the portal user who gave the verbal approval
         status TEXT NOT NULL DEFAULT 'PENDING', note TEXT,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (quarter_label, seq))""",
    """CREATE TABLE IF NOT EXISTS amc_removal_line (
         line_id BIGSERIAL PRIMARY KEY, removal_no TEXT NOT NULL REFERENCES amc_removal(removal_no), asset_key TEXT NOT NULL, reason TEXT NOT NULL,
         snapshot JSONB, override_note TEXT)""",
    "CREATE INDEX IF NOT EXISTS ix_amc_removal_line_asset ON amc_removal_line (asset_key)",
    # PENDING: waiting for its effective date. The snapshot and the credit are taken when it is applied. SKIPPED lines were no longer eligible then.
    "ALTER TABLE amc_removal DROP CONSTRAINT IF EXISTS amc_removal_status_check",
    "ALTER TABLE amc_removal ADD CONSTRAINT amc_removal_status_check CHECK (status IN ('PENDING','APPLIED','REVERSED','CANCELLED'))",
    "ALTER TABLE amc_removal ALTER COLUMN status SET DEFAULT 'PENDING'",
    "ALTER TABLE amc_removal_line ALTER COLUMN snapshot DROP NOT NULL",
    "ALTER TABLE amc_removal_line ADD COLUMN IF NOT EXISTS credit_days INT",
    "ALTER TABLE amc_removal_line ADD COLUMN IF NOT EXISTS credit_amount NUMERIC(14,2)",
    "ALTER TABLE amc_removal_line ADD COLUMN IF NOT EXISTS skipped_reason TEXT",
    "ALTER TABLE amc_removal_line ADD COLUMN IF NOT EXISTS via_parent TEXT",       # a component line removed together with this parent asset
]

REASONS = {"DECOMMISSIONED": "Decommissioned / retired", "SURPLUS": "Surplus", "TRANSFERRED": "Transferred to another site", "REPLACED": "Replaced",
           "WARRANTY": "Moved to warranty", "LOST_WRITTEN_OFF": "Lost, stolen or written off", "END_OF_LIFE": "End of life / support",
           "SCOPE_CHANGE": "Contract scope change", "WRONGLY_INCLUDED": "Wrongly included (data correction)"}
SNAP = ("asset_status", "cover_type", "cover_expiry_date", "rate_value", "pm_status")
TODAY = edit.TODAY


def _date(raw, q):
    """Effective date: any day of the running quarter (default its first day). Past days are normal - the removal is back-dated."""
    try:
        d = dt.date.fromisoformat(str(raw)[:10]) if raw not in (None, "") else q["start"]
    except ValueError:
        raise Invalid("Enter a valid date.", {"effective_date": "enter a valid date (YYYY-MM-DD)"}) from None
    if d < q["start"] or d > q["end"]:
        raise Invalid(f"The effective date must fall in the running quarter ({q['start']} to {q['end']}).", {"effective_date": f"between {q['start']} and {q['end']}"})
    return d


def billing(rate, effective):
    """What the vendor owes back for one asset removed on `effective`. The rate is per QUARTER, so it is pro-rated over the days of that
    quarter from the effective date to its last day: removed on the first day = the whole quarterly rate."""
    q = pm.quarter(effective)
    days, total = (q["end"] - effective).days + 1, (q["end"] - q["start"]).days + 1
    amount = None if rate is None else (Decimal(str(rate)) * days / total).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return {"days": days, "quarter_days": total, "amount": amount}


def _ineligible(row):
    """Why this asset cannot be removed from AMC, or None."""
    if row["is_current"] != 1:
        return "it is archived or not current"
    if row["record_level"] not in ("ASSET", "COMPONENT"):
        return "it is not an asset or a component line"
    if row["asset_class"] == "LAPTOP":
        return "laptops are outside AMC scope"
    if row.get("cover_type") != "AMC":
        return "it is not covered by AMC"
    if row.get("asset_status") == "REMOVED_FROM_AMC":
        return "it is already removed from AMC"
    return None


def _expand(q, keys):
    """Every line a request needs, parents first: the chosen keys, each followed by its component lines (monitor, GPU ... billed under it and
    leaving with it). A component chosen on its own stays a line of its own, unless its parent is chosen too (then it follows the parent).
    `q(sql, params)` returns rows. -> [(asset_key, via_parent or None)]"""
    chosen = set(keys)
    own = {r["asset_key"]: r["parent_asset_key"] for r in q("SELECT asset_key, parent_asset_key FROM asset WHERE asset_key = ANY(%s)", (keys,))}
    out = []
    for k in keys:
        if own.get(k) in chosen:
            continue                                          # it follows its parent below
        out.append((k, None))
        out += [(r["asset_key"], k) for r in q("SELECT asset_key FROM asset WHERE parent_asset_key = %s AND is_current = 1 AND asset_key <> %s ORDER BY asset_key", (k, k))]
    return out


def _open_work(con, k):
    calls = edit._rows(con, "SELECT count(*) AS n FROM svc_call WHERE asset_key = %s AND is_current = 1 AND call_status = 'OPEN'", (k,))[0]["n"]
    rma = edit._rows(con, "SELECT count(*) AS n FROM oem_rma WHERE asset_key = %s AND is_current = 1 AND coalesce(return_status, 'PENDING') <> 'RETURNED'", (k,))[0]["n"]
    return calls, rma


def preflight(key):
    """What the removal dialog shows for one asset before anything is changed. Read only."""
    row = db.one("SELECT * FROM asset WHERE asset_key = %s", [key])
    if not row:
        raise NotFound(f"Asset '{key}' was not found.")
    calls = db.one("SELECT count(*) AS n FROM svc_call WHERE asset_key = %s AND is_current = 1 AND call_status = 'OPEN'", [key])["n"]
    rma = db.one("SELECT count(*) AS n FROM oem_rma WHERE asset_key = %s AND is_current = 1 AND coalesce(return_status, 'PENDING') <> 'RETURNED'", [key])["n"]
    comps = db.one("SELECT count(*) AS n FROM asset WHERE parent_asset_key = %s AND is_current = 1", [key])["n"]
    return {"key": key, "blocked": _ineligible(row), "open_calls": calls, "open_rma": rma, "components": comps, **meta(),
            "asset": {f: row.get(f) for f in ("asset_key", "asset_class", "asset_type", "make", "model", "serial_no", "engineer_name", "location_code", *SNAP)}}


def meta():
    """What the picker page needs once: the quarter, the reasons and the people who can have given the verbal approval."""
    q = pm.quarter(TODAY())
    return {"quarter": {"label": q["label"], "start": q["start"], "end": q["end"]}, "default_date": q["start"],
            "reasons": [{"value": k, "label": v} for k, v in REASONS.items()],
            "approvers": [{"username": r["username"], "display_name": r["display_name"]} for r in db.query("SELECT username, display_name FROM portal_user WHERE active ORDER BY display_name")]}


def impact(keys, effective_date):
    """The billing impact of removing these assets on a date: days and credit per asset (its component lines' rates included), and the total. Read only."""
    q = pm.quarter(TODAY())
    when = _date(effective_date, q)
    keys = list(dict.fromkeys(str(k).strip().upper() for k in (keys or []) if str(k).strip()))
    lines = _expand(lambda sql, p: db.query(sql, list(p)), keys)
    rows = {r["asset_key"]: r for r in db.query("SELECT asset_key, rate_value, cover_type, asset_class, is_current, record_level, asset_status FROM asset WHERE asset_key = ANY(%s)", [[k for k, _ in lines]])}
    out, total = {}, Decimal("0")
    for k, parent in lines:
        r = rows.get(k)
        b = billing(r["rate_value"] if r else None, when)
        o = out.setdefault(parent or k, {"asset_key": parent or k, "rate_value": None, "days": b["days"], "amount": Decimal("0"), "component_amount": Decimal("0"), "components": 0, "no_rate": False, "blocked": None})
        if parent:
            o["components"] += 1
            o["component_amount"] += b["amount"] or 0
        else:
            o.update(rate_value=r["rate_value"] if r else None, no_rate=bool(r) and r["rate_value"] is None, blocked="it was not found" if not r else _ineligible(r))
        o["amount"] += b["amount"] or 0
        total += b["amount"] or 0
    return {"effective_date": when, "quarter": {"label": q["label"], "start": q["start"], "end": q["end"], "days": (q["end"] - q["start"]).days + 1}, "assets": list(out.values()), "total": total,
            "note": "Credit due from the vendor for the part of the quarter the asset is no longer covered. The rate is per quarter."}


CANDIDATE_CAP, KEY_CAP = 300, 2000


def candidates(f):
    """The assets that can be picked for removal, with the filters offered beside them. Only eligible assets are listed (current, AMC cover,
    not a laptop, not already removed); a component line is listed only when `components` is set - otherwise it travels with its parent.
    f: q (text), classes[], types[], engineer, location, components (0/1), all (return every match, up to the cap, not just the first page). Read only."""
    f = f or {}
    where = ["is_current = 1", "cover_type = 'AMC'", "asset_class <> 'LAPTOP'", "coalesce(asset_status, '') <> 'REMOVED_FROM_AMC'"]
    where.append("record_level IN ('ASSET','COMPONENT')" if f.get("components") else "record_level = 'ASSET'")
    base = " AND ".join(where)
    p = []
    q = (f.get("q") or "").strip()[:60]
    flt = ""
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        flt += " AND (asset_key ILIKE %s OR hostname ILIKE %s OR serial_no ILIKE %s OR model ILIKE %s OR make ILIKE %s OR ongc_asset_id ILIKE %s OR user_name ILIKE %s)"
        p += [like] * 7
    for col, vals in (("asset_class", f.get("classes")), ("asset_type", f.get("types"))):
        vals = [str(v).strip().upper() for v in (vals or []) if str(v).strip()]
        if vals:
            flt += f" AND {col} = ANY(%s)"
            p.append(vals)
    for col, val in (("engineer_name", f.get("engineer")), ("location_code", f.get("location"))):
        if val:
            flt += f" AND {col} = %s"
            p.append(str(val).strip().upper())
    cols = """asset_key, record_level, parent_asset_key, asset_class, asset_type, make, model, serial_no, engineer_name, location_code, rate_value, cover_expiry_date,
              (SELECT count(*) FROM asset c WHERE c.parent_asset_key = asset.asset_key AND c.is_current = 1 AND c.asset_key <> asset.asset_key) AS components,
              (SELECT count(*) FROM svc_call s WHERE s.asset_key = asset.asset_key AND s.is_current = 1 AND s.call_status = 'OPEN') AS open_calls"""
    keys = [r["asset_key"] for r in db.query(f"SELECT asset_key FROM asset WHERE {base}{flt} ORDER BY asset_class, asset_type, asset_key LIMIT {KEY_CAP + 1}", p)]
    rows = db.query(f"SELECT {cols} FROM asset WHERE {base}{flt} ORDER BY asset_class, asset_type, asset_key LIMIT {KEY_CAP if f.get('all') else CANDIDATE_CAP}", p)
    facets = {"classes": db.query(f"SELECT asset_class AS value, count(*) AS n FROM asset WHERE {base} AND asset_class IS NOT NULL GROUP BY 1 ORDER BY 1"),
              "types": db.query(f"SELECT asset_class, asset_type AS value, count(*) AS n FROM asset WHERE {base} AND asset_type IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2"),
              "engineers": db.query(f"SELECT engineer_name AS value, count(*) AS n FROM asset WHERE {base} AND engineer_name IS NOT NULL GROUP BY 1 ORDER BY 1"),
              "locations": db.query(f"SELECT location_code AS value, count(*) AS n FROM asset WHERE {base} AND location_code IS NOT NULL GROUP BY 1 ORDER BY 1")}
    return {"rows": rows, "total": len(keys) if len(keys) <= KEY_CAP else None, "keys": keys[:KEY_CAP] if len(keys) <= KEY_CAP else [], "capped": len(keys) > len(rows), "facets": facets}


def _archive_line(con, no, line, row, when, approver, note, editor, ip):
    """Archive one asset of an applied request and record what it looked like and what is owed back."""
    k = row["asset_key"]
    b = billing(row.get("rate_value"), when)
    con.execute("UPDATE amc_removal_line SET snapshot = %s::jsonb, credit_days = %s, credit_amount = %s WHERE line_id = %s",
                (edit.json.dumps({f: edit._jd(row.get(f)) for f in SNAP}), b["days"], b["amount"], line["line_id"]))
    edit._update_row(con, "assets", k, {"asset_status": "REMOVED_FROM_AMC", "rate_value": None, "is_current": 0})
    con.execute("""INSERT INTO portal_lock (dataset, record_key, field, value, editor) VALUES ('assets',%s,%s,'true'::jsonb,%s)
                   ON CONFLICT (dataset, record_key, field) DO UPDATE SET editor = EXCLUDED.editor, edited_at = now()""", (k, L.ARCHIVED, editor))
    edit._audit(con, editor, ip, "assets", k, "AMC_REMOVE",
                {"asset_status": {"old": row.get("asset_status"), "new": "REMOVED_FROM_AMC"}, "rate_value": {"old": edit._jd(row.get("rate_value")), "new": None},
                 "removal_no": no, "reason": line["reason"], "effective_date": when.isoformat(), "approved_by": approver,
                 "credit_days": b["days"], "credit_amount": edit._jd(b["amount"])}, note)


def _apply(con, no, editor, ip):
    """Apply a PENDING request: each asset is checked again (it may have changed since the request was made) and archived, or skipped with the reason."""
    head = edit._rows(con, "SELECT * FROM amc_removal WHERE removal_no = %s AND status = 'PENDING' FOR UPDATE", (no,))
    if not head:
        raise NotFound("This request is not waiting to be applied.")
    head = head[0]
    done = skipped = 0
    blocked_parents = set()
    lines = edit._rows(con, "SELECT * FROM amc_removal_line WHERE removal_no = %s ORDER BY line_id", (no,))
    for line in lines:
        r = edit._rows(con, "SELECT * FROM asset WHERE asset_key = %s FOR UPDATE", (line["asset_key"],))
        if line["via_parent"]:                                  # a component line: it only needs its parent to have gone
            why = "its parent asset was not removed" if line["via_parent"] in blocked_parents else ("it was not found" if not r or r[0]["is_current"] != 1 else None)
        else:
            why = "it was not found" if not r else _ineligible(r[0])
        if why:
            con.execute("UPDATE amc_removal_line SET skipped_reason = %s WHERE line_id = %s", (why, line["line_id"]))
            blocked_parents.add(line["asset_key"])
            skipped += 1
            continue
        _archive_line(con, no, line, r[0], head["effective_date"], head["approved_by"], head["note"], editor, ip)
        done += 1
        if not line["via_parent"]:                              # a component added after the request was made still leaves with its parent
            known = {x["asset_key"] for x in lines}
            for c in edit._rows(con, "SELECT * FROM asset WHERE parent_asset_key = %s AND is_current = 1 AND asset_key <> %s ORDER BY asset_key FOR UPDATE", (line["asset_key"], line["asset_key"])):
                if c["asset_key"] not in known:
                    nl = edit._rows(con, "INSERT INTO amc_removal_line (removal_no, asset_key, reason, via_parent) VALUES (%s,%s,%s,%s) RETURNING *", (no, c["asset_key"], line["reason"], line["asset_key"]))[0]
                    _archive_line(con, no, nl, c, head["effective_date"], head["approved_by"], head["note"], editor, ip)
                    done += 1
    con.execute("UPDATE amc_removal SET status = 'APPLIED' WHERE removal_no = %s", (no,))
    return done, skipped


def remove(keys, body, editor, ip):
    """Remove the assets `keys` from AMC in one request. body: reason (a REASONS key, or {asset_key: reason}), approved_by (portal username),
    effective_date (a day of the running quarter; today or earlier applies at once, a later day waits), note, override_note (needed when an asset
    still has open calls / RMA cases). Returns {"removal_no", "assets", "status"}."""
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
    when = _date(body.get("effective_date"), q)
    approver = str(body.get("approved_by") or "").strip()
    with db.write() as con:
        if not approver or not edit._exists(con, "SELECT 1 FROM portal_user WHERE username = %s AND active", (approver,)):
            raise Invalid("Choose the portal user who gave the verbal approval.", {"approved_by": "required - pick an active portal user"})
        rows = {r["asset_key"]: r for r in edit._rows(con, "SELECT * FROM asset WHERE asset_key = ANY(%s) ORDER BY asset_key FOR UPDATE", (keys,))}
        lines = _expand(lambda sql, p: edit._rows(con, sql, p), keys)
        problems, warnings = [], []
        for k in (k for k, parent in lines if parent is None):
            r = rows.get(k)
            why = "it was not found" if not r else _ineligible(r)
            if why:
                problems.append(f"{k}: {why}")
                continue
            calls, rma = _open_work(con, k)
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
        con.execute("INSERT INTO amc_removal (removal_no, quarter_label, seq, effective_date, requested_by, approved_by, approved_on, note, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'PENDING')",
                    (no, q["label"], seq, when, editor, approver, TODAY(), note))
        for k, parent in lines:
            con.execute("INSERT INTO amc_removal_line (removal_no, asset_key, reason, override_note, via_parent) VALUES (%s,%s,%s,%s,%s)", (no, k, reasons[parent or k], override if not parent else None, parent))
        if when <= TODAY():
            _apply(con, no, editor, ip)
    return {"removal_no": no, "assets": [k for k, p in lines if p is None], "components": sum(1 for _, p in lines if p), "status": "APPLIED" if when <= TODAY() else "PENDING"}


def apply_due(editor="SCHEDULER", ip=None):
    """Apply every PENDING request whose effective date has arrived (run daily by the scheduler). Returns [(removal_no, applied, skipped)]."""
    out = []
    for r in db.query("SELECT removal_no FROM amc_removal WHERE status = 'PENDING' AND effective_date <= %s ORDER BY effective_date, seq", [TODAY()]):
        with db.write() as con:
            out.append((r["removal_no"], *_apply(con, r["removal_no"], editor, ip)))
    return out


def cancel(removal_no, editor, ip):
    """Withdraw a request that is still waiting for its effective date. Nothing has been changed on the assets yet."""
    no = str(removal_no or "").strip().upper()
    with db.write() as con:
        if not con.execute("UPDATE amc_removal SET status = 'CANCELLED' WHERE removal_no = %s AND status = 'PENDING'", (no,)).rowcount:
            raise Conflict("Only a request that is still waiting for its effective date can be cancelled.")
    return {"removal_no": no, "status": "CANCELLED"}


def requests(limit=100):
    """Recent requests, newest first, each with its lines and the credit owed. Read only."""
    heads = db.query("SELECT * FROM amc_removal ORDER BY created_at DESC, removal_no DESC LIMIT %s", [int(limit)])
    lines = db.query("SELECT removal_no, asset_key, reason, credit_days, credit_amount, skipped_reason FROM amc_removal_line WHERE removal_no = ANY(%s) ORDER BY line_id", [[h["removal_no"] for h in heads]])
    for h in heads:
        h["lines"] = [x for x in lines if x["removal_no"] == h["removal_no"]]
        h["credit_total"] = sum((x["credit_amount"] or 0 for x in h["lines"]), Decimal("0"))
    return heads
