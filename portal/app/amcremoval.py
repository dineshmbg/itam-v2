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
    "ALTER TABLE amc_removal ADD CONSTRAINT amc_removal_status_check CHECK (status IN ('REQUESTED','PENDING','APPLIED','REVERSED','CANCELLED','REJECTED'))",
    "ALTER TABLE amc_removal ALTER COLUMN status SET DEFAULT 'PENDING'",
    # REQUESTED: an engineer's request waiting for a team leader / administrator (no approver yet); REJECTED carries the reason the engineer reads.
    "ALTER TABLE amc_removal ALTER COLUMN approved_by DROP NOT NULL",
    "ALTER TABLE amc_removal ALTER COLUMN approved_on DROP NOT NULL",
    "ALTER TABLE amc_removal ADD COLUMN IF NOT EXISTS decided_by TEXT",
    "ALTER TABLE amc_removal ADD COLUMN IF NOT EXISTS decision_note TEXT",
    "ALTER TABLE amc_removal_line ALTER COLUMN snapshot DROP NOT NULL",
    "ALTER TABLE amc_removal_line ADD COLUMN IF NOT EXISTS credit_days INT",
    "ALTER TABLE amc_removal_line ADD COLUMN IF NOT EXISTS credit_amount NUMERIC(14,2)",
    "ALTER TABLE amc_removal_line ADD COLUMN IF NOT EXISTS skipped_reason TEXT",
    "ALTER TABLE amc_removal_line ADD COLUMN IF NOT EXISTS reinstated_at DATE",         # back in AMC from this day; the credit kept is net_credit_*
    "ALTER TABLE amc_removal_line ADD COLUMN IF NOT EXISTS reinstated_by TEXT",
    "ALTER TABLE amc_removal_line ADD COLUMN IF NOT EXISTS reinstated_approved_by TEXT",
    "ALTER TABLE amc_removal_line ADD COLUMN IF NOT EXISTS reinstate_reason TEXT",
    "ALTER TABLE amc_removal_line ADD COLUMN IF NOT EXISTS net_credit_days INT",
    "ALTER TABLE amc_removal_line ADD COLUMN IF NOT EXISTS net_credit_amount NUMERIC(14,2)",
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
    if row.get("asset_status") == "REMOVED_FROM_AMC":
        return "it is already removed from AMC"
    if row["is_current"] != 1:
        return "it is archived or not current"
    if row["record_level"] not in ("ASSET", "COMPONENT"):
        return "it is not an asset or a component line"
    if row["asset_class"] == "LAPTOP":
        return "laptops are outside AMC scope"
    if row.get("cover_type") != "AMC":
        return "it is not covered by AMC"
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
            "approvers": [{"username": r["username"], "display_name": r["display_name"]} for r in _only_deciders(db.query(_DECIDERS))]}


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


def _norm(keys):
    return list(dict.fromkeys(str(k).strip().upper() for k in (keys or []) if str(k).strip()))


def _text(raw):
    return re.sub(r"\s+", " ", str(raw or "").strip())[:300] or None


def _reasons(keys, body):
    r = body.get("reason")
    r = r if isinstance(r, dict) else {k: r for k in keys}
    r = {k: str(r.get(k) or "").upper() for k in keys}
    if any(v not in REASONS for v in r.values()):
        raise Invalid("Give a reason for every asset.", {"reason": "required"})
    return r


def _check(con, keys, override, owner=None):
    """Lock the chosen assets, work out the lines (parts follow their parent) and refuse the request - touching nothing - if any chosen asset
    cannot go. Open calls / RMA cases refuse it too unless an override note is given. `owner`: every chosen asset must be assigned to this engineer."""
    rows = {r["asset_key"]: r for r in edit._rows(con, "SELECT * FROM asset WHERE asset_key = ANY(%s) ORDER BY asset_key FOR UPDATE", (keys,))}
    lines = _expand(lambda sql, p: edit._rows(con, sql, p), keys)
    problems, warnings = [], []
    for k in (k for k, parent in lines if parent is None):
        r = rows.get(k)
        why = "it was not found" if not r else _ineligible(r)
        if not why and owner is not None and r.get("engineer_name") != owner:
            why = "it is not assigned to you"
        if why:
            problems.append(f"{k}: {why}")
            continue
        calls, rma = _open_work(con, k)
        if owner is None and (calls or rma) and not (override and len(override) >= 5):
            warnings.append(f"{k}: {calls} open call(s), {rma} OEM RMA case(s) not returned")
    if problems:
        raise Conflict("Nothing was removed. " + "; ".join(problems))
    if warnings:
        raise Conflict("Nothing was removed. " + "; ".join(warnings) + ". Give an override note (at least 5 characters) to remove them anyway.")
    return lines


_DECIDERS = """SELECT u.username, u.display_name, u.role, u.extended_access, u.read_only, e.designation FROM portal_user u
               LEFT JOIN v_engineer e ON e.engineer_key = u.engineer_key WHERE u.active ORDER BY u.display_name, u.username"""


def _only_deciders(rows):
    """Active, writable accounts that decide: administrators and Team Leader/SI users (the same test as auth.is_lead). Nobody else can give the approval."""
    from . import auth
    return [r for r in rows if not r["read_only"] and (r["role"] == "ADMIN" or (not r["extended_access"] and auth.is_lead_designation(r["designation"])))]


def _approver(con, raw):
    who = str(raw or "").strip()
    if not who or who not in {r["username"] for r in _only_deciders(edit._rows(con, _DECIDERS, ()))}:
        raise Invalid("Choose the administrator or Team Leader/SI who gave the verbal approval.", {"approved_by": "required - only an administrator or Team Leader/SI can approve"})
    return who


def _create(con, q, when, requested_by, approver, note, status, lines, reasons, override):
    con.execute("SELECT pg_advisory_xact_lock(hashtext('amc_removal'))")          # two requests at once must not take the same number
    seq = edit._rows(con, "SELECT coalesce(max(seq), 0) + 1 AS n FROM amc_removal WHERE quarter_label = %s", (q["label"],))[0]["n"]
    fy = q["start"].year if q["start"].month >= 4 else q["start"].year - 1
    no = f"AMCR-{fy}{q['label'][:2]}-{seq:03d}"
    con.execute("INSERT INTO amc_removal (removal_no, quarter_label, seq, effective_date, requested_by, approved_by, approved_on, note, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (no, q["label"], seq, when, requested_by, approver, TODAY() if approver else None, note, status))
    for k, parent in lines:
        con.execute("INSERT INTO amc_removal_line (removal_no, asset_key, reason, override_note, via_parent) VALUES (%s,%s,%s,%s,%s)", (no, k, reasons[parent or k], override if not parent else None, parent))
    return no


def _result(no, lines, status):
    return {"removal_no": no, "assets": [k for k, p in lines if p is None], "components": sum(1 for _, p in lines if p), "status": status}


def remove(keys, body, editor, ip):
    """A team leader / administrator removes the assets `keys` from AMC in one request. body: reason (a REASONS key, or {asset_key: reason}),
    approved_by (portal username), effective_date (a day of the running quarter; today or earlier applies at once, a later day waits), note,
    override_note (needed when an asset still has open calls / RMA cases). Returns {"removal_no", "assets", "components", "status"}."""
    body = body or {}
    keys = _norm(keys)
    if not keys:
        raise Invalid("Choose at least one asset.", {"keys": "required"})
    reasons = _reasons(keys, body)
    q = pm.quarter(TODAY())
    when = _date(body.get("effective_date"), q)
    with db.write() as con:
        approver = _approver(con, body.get("approved_by"))
        lines = _check(con, keys, _text(body.get("override_note")))
        no = _create(con, q, when, editor, approver, _text(body.get("note")), "PENDING", lines, reasons, _text(body.get("override_note")))
        if when <= TODAY():
            _apply(con, no, editor, ip)
    return _result(no, lines, "APPLIED" if when <= TODAY() else "PENDING")


def request(keys, body, engineer_key, editor, ip):
    """An engineer asks for some of THEIR OWN assets to be removed from AMC. Nothing changes until a team leader or administrator approves."""
    body = body or {}
    keys = _norm(keys)
    if not engineer_key:
        raise Invalid("Only an engineer can request this.")
    if not keys:
        raise Invalid("Choose at least one asset.", {"keys": "required"})
    reasons = _reasons(keys, body)
    q = pm.quarter(TODAY())
    with db.write() as con:
        lines = _check(con, keys, None, owner=engineer_key)
        no = _create(con, q, _date(body.get("effective_date"), q), editor, None, _text(body.get("note")), "REQUESTED", lines, reasons, None)
    return _result(no, lines, "REQUESTED")


def approve(removal_no, body, editor, ip):
    """A team leader / administrator decides an engineer's request: records who gave the verbal approval, may change the date, then applies
    it (or waits for the date). The assets are checked again, and open calls need an override note, exactly as in remove()."""
    body = body or {}
    no = str(removal_no or "").strip().upper()
    q = pm.quarter(TODAY())
    with db.write() as con:
        head = edit._rows(con, "SELECT * FROM amc_removal WHERE removal_no = %s AND status = 'REQUESTED' FOR UPDATE", (no,))
        if not head:
            raise Conflict("This request is not waiting for a decision.")
        head = head[0]
        when = _date(body.get("effective_date") or max(head["effective_date"], q["start"]), q)
        approver = _approver(con, body.get("approved_by"))
        override = _text(body.get("override_note"))
        keys = [r["asset_key"] for r in edit._rows(con, "SELECT asset_key FROM amc_removal_line WHERE removal_no = %s AND via_parent IS NULL ORDER BY line_id", (no,))]
        _check(con, keys, override)
        con.execute("UPDATE amc_removal SET status = 'PENDING', approved_by = %s, approved_on = %s, effective_date = %s, decided_by = %s WHERE removal_no = %s", (approver, TODAY(), when, editor, no))
        if override:
            con.execute("UPDATE amc_removal_line SET override_note = %s WHERE removal_no = %s AND via_parent IS NULL", (override, no))
        if when <= TODAY():
            _apply(con, no, editor, ip)
    return {"removal_no": no, "status": "APPLIED" if when <= TODAY() else "PENDING"}


def reject(removal_no, body, editor, ip):
    """Turn an engineer's request down. A reason is required: the engineer reads it."""
    why = _text((body or {}).get("reason"))
    if not why or len(why) < 5:
        raise Invalid("Give the engineer a reason (at least 5 characters).", {"reason": "required"})
    no = str(removal_no or "").strip().upper()
    with db.write() as con:
        if not con.execute("UPDATE amc_removal SET status = 'REJECTED', decided_by = %s, decision_note = %s WHERE removal_no = %s AND status = 'REQUESTED'", (editor, why, no)).rowcount:
            raise Conflict("This request is not waiting for a decision.")
    return {"removal_no": no, "status": "REJECTED"}


def apply_due(editor="SCHEDULER", ip=None):
    """Apply every PENDING request whose effective date has arrived (run daily by the scheduler). Returns [(removal_no, applied, skipped)]."""
    out = []
    for r in db.query("SELECT removal_no FROM amc_removal WHERE status = 'PENDING' AND effective_date <= %s ORDER BY effective_date, seq", [TODAY()]):
        with db.write() as con:
            out.append((r["removal_no"], *_apply(con, r["removal_no"], editor, ip)))
    return out


def cancel(removal_no, editor, ip, only_own=False):
    """Withdraw a request that has not been applied (waiting for its date, or for a decision). Nothing has been changed on the assets yet.
    `only_own`: an engineer may only withdraw their own request, and only while it waits for a decision."""
    no = str(removal_no or "").strip().upper()
    sql = "UPDATE amc_removal SET status = 'CANCELLED' WHERE removal_no = %s AND " + ("status = 'REQUESTED' AND requested_by = %s" if only_own else "status IN ('PENDING','REQUESTED')")
    with db.write() as con:
        if not con.execute(sql, (no, editor) if only_own else (no,)).rowcount:
            raise Conflict("Only a request that has not been applied yet can be cancelled" + (", and only by the person who made it." if only_own else "."))
    return {"removal_no": no, "status": "CANCELLED"}


def _with_lines(heads):
    lines = db.query("""SELECT removal_no, asset_key, reason, via_parent, credit_days, credit_amount, skipped_reason, reinstated_at, reinstate_reason, net_credit_days, net_credit_amount
                        FROM amc_removal_line WHERE removal_no = ANY(%s) ORDER BY line_id""", [[h["removal_no"] for h in heads]])
    for h in heads:
        h["lines"] = [x for x in lines if x["removal_no"] == h["removal_no"]]
        h["credit_total"] = sum(((x["net_credit_amount"] if x["reinstated_at"] else x["credit_amount"]) or 0 for x in h["lines"]), Decimal("0"))      # what stands after any reinstatement
    return heads


def requests(limit=100, status=None):
    """Recent requests, newest first, each with its lines and the credit owed (waiting ones first when filtered). Read only."""
    heads = db.query("SELECT * FROM amc_removal WHERE (%s::text IS NULL OR status = %s) ORDER BY created_at DESC, removal_no DESC LIMIT %s", [status, status, int(limit)])
    return _with_lines(heads)


def mine(engineer_key, username):
    """What an engineer sees: their own assets that can be requested, their requests and what became of them, and - read only, outside every
    count - their assets that have left the AMC."""
    heads = db.query("SELECT * FROM amc_removal WHERE requested_by = %s ORDER BY created_at DESC LIMIT 100", [username])
    removed = db.query("""SELECT l.asset_key, l.via_parent, l.reason, r.removal_no, r.effective_date, a.asset_class, a.asset_type, a.make, a.model, a.serial_no, a.location_code
                          FROM amc_removal_line l JOIN amc_removal r USING (removal_no) JOIN asset a ON a.asset_key = l.asset_key
                          WHERE r.status = 'APPLIED' AND l.skipped_reason IS NULL AND a.is_current = 0 AND a.engineer_name = %s ORDER BY r.effective_date DESC, l.asset_key""", [engineer_key])
    return {"assets": candidates({"engineer": engineer_key, "all": True})["rows"] if engineer_key else [], "requests": _with_lines(heads), "removed": removed,
            "reasons": [{"value": k, "label": v} for k, v in REASONS.items()], "quarter": meta()["quarter"], "default_date": meta()["default_date"]}


# ---------------------------------------------------------------- reinstatement
def _num(v):
    return None if v is None else Decimal(str(v))


def reinstate(keys, body, editor, ip):
    """Put assets that were removed from AMC back: un-archive them and restore the cover, rate and PM state saved when they were removed (their
    parts come back with them). body: approved_by (portal username - the verbal approval), reason (at least 5 characters), effective_date (from
    when the vendor bills them again: not before the removal or the running quarter, not in the future; default today). One transaction: all or none.
    The removal request stays as the record; a request whose assets have all come back is marked REVERSED."""
    body = body or {}
    keys = _norm(keys)
    if not keys:
        raise Invalid("Choose at least one asset.", {"keys": "required"})
    why = _text(body.get("reason"))
    if not why or len(why) < 5:
        raise Invalid("Give a reason for reinstating (at least 5 characters).", {"reason": "required"})
    q = pm.quarter(TODAY())
    try:
        when = dt.date.fromisoformat(str(body.get("effective_date"))[:10]) if body.get("effective_date") else TODAY()
    except ValueError:
        raise Invalid("Enter a valid date.", {"effective_date": "enter a valid date (YYYY-MM-DD)"}) from None
    if when > TODAY() or when < q["start"]:
        raise Invalid(f"The date must be between the start of the running quarter ({q['start']}) and today.", {"effective_date": f"between {q['start']} and {TODAY()}"})
    touched = set()
    with db.write() as con:
        approver = _approver(con, body.get("approved_by"))
        problems = []
        todo = []
        for k in keys:
            ln = edit._rows(con, """SELECT l.*, r.effective_date, r.created_at FROM amc_removal_line l JOIN amc_removal r USING (removal_no)
                                    WHERE l.asset_key = %s AND l.via_parent IS NULL AND r.status = 'APPLIED' AND l.skipped_reason IS NULL AND l.snapshot IS NOT NULL
                                      AND l.reinstated_at IS NULL ORDER BY r.created_at DESC, l.line_id DESC LIMIT 1 FOR UPDATE OF l""", (k,))
            if not ln:
                problems.append(f"{k}: it was not removed from AMC through a request, or it has already been reinstated")
            elif when < ln[0]["effective_date"]:
                problems.append(f"{k}: it cannot be reinstated from before it was removed ({ln[0]['effective_date']})")
            else:
                todo.append(ln[0])
        if problems:
            raise Conflict("Nothing was reinstated. " + "; ".join(problems))
        for ln in todo:
            parts = edit._rows(con, "SELECT * FROM amc_removal_line WHERE removal_no = %s AND via_parent = %s AND skipped_reason IS NULL AND snapshot IS NOT NULL AND reinstated_at IS NULL FOR UPDATE",
                               (ln["removal_no"], ln["asset_key"]))
            for x in [ln, *parts]:
                s = x["snapshot"]
                cols = {"is_current": 1, "asset_status": s.get("asset_status"), "cover_type": s.get("cover_type"), "pm_status": s.get("pm_status"),
                        "cover_expiry_date": L.from_json("cover_expiry_date", s.get("cover_expiry_date")), "rate_value": _num(s.get("rate_value"))}
                edit._update_row(con, "assets", x["asset_key"], cols)
                con.execute("DELETE FROM portal_lock WHERE dataset = 'assets' AND record_key = %s AND field = %s", (x["asset_key"], L.ARCHIVED))
                span = pm.quarter(ln["effective_date"])
                # credit kept = the days it really was out: back in the same quarter -> up to the day it returned; later quarter -> all of the original credit
                days = (when - ln["effective_date"]).days if when <= span["end"] else x["credit_days"]
                net = None if s.get("rate_value") is None else (_num(s["rate_value"]) * days / ((span["end"] - span["start"]).days + 1)).quantize(Decimal("0.01"), ROUND_HALF_UP)
                con.execute("UPDATE amc_removal_line SET reinstated_at = %s, reinstated_by = %s, reinstated_approved_by = %s, reinstate_reason = %s, net_credit_days = %s, net_credit_amount = %s WHERE line_id = %s",
                            (when, editor, approver, why, days, net, x["line_id"]))
                edit._audit(con, editor, ip, "assets", x["asset_key"], "AMC_REINSTATE",
                            {"asset_status": {"old": "REMOVED_FROM_AMC", "new": s.get("asset_status")}, "rate_value": {"old": None, "new": s.get("rate_value")}, "removal_no": x["removal_no"],
                             "effective_date": when.isoformat(), "approved_by": approver, "net_credit_days": days}, why)
            touched.add(ln["removal_no"])
        for no in touched:
            if not edit._exists(con, "SELECT 1 FROM amc_removal_line WHERE removal_no = %s AND via_parent IS NULL AND skipped_reason IS NULL AND reinstated_at IS NULL", (no,)):
                con.execute("UPDATE amc_removal SET status = 'REVERSED' WHERE removal_no = %s", (no,))
    return {"reinstated": [ln["asset_key"] for ln in todo], "removals": sorted(touched)}


# ---------------------------------------------------------------- a pasted / uploaded list
LIST_CAP = 2000
_HEADER_WORDS = {"asset", "asset (ci)", "ci", "ci no", "ci number", "asset key", "asset_key", "hostname", "host name", "serial", "serial no", "serial number", "serial_no", "name", "computer name", "key"}


def parse_list(text):
    """Asset names from pasted text: one per line, or separated by commas, tabs or semicolons. A first line that is a column heading is dropped."""
    toks = [t.strip().strip("'\"").upper() for t in re.split(r"[\r\n,;\t]+", str(text or "")) if t.strip().strip("'\"")]
    if toks and toks[0].lower() in _HEADER_WORDS:
        toks = toks[1:]
    return list(dict.fromkeys(toks))[:LIST_CAP]


def _reason_key(v):
    """A reason as typed in the template (its label, or its code) -> the code; '?text' when it is not one of the listed reasons; None when blank."""
    t = str(v or "").strip()
    if not t:
        return None
    by_label = {lbl.upper(): k for k, lbl in REASONS.items()}
    return t.upper() if t.upper() in REASONS else by_label.get(t.upper()) or "?" + t


def read_list(data):
    """(names, {name: reason}) from an uploaded list. The upload template (columns Asset (CI), Serial no., Reason) gives a reason per row and either the
    CI or the serial is enough; any other workbook is read as its first column, and text / CSV as names split on lines, commas, tabs or semicolons."""
    if data[:2] != b"PK":
        return parse_list(data.decode("utf-8", "replace")), {}
    import io
    from openpyxl import load_workbook
    try:
        rows = [list(r) for r in load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0].iter_rows(values_only=True)]
    except Exception as e:      # noqa: BLE001
        raise Invalid("That Excel file could not be read.") from e
    head = [str(c or "").strip().lower() for c in (rows[0] if rows else [])]
    if "asset (ci)" not in head and "serial no." not in head:
        return parse_list("\n".join(str(r[0]).strip() for r in rows if r and r[0] is not None)), {}
    at = lambda name: head.index(name) if name in head else None  # noqa: E731
    ci, sn, rs = at("asset (ci)"), at("serial no."), at("reason")
    names, reasons = [], {}
    for r in rows[1:]:
        cell = lambda i: str(r[i]).strip().upper() if i is not None and i < len(r) and r[i] is not None and str(r[i]).strip() else ""  # noqa: E731
        t = cell(ci) or cell(sn)
        if not t:
            continue
        names.append(t)
        k = _reason_key(r[rs]) if rs is not None and rs < len(r) else None
        if k:
            reasons[t] = k
    return list(dict.fromkeys(names))[:LIST_CAP], reasons


def template_bytes():
    """The bulk-upload template: Asset (CI), Serial no. (either is enough) and a Reason picked from a drop-down list."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.worksheet.datavalidation import DataValidation
    wb = Workbook()
    ws = wb.active
    ws.title = "Assets"
    ws.append(["Asset (CI)", "Serial no.", "Reason"])
    for c in ws[1]:
        c.font = Font(bold=True)
    for col, w in zip("ABC", (28, 24, 34)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    lists = wb.create_sheet("Lists")
    for i, lbl in enumerate(REASONS.values(), start=1):
        lists.cell(row=i, column=1, value=lbl)
    lists.sheet_state = "hidden"
    dv = DataValidation(type="list", formula1=f"=Lists!$A$1:$A${len(REASONS)}", allow_blank=True)
    dv.error, dv.errorTitle = "Choose a reason from the list.", "Reason"
    ws.add_data_validation(dv)
    dv.add("C2:C2001")
    how = wb.create_sheet("How to fill")
    for line in ("One asset per row on the Assets sheet, from row 2 (up to 2,000 rows).",
                 "Asset (CI) or Serial no. - either one is enough. Hostname and ONGC asset ID also work in the Asset (CI) column.",
                 "Reason - pick one from the list. Leave it blank to choose the reason in the basket afterwards.",
                 "Nothing is removed by uploading: the portal checks every row, tells you which can go and which cannot (laptops, warranty-only, already removed, not found),",
                 "and you add the good ones to the basket, set the date and the verbal approval, and send the request.",
                 "Reasons: " + "; ".join(REASONS.values())):
        how.append([line])
    how.column_dimensions["A"].width = 150
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def list_from_file(data, filename):
    """The same from an uploaded file: text/CSV as it is, or the first column of the first sheet of an Excel workbook."""
    if data[:2] == b"PK":
        import io
        from openpyxl import load_workbook
        try:
            ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
            return parse_list("\n".join(str(r[0]).strip() for r in ws.iter_rows(values_only=True) if r and r[0] is not None))
        except Exception as e:      # noqa: BLE001
            raise Invalid("That Excel file could not be read.") from e
    return parse_list(data.decode("utf-8", "replace"))


def match_list(tokens, owner=None, reasons=None):
    """Match each name against the register (asset key, CI number, hostname, serial number or ONGC asset ID). For every name: the asset it
    found and whether it can be removed, or why not - an asset already removed is recognised, not 'not found'. Nothing is changed.
    `owner`: an engineer sees only their own assets (anything else reads as not found)."""
    tokens = _norm(tokens)[:LIST_CAP]
    found = db.query("""SELECT asset_key, ci_no, hostname, serial_no, ongc_asset_id, is_current, record_level, asset_class, asset_type, make, model, engineer_name, location_code,
                               cover_type, asset_status, rate_value,
                               (SELECT count(*) FROM asset c WHERE c.parent_asset_key = asset.asset_key AND c.is_current = 1 AND c.asset_key <> asset.asset_key) AS components,
                               (SELECT count(*) FROM svc_call s WHERE s.asset_key = asset.asset_key AND s.is_current = 1 AND s.call_status = 'OPEN') AS open_calls
                        FROM asset WHERE upper(asset_key) = ANY(%s) OR upper(ci_no) = ANY(%s) OR upper(hostname) = ANY(%s) OR upper(serial_no) = ANY(%s) OR upper(ongc_asset_id) = ANY(%s)""",
                      [tokens] * 5)
    out, seen = [], {}
    for t in tokens:
        hits = [r for r in found if t in {str(r[c] or "").upper() for c in ("asset_key", "ci_no", "hostname", "serial_no", "ongc_asset_id")}]
        live = [r for r in hits if r["is_current"] == 1]
        hits = live or hits                                                   # a live record beats an archived one carrying the same name
        hits = [r for r in hits if str(r["asset_key"]).upper() == t] or hits  # and an exact asset key beats a hostname or serial that reads the same
        if owner is not None:
            hits = [r for r in hits if r["engineer_name"] == owner]
        if not hits:
            out.append({"input": t, "result": "NOT_FOUND", "why": "no asset in the register has this name, serial number or ONGC ID"})
        elif len(hits) > 1:
            out.append({"input": t, "result": "AMBIGUOUS", "why": "matches " + ", ".join(sorted(r["asset_key"] for r in hits)[:5]) + " - use the asset key"})
        else:
            r = hits[0]
            why = _ineligible(r)
            if not why and r["asset_key"] in seen:
                why = f"same asset as {seen[r['asset_key']]}"
            seen.setdefault(r["asset_key"], t)
            row = {k: r[k] for k in ("asset_key", "record_level", "asset_class", "asset_type", "make", "model", "serial_no", "engineer_name", "location_code", "rate_value", "components", "open_calls")}
            res = {"input": t, "result": "BLOCKED" if why else "OK", "why": why, "row": row}
            given = (reasons or {}).get(t)
            if given and given.startswith("?"):
                res["reason_note"] = f"the reason '{given[1:]}' is not one of the listed reasons - choose it in the basket"
            elif given:
                res["reason"] = given
            out.append(res)
    n = lambda s: sum(1 for x in out if x["result"] == s)  # noqa: E731
    return {"rows": out, "ok": n("OK"), "blocked": n("BLOCKED"), "not_found": n("NOT_FOUND"), "ambiguous": n("AMBIGUOUS"), "capped": len(_norm(tokens)) >= LIST_CAP}


# ---------------------------------------------------------------- reports
_LIVE = "r.status IN ('APPLIED','REVERSED') AND l.skipped_reason IS NULL AND l.snapshot IS NOT NULL"


def summary():
    """Per quarter: how many assets (and parts) were removed, how many came back, what credit stands, and how many requests wait. Read only."""
    rows = db.query(f"""SELECT r.quarter_label, min(r.created_at) AS first_at,
                               count(*) FILTER (WHERE l.via_parent IS NULL AND {_LIVE}) AS assets,
                               count(*) FILTER (WHERE l.via_parent IS NOT NULL AND {_LIVE}) AS parts,
                               count(*) FILTER (WHERE l.via_parent IS NULL AND {_LIVE} AND l.reinstated_at IS NOT NULL) AS reinstated,
                               coalesce(sum(CASE WHEN {_LIVE} THEN CASE WHEN l.reinstated_at IS NOT NULL THEN l.net_credit_amount ELSE l.credit_amount END END), 0) AS credit
                        FROM amc_removal r JOIN amc_removal_line l USING (removal_no) GROUP BY r.quarter_label ORDER BY min(r.created_at) DESC""")
    status = {x["status"]: x["n"] for x in db.query("SELECT status, count(*) AS n FROM amc_removal GROUP BY 1")}
    return {"quarters": rows, "waiting": status.get("REQUESTED", 0), "pending": status.get("PENDING", 0)}


def report_bytes(quarter=None):
    """An Excel workbook of every removal line - the paper behind a credit note: request, dates, asset, reason, rate, days and credit, who asked
    and who approved. `quarter`: one quarter label, or all."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font
    lines = db.query("""SELECT r.removal_no, r.quarter_label, r.status, r.effective_date, r.requested_by, r.approved_by, r.approved_on, r.decided_by, l.asset_key, l.via_parent, l.reason,
                               a.asset_class, a.asset_type, a.make, a.model, a.serial_no, a.engineer_name, a.location_code, l.snapshot->>'rate_value' AS rate_value,
                               l.credit_days, l.credit_amount, l.skipped_reason, l.reinstated_at, l.reinstated_approved_by, l.reinstate_reason, l.net_credit_days, l.net_credit_amount
                        FROM amc_removal_line l JOIN amc_removal r USING (removal_no) LEFT JOIN asset a ON a.asset_key = l.asset_key
                        WHERE r.status IN ('APPLIED','REVERSED') AND (%s::text IS NULL OR r.quarter_label = %s) ORDER BY r.created_at, r.removal_no, l.line_id""", [quarter, quarter])
    wb = Workbook()
    ws = wb.active
    ws.title = "AMC removals"
    head = ["Request", "Quarter", "Effective date", "Asset (CI)", "Part of", "Class", "Type", "Make", "Model", "Serial no.", "Engineer", "Location", "Reason", "Rate per quarter",
            "Days not covered", "Credit", "Skipped because", "Reinstated on", "Reinstated - approved by", "Reinstated - reason", "Days kept out", "Credit that stands",
            "Requested by", "Verbal approval by", "Approved on"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
    for x in lines:
        ws.append([x["removal_no"], x["quarter_label"], x["effective_date"], x["asset_key"], x["via_parent"], x["asset_class"], x["asset_type"], x["make"], x["model"], x["serial_no"], x["engineer_name"],
                   x["location_code"], REASONS.get(x["reason"], x["reason"]), _num(x["rate_value"]), x["credit_days"], x["credit_amount"], x["skipped_reason"], x["reinstated_at"],
                   x["reinstated_approved_by"], x["reinstate_reason"], x["net_credit_days"], x["net_credit_amount"] if x["reinstated_at"] else x["credit_amount"], x["requested_by"], x["approved_by"], x["approved_on"]])
    for col, w in zip("ABCDEFGHIJKLMNOPQRSTUVWXY", [18, 18, 13, 24, 20, 14, 18, 14, 18, 18, 18, 18, 24, 12, 10, 12, 24, 13, 18, 28, 10, 14, 18, 18, 12]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    s2 = wb.create_sheet("By quarter")
    s2.append(["Quarter", "Assets removed", "Parts removed", "Reinstated", "Credit that stands"])
    for c in s2[1]:
        c.font = Font(bold=True)
    for x in summary()["quarters"]:
        if quarter in (None, x["quarter_label"]):
            s2.append([x["quarter_label"], x["assets"], x["parts"], x["reinstated"], x["credit"]])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
