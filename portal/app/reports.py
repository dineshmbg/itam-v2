"""Custom report builder: every column of a register is available; filters, grouping and sorting are described as data and turned into SQL
from a whitelist (column names come from information_schema, never from the request). Values are always bound parameters."""
import datetime as dt
import re

from . import db
from .datasets import DATASETS, HIDDEN_DETAIL
from .export import safe_name

SETS = {
    "assets": ("asset", "is_current = 1", "Assets"), "calls": ("svc_call", "is_current = 1", "Calls"), "inward": ("spare_inward", "is_current = 1", "Inward"),
    "outward": ("spare_outward", "is_current = 1", "Outward"), "rma": ("oem_rma", "is_current = 1", "OEM RMA"),
    "engineers": ("v_engineer", "true", "Engineers"), "employees": ("employee", "record_status = 'ACTIVE'", "Employees"),
}
PERSONAL = {"mobile_no", "personal_email", "date_of_birth", "user_mobile"}
INTERNAL = HIDDEN_DETAIL | {"is_current", "is_on_roster", "first_seen_date", "last_seen_date"}
TYPES = {"integer": "number", "bigint": "number", "numeric": "number", "smallint": "number", "double precision": "number", "date": "date", "timestamp with time zone": "date"}
OPS = {
    "text": ["eq", "neq", "in", "contains", "starts", "blank", "notblank"],
    "number": ["eq", "neq", "gt", "gte", "lt", "lte", "between", "blank", "notblank"],
    "date": ["eq", "before", "after", "between", "blank", "notblank", "in_last_days", "in_next_days"],
}
LIKE_ESC = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})
MAX_ROWS = 100000

_catalog = {}


class ReportError(Exception):
    http_error = True


def catalog(dataset, admin):
    if dataset not in SETS:
        raise ReportError("Unknown register.")
    if dataset not in _catalog:
        table = SETS[dataset][0]
        cols = db.query("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position", [table])
        _catalog[dataset] = [{"key": c["column_name"], "type": TYPES.get(c["data_type"], "text")} for c in cols if c["column_name"] not in INTERNAL]
    return [f for f in _catalog[dataset] if admin or f["key"] not in PERSONAL]


def label_of(key):
    acr = {"ci", "cipl", "ongc", "os", "ip", "ram", "cpf", "hr", "pm", "amc", "dc", "rma", "sr", "tat", "sn", "id", "mgmt", "oem", "awb"}
    words = [("no." if w == "no" else w.upper() if w in acr else w) for w in key.split("_")]
    return " ".join(words).upper()


def describe(dataset, admin):
    cat = catalog(dataset, admin)
    return {"dataset": dataset, "label": SETS[dataset][2], "fields": [{**f, "label": label_of(f["key"]), "ops": OPS[f["type"]]} for f in cat]}


def values(dataset, field, admin, q=""):
    f = _field(dataset, field, admin)
    table, base, _ = SETS[dataset]
    if f["type"] != "text":
        return []
    rows = db.query(f'SELECT "{field}" AS v, count(*) AS n FROM {table} WHERE {base} AND "{field}" IS NOT NULL AND "{field}"::text ILIKE %s GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 40',
                    ["%" + q.translate(LIKE_ESC) + "%"])
    return [{"v": r["v"], "n": r["n"]} for r in rows]


def _field(dataset, key, admin):
    for f in catalog(dataset, admin):
        if f["key"] == key:
            return f
    raise ReportError(f"Unknown field '{key}'.")


def _cond(f, flt):
    op, k, typ = flt.get("op"), f'"{f["key"]}"', f["type"]
    if op not in OPS[typ]:
        raise ReportError(f"'{op}' cannot be used with {f['key']}.")
    v, v2, vs = flt.get("value"), flt.get("value2"), flt.get("values")
    if op == "blank":
        return f"{k} IS NULL", []
    if op == "notblank":
        return f"{k} IS NOT NULL", []
    if op == "in":
        vals = [str(x) for x in (vs or []) if str(x) != ""][:200]
        if not vals:
            raise ReportError(f"{f['key']}: choose at least one value.")
        return f"{k}::text = ANY(%s)", [[x.upper() for x in vals]]
    if typ == "date":
        if op in ("in_last_days", "in_next_days"):
            n = int(v)
            return (f"{k} BETWEEN current_date - %s AND current_date", [n]) if op == "in_last_days" else (f"{k} BETWEEN current_date AND current_date + %s", [n])
        d = dt.date.fromisoformat(str(v)[:10])
        if op == "eq":
            return f"{k} = %s", [d]
        if op == "before":
            return f"{k} < %s", [d]
        if op == "after":
            return f"{k} > %s", [d]
        return f"{k} BETWEEN %s AND %s", [d, dt.date.fromisoformat(str(v2)[:10])]
    if typ == "number":
        n = float(v)
        sym = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
        if op == "between":
            return f"{k} BETWEEN %s AND %s", [n, float(v2)]
        return f"{k} {sym[op]} %s", [n]
    s = str(v if v is not None else "").strip().upper()
    if s == "" and op in ("eq", "neq", "contains", "starts"):
        raise ReportError(f"{f['key']}: enter a value.")
    if op == "eq":
        return f"upper({k}) = %s", [s]
    if op == "neq":
        return f"(upper({k}) <> %s OR {k} IS NULL)", [s]
    if op == "contains":
        return f"upper({k}) LIKE %s", ["%" + s.translate(LIKE_ESC) + "%"]
    return f"upper({k}) LIKE %s", [s.translate(LIKE_ESC) + "%"]


MAX_BLANK_COLUMNS = 10


def _blank_columns(defn):
    """Extra columns with no data of their own - just a custom heading, left empty for the reader to fill in by hand (a sign-off, a remark).
    They never touch the SQL: every export writer already treats a row missing a column's key as blank, so appending (key, label) pairs here is enough."""
    out = []
    for i, b in enumerate((defn.get("blank_columns") or [])[:MAX_BLANK_COLUMNS]):
        label = re.sub(r"\s+", " ", str(b.get("label") or "").strip()).upper()[:60]
        if not label:
            raise ReportError("Give every blank column a heading.")
        out.append((f"_blank_{i}", label))
    return out


def build(defn, admin):
    """-> (sql, params, columns[(key,label)]). `defn`: {dataset, columns[], filters[], match, group_by[], metrics[], sort[]}"""
    ds = defn.get("dataset")
    table, base, _ = SETS.get(ds) or (None, None, None)
    if not table:
        raise ReportError("Choose a register.")
    where, params = [base], []
    conds = []
    for flt in defn.get("filters") or []:
        c, p = _cond(_field(ds, flt.get("field"), admin), flt)
        conds.append(c); params += p
    if conds:
        where.append("(" + (" OR " if defn.get("match") == "any" else " AND ").join(conds) + ")")
    group = defn.get("group_by") or []
    if group:
        sel, gcols, cols = [], [], []
        for g in group[:4]:
            key, _, part = str(g).partition(":")
            f = _field(ds, key, admin)
            if part:
                if f["type"] != "date" or part not in ("month", "year", "quarter"):
                    raise ReportError(f"Cannot group {key} by {part}.")
                fmt = {"month": "YYYY-MM", "year": "YYYY", "quarter": 'YYYY-"Q"Q'}[part]
                expr = 'to_char("%s", \'%s\')' % (key, fmt)      # key is a catalog column; fmt comes from the constant table above
                name = f"{key}_{part}"
            else:
                expr, name = f'"{key}"', key
            sel.append(f"{expr} AS \"{name}\""); gcols.append(expr); cols.append((name, label_of(key) + (f" ({part})" if part else "")))
        sel.append("count(*) AS n"); cols.append(("n", "RECORDS"))
        for i, m in enumerate((defn.get("metrics") or [])[:6]):
            fn = m.get("fn")
            if fn not in ("sum", "avg", "min", "max", "count_distinct"):
                raise ReportError("Unknown summary function.")
            f = _field(ds, m.get("field"), admin)
            if fn in ("sum", "avg") and f["type"] != "number":
                raise ReportError(f"{fn} needs a number field.")
            expr = f'count(DISTINCT "{f["key"]}")' if fn == "count_distinct" else f'{fn}("{f["key"]}")' + ("::numeric" if fn == "avg" else "")
            sel.append(f"{expr} AS m{i}"); cols.append((f"m{i}", f"{fn.replace('_', ' ')} of {label_of(f['key'])}"))
        order = "ORDER BY n DESC, 1"
        sql = f"SELECT {', '.join(sel)} FROM {table} WHERE {' AND '.join(where)} GROUP BY {', '.join(gcols)} {order}"
        return sql, params, cols + _blank_columns(defn)
    chosen = defn.get("columns") or [f["key"] for f in catalog(ds, admin)[:12]]
    cols = []
    for key in chosen[:120]:
        f = _field(ds, key, admin)
        cols.append((f["key"], label_of(f["key"])))
    sort = []
    for s in (defn.get("sort") or [])[:4]:
        f = _field(ds, s.get("field"), admin)
        sort.append(f'"{f["key"]}" {"DESC" if s.get("dir") == "desc" else "ASC"} NULLS LAST')
    pk = DATASETS[ds]["pk"] if ds in DATASETS else None
    if pk and not sort:
        sort.append(f'"{pk}"')
    select = ", ".join(f'"{k}"' for k, _ in cols)
    sql = f"SELECT {select} FROM {table} WHERE {' AND '.join(where)}" + (f" ORDER BY {', '.join(sort)}" if sort else "")
    return sql, params, cols + _blank_columns(defn)


def run(defn, admin, limit=200):
    sql, params, cols = build(defn, admin)
    limit = max(1, min(int(limit), MAX_ROWS))
    total = db.one(f"SELECT count(*) AS n FROM ({sql}) q", params)["n"]
    rows = db.query(f"{sql} LIMIT %s", params + [limit])
    return {"columns": [{"key": k, "label": l} for k, l in cols], "rows": rows, "total": total}


def table_for_export(defn, admin, title):
    r = run(defn, admin, MAX_ROWS)
    return {"title": title, "columns": [(c["key"], c["label"]) for c in r["columns"]], "rows": r["rows"]}, r["total"]


def filename(defn):
    return safe_name(f"{SETS[defn['dataset']][2]}_report_{dt.date.today():%Y%m%d}")


# ---------------------------------------------------------------- saved reports
DDL = ["""CREATE TABLE IF NOT EXISTS portal_report (
          report_id SERIAL PRIMARY KEY, name TEXT NOT NULL, owner TEXT NOT NULL, definition JSONB NOT NULL, shared BOOLEAN NOT NULL DEFAULT FALSE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (owner, name))"""]


def list_saved(user):
    return db.query("SELECT report_id AS id, name, owner, definition, shared, updated_at FROM portal_report WHERE owner = %s OR shared ORDER BY lower(name)", [user["username"]])


def save(user, name, defn, shared):
    name = re.sub(r"\s+", " ", (name or "").strip()).upper()
    if not 2 <= len(name) <= 80:
        raise ReportError("Give the report a name (2 to 80 characters).")
    import json
    build(defn, user["role"] == "ADMIN")            # refuse definitions that would not run
    with db.write() as con:
        con.execute("""INSERT INTO portal_report (name, owner, definition, shared) VALUES (%s,%s,%s::jsonb,%s)
                       ON CONFLICT (owner, name) DO UPDATE SET definition = EXCLUDED.definition, shared = EXCLUDED.shared, updated_at = now()""",
                    (name, user["username"], json.dumps(defn), bool(shared)))
    return {"name": name}


def delete(user, report_id):
    with db.write() as con:
        n = con.execute("DELETE FROM portal_report WHERE report_id = %s AND (owner = %s OR %s)", (report_id, user["username"], user["role"] == "ADMIN")).rowcount
    if not n:
        raise ReportError("Report not found or not yours.")
