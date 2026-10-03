"""Manual-override layer: keeps edits made in the portal alive across loader runs.

Every field edited in the portal is stored in portal_lock. Loaders call `apply()` on the rows they are about to write: locked fields keep their
manual value, dependent derived columns are recomputed with tools/itam_rules.py, records created in the portal are never retired because they are
missing from a file, and records archived in the portal stay archived. When the incoming (source) value differs from a locked value the difference is
remembered (source_value) so the portal can show "the source file now says ...".
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import itam_rules as R  # noqa: E402

CREATED, ARCHIVED = "__created__", "__archived__"
TABLE_DATASET = {"asset": "assets", "svc_call": "calls", "spare_inward": "inward", "spare_outward": "outward", "oem_rma": "rma", "cipl_employee": "engineers"}
DATASET_TABLE = {v: k for k, v in TABLE_DATASET.items()}
DATASET_KEY = {"assets": "asset_key", "calls": "sr_id", "inward": "inward_id", "outward": "outward_id", "rma": "rma_line_id", "engineers": "ecode"}
INT_FIELDS = {"cpf_no"}


def ensure_tables(con):
    con.execute("""CREATE TABLE IF NOT EXISTS portal_lock (
        dataset TEXT NOT NULL, record_key TEXT NOT NULL, field TEXT NOT NULL, value JSONB, editor TEXT, edited_at TIMESTAMPTZ DEFAULT now(),
        source_value JSONB, source_seen_at TIMESTAMPTZ, PRIMARY KEY (dataset, record_key, field))""")
    con.execute("""CREATE TABLE IF NOT EXISTS portal_audit (
        audit_id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now(), editor TEXT NOT NULL, client_ip TEXT, dataset TEXT NOT NULL,
        record_key TEXT NOT NULL, action TEXT NOT NULL, changes JSONB, reason TEXT)""")


def jsonable(v):
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()[:10]
    if hasattr(v, "quantize"):
        return float(v)
    return v


def from_json(field, v):
    if v is None:
        return None
    if field.endswith("_date") and isinstance(v, str):
        return dt.date.fromisoformat(v[:10])
    if field in INT_FIELDS:
        return int(v)
    return v


def load_overrides(con, dataset):
    """-> (locks {key: {field: value}}, created {keys}, archived {keys})."""
    locks, created, archived = {}, set(), set()
    with con.cursor() as cur:
        cur.execute("SELECT record_key, field, value FROM portal_lock WHERE dataset = %s", (dataset,))
        for key, field, value in cur.fetchall():
            if field == CREATED:
                created.add(key)
            elif field == ARCHIVED:
                archived.add(key)
            else:
                locks.setdefault(key, {})[field] = value
    return locks, created, archived


def build_ctx(con, dataset, row, resolve_user=False, spare=True):
    """Facts from other tables that the derive rules need. `con` is any psycopg connection."""
    ctx = {}
    with con.cursor() as cur:
        if dataset == "assets" and resolve_user:
            ctx["resolve_user"] = True
            if row.get("cpf_no") is not None:
                cur.execute("SELECT employee_name, designation, level, mobile_no, date_of_retirement, record_status FROM employee WHERE cpf_no = %s", (row["cpf_no"],))
                r = cur.fetchone()
                ctx["hr"] = dict(zip(("employee_name", "designation", "level", "mobile_no", "date_of_retirement", "record_status"), r)) if r else None
        if dataset == "calls":
            if spare:
                cur.execute("SELECT EXISTS (SELECT 1 FROM spare_inward WHERE sr_id = %s AND received_date IS NOT NULL AND is_current = 1)", (row["sr_id"],))
                ctx["inward_received"] = cur.fetchone()[0]
            if row.get("asset_key"):
                cur.execute("SELECT EXISTS (SELECT 1 FROM asset WHERE asset_key = %s AND is_current = 1)", (row["asset_key"],))
                ctx["asset_known"] = cur.fetchone()[0]
        if dataset in ("inward", "outward"):
            cur.execute("SELECT EXISTS (SELECT 1 FROM svc_call WHERE sr_id = %s AND is_current = 1)", (row.get("sr_id"),))
            ctx["call_known"] = cur.fetchone()[0]
            if dataset == "outward" and row.get("asset_key"):
                cur.execute("SELECT EXISTS (SELECT 1 FROM asset WHERE asset_key = %s AND is_current = 1)", (row["asset_key"],))
                ctx["asset_known"] = cur.fetchone()[0]
    return ctx


def apply_row(con, dataset, rec, fields, as_of):
    """rec: dict with lower-case column names (mutated). fields: {field: json value}. Returns {field: incoming value} for fields whose source value differed."""
    differed = {}
    for f, v in fields.items():
        new = from_json(f, v)
        if jsonable(rec.get(f)) != jsonable(new):
            differed[f] = rec.get(f)
        rec[f] = new
    ctx = build_ctx(con, dataset, rec, resolve_user="cpf_no" in fields, spare=False)   # loaders keep the spare status computed from the file
    rec.update(R.DERIVE[dataset](rec, ctx, as_of))
    return differed


def apply_records(con, dataset, records, key, as_of, upper=True):
    """records: list of dicts keyed by UPPER (upper=True) or lower column names. Locked fields are re-applied in place.
    Returns (created_keys, archived_keys)."""
    locks, created, archived = load_overrides(con, dataset)
    if not locks:
        return created, archived
    n = 0
    for rec in records:
        k = rec[key.upper() if upper else key]
        fields = locks.get(k)
        if not fields:
            continue
        low = {c.lower(): v for c, v in rec.items()} if upper else rec
        differed = apply_row(con, dataset, low, fields, as_of)
        for c, v in low.items():
            rec[c.upper() if upper else c] = v
        for f, incoming in differed.items():
            con.execute("UPDATE portal_lock SET source_value = %s::jsonb, source_seen_at = now() WHERE dataset=%s AND record_key=%s AND field=%s",
                        (json.dumps(jsonable(incoming)), dataset, k, f))
        n += 1
    return created, archived


def alias_conflicts(con, rows):
    """Guard for the asset loader after a Replace / Redeploy (portal/app/lifecycle.py). `rows`: [(asset_key, serial_no, ongc_asset_id, source_row)]
    taken from the incoming file. Returns readable messages for rows that would recreate a former name or put the wrong machine on a reused name;
    an empty list means the file is safe to load. Does nothing when no asset has ever been replaced."""
    with con.cursor() as cur:
        cur.execute("SELECT to_regclass('asset_alias') IS NOT NULL")
        if not cur.fetchone()[0]:
            return []
        cur.execute("SELECT alias_key, asset_key FROM asset_alias WHERE alias_key <> asset_key")
        aliases = {}
        for alias_key, asset_key in cur.fetchall():
            aliases.setdefault(alias_key, set()).add(asset_key)
        hit = [r for r in rows if r[0] in aliases]
        if not hit:
            return []
        cur.execute("SELECT asset_key, serial_no, ongc_asset_id FROM asset WHERE asset_key = ANY(%s)", ([r[0] for r in hit],))
        held = {k: (sn, oid) for k, sn, oid in cur.fetchall()}
    norm = lambda v: str(v).strip().upper() if v not in (None, "") else None  # noqa: E731
    out = []
    for key, serial, ongc, src in hit:
        where = f"row {int(src)}" if src not in (None, "") else key
        if key not in held:
            now = ", ".join(sorted(aliases[key]))
            out.append(f"{where}: {key} is a former name of the machine that is now {now}. Use the new name in the sheet.")
            continue
        db_serial, db_ongc = held[key]
        for label, a, b in (("serial number", norm(serial), norm(db_serial)), ("ONGC asset ID", norm(ongc), norm(db_ongc))):
            if a and b and a != b:
                out.append(f"{where}: {key} was re-assigned to another machine (now {label} {b}) but the file still says {a}. Update or remove that row.")
                break
    return out
