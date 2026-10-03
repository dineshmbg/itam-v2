"""Replace an asset / redeploy a retired one: the identity of a physical machine versus the name (CI number, hostname) it currently carries.

A machine's physical identity - serial number, ONGC asset ID, census number - never changes. Its asset key / CI number / hostname are role
labels that can move:
  * replace():  a new machine takes over an old machine's key. The old machine is re-keyed to <key>-RET-<yyyymmdd> and retired (archived,
                status REPLACED) with its own history; the new machine is re-keyed to the vacated name.
  * redeploy(): a retired machine goes back into service under a new key. It stays ONE record, with its history.
Both run in one transaction (all or nothing), are administrator-only, re-key every table that refers to the asset (see KEY_COLUMNS), keep the
former names in asset_alias, and write the audit trail. The three identity numbers are never accepted from the browser on a redeploy.
"""
import datetime as dt
import re

from . import db, edit
from .edit import Conflict, Invalid, NotFound
import itam_locks as L  # noqa: E402  (tools/ is on sys.path - see edit.py)

DDL = [
    # Portal-only retirement columns: the asset loader names its own fixed column list and never touches these.
    "ALTER TABLE asset ADD COLUMN IF NOT EXISTS retired_on DATE",
    "ALTER TABLE asset ADD COLUMN IF NOT EXISTS retired_reason TEXT",
    "ALTER TABLE asset ADD COLUMN IF NOT EXISTS replaced_by_key TEXT",
    "ALTER TABLE asset ADD COLUMN IF NOT EXISTS disposal_ref TEXT",
    "ALTER TABLE asset ADD COLUMN IF NOT EXISTS data_wiped TEXT",
    """CREATE TABLE IF NOT EXISTS asset_alias (
         alias_id BIGSERIAL PRIMARY KEY, alias_key TEXT NOT NULL, asset_key TEXT NOT NULL,   -- alias_key: a name the machine used to carry; asset_key: the key it carries now
         valid_from DATE, valid_to DATE, reason TEXT, created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS ix_asset_alias_alias ON asset_alias (alias_key)",
    "CREATE INDEX IF NOT EXISTS ix_asset_alias_asset ON asset_alias (asset_key)",
]

RETIRE_REASONS = {"END_OF_LIFE": "End of life / obsolete", "USER_UPGRADE": "User upgrade", "FAILED": "Failed beyond repair",
                  "CONDEMNED": "Condemned / written off", "LOST_STOLEN": "Lost or stolen"}
NEEDS_APPROVAL = {"CONDEMNED", "LOST_STOLEN"}          # reuse of such a machine needs a written approval reference

# Every table column that holds an asset key. Views are not listed (they read these). portal_lock / portal_audit are handled separately.
KEY_COLUMNS = [("asset", "asset_key"), ("asset", "parent_asset_key"), ("asset_change_log", "asset_key"), ("asset_snapshot", "asset_key"),
               ("asset_snapshot", "parent_asset_key"), ("asset_verification", "asset_key"), ("oem_rma", "asset_key"), ("oem_serial_history", "asset_key"),
               ("pm_record", "asset_key"), ("pm_snapshot", "asset_key"), ("spare_inward", "asset_key"), ("spare_outward", "asset_key"),
               ("svc_call", "asset_key"), ("asset_alias", "asset_key")]
KEY_RE = re.compile(r"[A-Z0-9][A-Z0-9._-]{1,39}")      # no "/": a slash in a key means "component of", see _mapping()
TODAY = edit.TODAY


# ---------------------------------------------------------------- helpers
def _key(raw, field):
    s = re.sub(r"\s+", "", str(raw or "")).upper()
    if not KEY_RE.fullmatch(s):
        raise Invalid("Some values are not valid.", {field: "use letters, digits and - . _ (2 to 40 characters)"})
    return s


def _date(raw, field):
    if raw in (None, ""):
        return TODAY()
    try:
        d = dt.date.fromisoformat(str(raw)[:10])
    except ValueError:
        raise Invalid("Enter a valid date.", {field: "enter a valid date (YYYY-MM-DD)"}) from None
    if not (dt.date(2000, 1, 1) <= d <= TODAY() + dt.timedelta(days=366)):
        raise Invalid("Date is out of range.", {field: "date is outside 2000-01-01 .. one year from today"})
    return d


def _text(raw, field, limit):
    s = re.sub(r"\s+", " ", str(raw or "").strip()).upper()
    if len(s) > limit:
        raise Invalid("Some values are not valid.", {field: f"at most {limit} characters"})
    return s or None


def _asset(con, key, lock=True):
    r = edit._rows(con, "SELECT * FROM asset WHERE asset_key = %s" + (" FOR UPDATE" if lock else ""), (key,))
    return r[0] if r else None


def _components(con, key):
    """Keys of the component lines (monitor, GPU, VM ...) that are keyed '<key>/...' under this asset."""
    return [r["asset_key"] for r in edit._rows(con, "SELECT asset_key FROM asset WHERE parent_asset_key = %s", (key,)) if r["asset_key"].startswith(key + "/")]


def _mapping(con, old, new):
    """{old key: new key} for the asset and for every component keyed '<old>/...' (they get the same new prefix). Refuses if any target exists."""
    m = {old: new, **{c: new + c[len(old):] for c in _components(con, old)}}
    taken = edit._rows(con, "SELECT asset_key FROM asset WHERE asset_key = ANY(%s)", (list(m.values()),))
    if taken:
        raise Conflict(f"{taken[0]['asset_key']} already exists in the register.")
    return m


def _rekey(con, mapping):
    """Move every reference to the keys in `mapping` (old -> new) across all tables, the manual-edit locks and the audit trail."""
    for old, new in mapping.items():
        for table, col in KEY_COLUMNS:
            con.execute(f"UPDATE {table} SET {col} = %s WHERE {col} = %s", (new, old))
        con.execute("UPDATE portal_lock SET record_key = %s WHERE dataset = 'assets' AND record_key = %s", (new, old))
        con.execute("UPDATE portal_audit SET record_key = %s WHERE dataset = 'assets' AND record_key = %s", (new, old))


def _count(con, sql, params):
    with con.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def _open_work(con, keys):
    keys = list(keys)
    return (_count(con, "SELECT count(*) FROM svc_call WHERE asset_key = ANY(%s) AND is_current = 1 AND call_status = 'OPEN'", (keys,)),
            _count(con, "SELECT count(*) FROM oem_rma WHERE asset_key = ANY(%s) AND is_current = 1 AND coalesce(return_status, 'PENDING') <> 'RETURNED'", (keys,)))


def _alias(con, alias_key, asset_key, valid_from, valid_to, reason, by):
    con.execute("INSERT INTO asset_alias (alias_key, asset_key, valid_from, valid_to, reason, created_by) VALUES (%s,%s,%s,%s,%s,%s)", (alias_key, asset_key, valid_from, valid_to, reason, by))


def _same_machine_elsewhere(con, serial, ongc, exclude):
    """Another CURRENT asset already carrying this serial number or ONGC asset ID (one physical machine = one live record)."""
    for col, v in (("serial_no", serial), ("ongc_asset_id", ongc)):
        if not v:
            continue
        r = edit._rows(con, f"SELECT asset_key FROM asset WHERE is_current = 1 AND {col} = %s AND asset_key <> ALL(%s) LIMIT 1", (v, list(exclude)))
        if r:
            raise Conflict(f"{r[0]['asset_key']} already has the same {'serial number' if col == 'serial_no' else 'ONGC asset ID'} ({v}). One machine can only have one live record.")


def _lock_archived(con, key, editor):
    con.execute("""INSERT INTO portal_lock (dataset, record_key, field, value, editor) VALUES ('assets',%s,%s,'true'::jsonb,%s)
                   ON CONFLICT (dataset, record_key, field) DO UPDATE SET editor = EXCLUDED.editor, edited_at = now()""", (key, L.ARCHIVED, editor))


def _carry(src, carry):
    """Seat details copied from `src` onto `new_row`. Returns the columns to change."""
    upd = {}
    if carry.get("user"):
        upd.update({c: src.get(c) for c in ("cpf_no", "user_department", "user_category")})
    if carry.get("location"):
        upd.update({c: src.get(c) for c in ("location_code", "floor_area", "room")})
    if carry.get("engineer"):
        upd["engineer_name"] = src.get("engineer_name")
    return upd


def _finish_row(con, key, row, upd, lock_fields, editor, as_of):
    """Write `upd` plus the derived columns (user name, flags, PM status ...) and lock the fields a loader must not overwrite."""
    new = {**row, **upd}
    derived = edit._derive(con, "assets", new, set(upd) & {"cpf_no"}, as_of)
    write = {c: v for c, v in {**upd, **derived}.items() if not edit._eq(row.get(c), v)}
    edit._update_row(con, "assets", key, write)
    changed = {c: v for c, v in upd.items() if not edit._eq(row.get(c), v)}
    for f in lock_fields:
        if f in changed:
            edit._lock(con, "assets", key, f, changed[f], editor, row.get(f))
    return changed, {c: v for c, v in write.items() if c not in changed}


def _chg(old, new):
    return {"old": edit._jd(old), "new": edit._jd(new)}


# ---------------------------------------------------------------- read side (the dialogs)
def preflight(key):
    """Everything the Replace / Redeploy dialogs show before anything is changed. Read only."""
    row = db.one("SELECT * FROM asset WHERE asset_key = %s", [key])
    if not row:
        raise NotFound(f"Asset '{key}' was not found.")
    keys = [key] + [r["asset_key"] for r in db.query("SELECT asset_key FROM asset WHERE parent_asset_key = %s AND asset_key LIKE %s", [key, key + "/%"])]
    calls = db.one("SELECT count(*) AS n FROM svc_call WHERE asset_key = ANY(%s) AND is_current = 1 AND call_status = 'OPEN'", [keys])["n"]
    rma = db.one("SELECT count(*) AS n FROM oem_rma WHERE asset_key = ANY(%s) AND is_current = 1 AND coalesce(return_status, 'PENDING') <> 'RETURNED'", [keys])["n"]
    hist = {"change log": "SELECT count(*) AS n FROM asset_change_log WHERE asset_key = %s", "snapshots": "SELECT count(*) AS n FROM asset_snapshot WHERE asset_key = %s",
            "PM snapshots": "SELECT count(*) AS n FROM pm_snapshot WHERE asset_key = %s", "calls": "SELECT count(*) AS n FROM svc_call WHERE asset_key = %s",
            "RMA cases": "SELECT count(*) AS n FROM oem_rma WHERE asset_key = %s"}
    fields = ("asset_key", "ci_no", "hostname", "asset_class", "asset_type", "record_level", "make", "model", "serial_no", "ongc_asset_id", "ongc_census_no", "cpf_no", "user_name",
              "user_designation", "user_department", "engineer_name", "location_code", "floor_area", "room", "cover_type", "cover_expiry_date", "pm_status", "asset_status",
              "is_current", "retired_on", "retired_reason", "replaced_by_key", "os", "processor", "ram", "storage")
    return {"key": key, "row": {f: row.get(f) for f in fields}, "retired": row.get("is_current") == 0 and row.get("asset_status") == "REPLACED",
            "open_calls": calls, "open_rma": rma, "components": len(keys) - 1,
            "history": {k: db.one(q, [key])["n"] for k, q in hist.items()},
            "aliases": db.query("SELECT alias_key, valid_from, valid_to, reason FROM asset_alias WHERE asset_key = %s ORDER BY valid_to NULLS LAST, alias_id", [key]),
            "reasons": [{"value": k, "label": v, "needs_approval": k in NEEDS_APPROVAL} for k, v in RETIRE_REASONS.items()]}


def candidates(q, exclude):
    q = (q or "").strip()[:60]
    if len(q) < 2:
        return []
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return db.query("""SELECT asset_key, asset_class, asset_type, make, model, serial_no, ongc_asset_id, hostname, user_name FROM asset
                       WHERE is_current = 1 AND record_level = 'ASSET' AND asset_key <> %s
                         AND (asset_key ILIKE %s OR hostname ILIKE %s OR serial_no ILIKE %s OR model ILIKE %s OR ongc_asset_id ILIKE %s)
                       ORDER BY asset_key LIMIT 20""", [exclude or "", like, like, like, like, like])


# ---------------------------------------------------------------- replace
def replace(old_key, new_key, body, editor, ip):
    """`old_key` (the machine going out) hands its name to `new_key` (the machine coming in). Returns {"retired_key", "live_key"}."""
    body = body or {}
    old_key, new_key = _key(old_key, "key"), _key(new_key, "replacement_key")
    if old_key == new_key:
        raise Invalid("Choose a different asset as the replacement.", {"replacement_key": "must be a different asset"})
    reason = str(body.get("reason") or "").upper()
    if reason not in RETIRE_REASONS:
        raise Invalid("Choose why the old machine is being retired.", {"reason": "required"})
    when = _date(body.get("date"), "date")
    disposal = _text(body.get("disposal_ref"), "disposal_ref", 80)
    wiped = "YES" if body.get("data_wiped") else "NO"
    carry = {k: bool((body.get("carry") or {}).get(k)) for k in ("user", "location", "engineer")}
    as_of = TODAY()
    with db.write() as con:
        rows = {k: _asset(con, k) for k in sorted({old_key, new_key})}       # locked in a fixed order: two admins at once cannot deadlock
        old, new = rows[old_key], rows[new_key]
        for k, r, f in ((old_key, old, "key"), (new_key, new, "replacement_key")):
            if not r or r["is_current"] != 1:
                raise NotFound(f"Asset '{k}' was not found (it may be archived or retired).")
            if r["record_level"] != "ASSET":
                raise Invalid("Only a whole asset can be replaced, not a component line.", {f: "not a whole asset"})
        for col, label in (("serial_no", "serial number"), ("ongc_asset_id", "ONGC asset ID")):
            if old.get(col) and new.get(col) and old[col] == new[col]:
                raise Invalid("These records describe the same machine.", {"replacement_key": f"has the same {label}"})
        serial = _text(body.get("serial_no"), "serial_no", 60) or new.get("serial_no")
        if not serial:
            raise Invalid("Enter the new machine's serial number - it is the machine's real identity.", {"serial_no": "required"})
        if old.get("serial_no") and serial == old["serial_no"]:
            raise Invalid("That is the old machine's serial number.", {"serial_no": "belongs to the machine being replaced"})
        _same_machine_elsewhere(con, serial, new.get("ongc_asset_id"), {old_key, new_key})
        calls, rma = _open_work(con, [old_key, *_components(con, old_key)])
        if calls or rma:
            raise Conflict("The old machine still has " + " and ".join(x for x in (f"{calls} open call(s)" if calls else "", f"{rma} OEM RMA case(s) not yet returned" if rma else "") if x)
                           + ". Close or complete them first, then replace it.")
        hostname = _text(body.get("hostname"), "hostname", 60) or old_key
        retired = f"{old_key}-RET-{when:%Y%m%d}"
        n = 1
        while edit._exists(con, "SELECT 1 FROM asset WHERE asset_key = %s UNION ALL SELECT 1 FROM asset_alias WHERE alias_key = %s", (retired, retired)):
            n += 1
            retired = f"{old_key}-RET-{when:%Y%m%d}-{n}"

        # 1. the old machine moves out of the way (with its history), and is retired
        _rekey(con, _mapping(con, old_key, retired))
        edit._update_row(con, "assets", retired, {"is_current": 0, "asset_status": "REPLACED", "retired_on": when, "retired_reason": reason,
                                                  "replaced_by_key": old_key, "disposal_ref": disposal, "data_wiped": wiped})
        _lock_archived(con, retired, editor)
        # 2. the new machine takes the vacated name
        _rekey(con, _mapping(con, new_key, old_key))
        live = _asset(con, old_key)
        upd = {"ci_no": old_key, "hostname": hostname, "serial_no": serial, "pm_date": None, "pm_done_by": None, "pm_signed_by": None, "pm_tracker_date": None,
               "remarks": ((live.get("remarks") + "; ") if live.get("remarks") else "") + f"REPLACED {old_key} ({old.get('make') or ''} {old.get('model') or ''} S/N {old.get('serial_no') or '-'}) ON {when}"}
        upd.update(_carry(old, carry))
        changed, derived = _finish_row(con, old_key, live, upd, ("ci_no", "hostname", "serial_no", "cpf_no", "user_department", "user_category", "location_code", "floor_area", "room", "engineer_name"), editor, as_of)
        # 3. former names and the audit trail
        _alias(con, old_key, retired, old.get("install_date") or old.get("first_seen_date"), when, "REPLACED", editor)
        _alias(con, new_key, old_key, new.get("first_seen_date"), when, "RENAMED ON REPLACEMENT", editor)
        edit._audit(con, editor, ip, "assets", retired, "RETIRE",
                    {"asset_key": _chg(old_key, retired), "asset_status": _chg(old.get("asset_status"), "REPLACED"), "replaced_by_key": _chg(None, old_key),
                     "retired_reason": _chg(None, reason), "disposal_ref": _chg(None, disposal), "data_wiped": _chg(None, wiped), "date": when.isoformat()}, body.get("note"))
        edit._audit(con, editor, ip, "assets", old_key, "REPLACE",
                    {"asset_key": _chg(new_key, old_key), **{f: _chg(live.get(f), v) for f, v in changed.items() if f != "remarks"}, "date": when.isoformat(),
                     "replaces": {"old": None, "new": f"{retired} (S/N {old.get('serial_no') or '-'}, ONGC {old.get('ongc_asset_id') or '-'})"}}, body.get("note"))
    return {"retired_key": retired, "live_key": old_key, "carried": [k for k, v in carry.items() if v]}


# ---------------------------------------------------------------- redeploy
def redeploy(retired_key, new_key, body, editor, ip):
    """A retired (archived) machine goes back into service under `new_key`. Serial / ONGC ID / census number are never accepted here."""
    body = body or {}
    for forbidden in ("serial_no", "ongc_asset_id", "ongc_census_no"):
        if forbidden in body:
            raise Invalid("A machine's serial number, ONGC asset ID and census number cannot be changed here.", {forbidden: "cannot be changed - it identifies the machine"})
    retired_key, new_key = _key(retired_key, "key"), _key(new_key, "new_key")
    when = _date(body.get("date"), "date")
    if retired_key == new_key:
        raise Invalid("Choose a new key.", {"new_key": "must differ from the retired key"})
    if not body.get("data_wiped"):
        raise Invalid("Confirm the old data was wiped before the machine goes back into service.", {"data_wiped": "required"})
    as_of = TODAY()
    errors, clean = {}, {}
    with db.write() as con:
        row = _asset(con, retired_key)
        if not row or row["is_current"] != 0 or not edit._exists(con, "SELECT 1 FROM portal_lock WHERE dataset='assets' AND record_key=%s AND field=%s", (retired_key, L.ARCHIVED)):
            raise NotFound(f"'{retired_key}' is not a retired or archived asset.")
        if row["record_level"] != "ASSET":
            raise Invalid("Only a whole asset can be redeployed.", {"key": "not a whole asset"})
        approval = _text(body.get("approval_ref"), "approval_ref", 80)
        if (row.get("retired_reason") in NEEDS_APPROVAL or not row.get("retired_reason")) and not approval:
            raise Invalid("This machine was condemned or archived. The manager's written approval reference is required to reuse it.", {"approval_ref": "required"})
        if edit._exists(con, "SELECT 1 FROM asset WHERE asset_key = %s", (new_key,)):
            raise Conflict(f"{new_key} already exists in the register.")
        if edit._exists(con, "SELECT 1 FROM asset_alias WHERE alias_key = %s AND asset_key <> %s", (new_key, retired_key)):
            raise Conflict(f"{new_key} was formerly the name of a different machine. Choose another name.")
        _same_machine_elsewhere(con, row.get("serial_no"), row.get("ongc_asset_id"), {retired_key})
        for f in ("cpf_no", "engineer_name", "location_code", "floor_area", "room", "os_family", "os", "processor", "ram", "storage"):
            if f in body and body[f] not in (None, ""):
                try:
                    clean[f] = edit.clean(con, "assets", f, body[f])
                except ValueError as e:
                    errors[f] = str(e)
        if errors:
            raise Invalid("Some values are not valid.", errors)
        purpose = _text(body.get("purpose"), "purpose", 80)
        hostname = _text(body.get("hostname"), "hostname", 60) or new_key
        was = {"asset_key": retired_key, "hostname": row.get("hostname"), "asset_status": row.get("asset_status"), "cpf_no": row.get("cpf_no"), "user_name": row.get("user_name")}

        _rekey(con, _mapping(con, retired_key, new_key))
        con.execute("DELETE FROM portal_lock WHERE dataset='assets' AND record_key=%s AND field=%s", (new_key, L.ARCHIVED))
        con.execute("DELETE FROM asset_alias WHERE alias_key = %s AND asset_key = %s", (new_key, new_key))     # returning to a name it used before
        live = _asset(con, new_key)
        upd = {"ci_no": new_key, "hostname": hostname, "is_current": 1, "asset_status": "IN_USE", "retired_on": None, "retired_reason": None, "replaced_by_key": None,
               "disposal_ref": None, "data_wiped": "YES", **clean,
               "remarks": ((live.get("remarks") + "; ") if live.get("remarks") else "") + f"REDEPLOYED {when}" + (f" AS {purpose}" if purpose else "") + (f" (APPROVAL {approval})" if approval else "")}
        for f in ("cpf_no", "engineer_name", "location_code", "floor_area", "room"):
            if f not in clean and f in body:          # an explicitly blank field in the dialog clears the old seat detail
                upd[f] = None
        if "cpf_no" in upd and upd["cpf_no"] is None:
            upd.update(user_name=None, user_designation=None, user_level=None, user_mobile=None, user_retirement_date=None, user_hr_status=None, user_department=None, user_category=None)
        if body.get("restart_pm", True):
            upd.update(pm_date=None, pm_done_by=None, pm_signed_by=None, pm_tracker_date=None)
        if not body.get("keep_cover", True):
            upd.update(cover_type=None, cover_expiry_date=None, cover_status=None, rate_component=None, rate_value=None)
        changed, derived = _finish_row(con, new_key, live, upd, ("ci_no", "hostname", "cpf_no", "user_department", "user_category", "location_code", "floor_area", "room", "engineer_name", "os_family", "os", "processor", "ram", "storage"), editor, as_of)
        _alias(con, retired_key, new_key, row.get("retired_on"), when, "REDEPLOYED", editor)
        edit._audit(con, editor, ip, "assets", new_key, "REDEPLOY",
                    {"asset_key": _chg(retired_key, new_key), "asset_status": _chg(was["asset_status"], "IN_USE"),
                     **{f: _chg(live.get(f), v) for f, v in changed.items() if f not in ("remarks", "is_current", "asset_status")},
                     "identity": {"old": None, "new": f"S/N {row.get('serial_no') or '-'}, ONGC {row.get('ongc_asset_id') or '-'}, census {row.get('ongc_census_no') or '-'} (unchanged)"},
                     "date": when.isoformat()}, body.get("note") or (f"approval {approval}" if approval else None))
    return {"live_key": new_key, "former_key": retired_key}
