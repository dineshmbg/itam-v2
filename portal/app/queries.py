"""Generic, whitelist-driven query engine for the registers (list + live facets + detail + global search)."""
import re

from . import auth, config, db, imac
from .datasets import ADMIN_ONLY_DATASETS, DATASETS, HIDDEN_DETAIL, NEVER_EXPOSE, search_expr

BLANK = "~blank"
_LIKE_ESC = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


class BadRequest(Exception):
    pass


def dataset(name):
    ds = DATASETS.get(name)
    if not ds:
        raise BadRequest(f"unknown dataset '{name}'")
    return ds


def check_access(name, user):
    """Raise if `user` (None for anonymous) may not open this register at all. Row-level scoping (scope_for) is separate and
    applies afterwards, to registers a User *can* open but only see their own rows within."""
    if name in ADMIN_ONLY_DATASETS and (not user or user.get("role") != "ADMIN") and (not user or user.get("call_parts_access") in (None, "NONE")):
        raise auth.AuthError("This register is for administrators only.", 403, code="forbidden")


def parse_filters(ds, args):
    """args: mapping of query params. f.<facet>=a|b|c ; empty value = explicitly 'all'; absent = dataset default."""
    out = {}
    valid = {f["key"] for f in ds["facets"]}
    for k, v in args.items():
        if k.startswith("f."):
            key = k[2:]
            if key not in valid:
                raise BadRequest(f"unknown filter '{key}'")
            out[key] = [x for x in v.split("|") if x != ""][:50]
    for k, v in ds.get("defaults", {}).items():
        out.setdefault(k, list(v))
    return out


def where(ds, q, filters, skip=None, base=None):
    """`base`: override for the dataset's own base condition - a plain SQL string, or (sql, params) when it needs bound values."""
    if isinstance(base, tuple):
        base_sql, base_params = base
    else:
        base_sql, base_params = (base or ds["base"]), []
    parts, params = [base_sql], list(base_params)
    if q:
        hay = search_expr(_search_cols(ds))
        for tok in re.findall(r"\S+", q.lower())[:6]:
            parts.append(f"{hay} LIKE %s")
            params.append("%" + tok.translate(_LIKE_ESC) + "%")
    for f in ds["facets"]:
        vals = filters.get(f["key"])
        if not vals or f["key"] == skip:
            continue
        expr = f["expr"]
        if f["kind"] == "flags":
            parts.append("(" + " OR ".join(f"{expr} LIKE %s" for _ in vals) + ")")
            params.extend("%" + v.translate(_LIKE_ESC) + "%" for v in vals)
            continue
        real = [v for v in vals if v != BLANK]
        conds = []
        if real:
            conds.append(f"({expr}) = ANY(%s)")
            params.append(real)
        if BLANK in vals:
            conds.append(f"({expr}) IS NULL")
        parts.append("(" + " OR ".join(conds) + ")")
    return " AND ".join(parts), params


def _search_cols(ds):
    return ds["search"]


def archived_base(name, ds):
    """Records archived from the portal (not rows a loader retired)."""
    return f"is_current = 0 AND {ds['pk']} IN (SELECT record_key FROM portal_lock WHERE dataset = '{name}' AND field = '__archived__')"


def scope_for(name, ds, user):
    """Row-level restriction for a non-admin engineer: only their own assigned assets / PM worklist rows, and only the employees those assets
    belong to. None for an administrator (or the read-only demo account, which carries role ADMIN) - they see everything."""
    if not user or user.get("role") == "ADMIN":
        return None
    eng = user.get("engineer_key")
    if name in ("assets", "pm"):
        return ("engineer_name = %s", [eng]) if eng else ("false", [])
    if name == "employees":
        return ("cpf_no IN (SELECT DISTINCT cpf_no FROM asset WHERE is_current = 1 AND cpf_no IS NOT NULL AND engineer_name = %s)", [eng]) if eng else ("false", [])
    return None


def _with_scope(base_sql, scope):
    if not scope:
        return base_sql, []
    return f"({base_sql}) AND ({scope[0]})", list(scope[1])


def list_rows(name, args, user=None, cap=None):
    ds = dataset(name)
    filters = parse_filters(ds, args)
    q = (args.get("q") or "").strip()[:120]
    limit = max(1, min(int(args.get("limit", 100)), cap or config.MAX_PAGE))
    offset = max(0, int(args.get("offset", 0)))
    cols = {c["key"]: c for c in ds["columns"]}
    sort_key = args.get("sort") or ds["sort"][0]
    direction = "DESC" if (args.get("dir") or ds["sort"][1]).lower() == "desc" else "ASC"
    sort_expr = cols[sort_key]["sort"] if sort_key in cols else ds["sort"][0]
    if sort_key not in cols and sort_key != ds["sort"][0]:
        raise BadRequest(f"cannot sort by '{sort_key}'")
    base_sql = "false" if (args.get("scope") == "archived" and ds.get("readonly")) else (archived_base(name, ds) if args.get("scope") == "archived" else ds["base"])
    base = _with_scope(base_sql, scope_for(name, ds, user))
    w, p = where(ds, q, filters, base=base)
    select = ", ".join([f"{c['expr']} AS {c['key']}" for c in ds["columns"]] + [f"{c['ref_id']} AS {c['key']}__ref" for c in ds["columns"] if c.get("ref_id")])
    sql = f"SELECT {select}, {ds['pk']} AS id FROM {ds['table']} WHERE {w} ORDER BY {sort_expr} {direction} NULLS LAST, {ds['pk']} LIMIT %s OFFSET %s"
    rows = db.query(sql, p + [limit, offset])
    out = {"rows": rows}
    if offset == 0 or args.get("count") == "1":
        out["total"] = db.one(f"SELECT count(*) AS n FROM {ds['table']} WHERE {w}", p)["n"]
    if args.get("facets") == "1":
        out["facets"] = facet_counts(ds, q, filters, base)
    out["filters"] = filters
    return out


def facet_counts(ds, q, filters, base=None):
    res = {}
    for f in ds["facets"]:
        w, p = where(ds, q, filters, skip=f["key"], base=base)
        expr, tbl = f["expr"], ds["table"]
        if f["kind"] == "flags":
            sql = f"SELECT flag AS v, count(*) AS n FROM {tbl}, regexp_split_to_table({expr}, '; ') AS flag WHERE {w} AND {expr} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT %s"
        else:
            nulls = "" if f["blank"] else f" AND ({expr}) IS NOT NULL"
            sql = f"SELECT ({expr})::text AS v, count(*) AS n FROM {tbl} WHERE {w}{nulls} GROUP BY 1 ORDER BY 2 DESC LIMIT %s"
        rows = db.query(sql, p + [f["limit"] + 1])
        items = [{"v": r["v"] if r["v"] is not None else BLANK, "n": r["n"]} for r in rows]
        order = f["order"]
        if isinstance(order, list):
            items.sort(key=lambda x: order.index(x["v"]) if x["v"] in order else 99)
        elif order == "value_desc":
            items.sort(key=lambda x: x["v"], reverse=True)
        # selected values must always be visible even when their count under the other filters is 0
        have = {i["v"] for i in items}
        for v in filters.get(f["key"], []):
            if v not in have:
                items.append({"v": v, "n": 0})
        res[f["key"]] = items[: f["limit"] + 5]
    return res


def meta():
    out = {}
    for name, ds in DATASETS.items():
        out[name] = {
            "label": ds["label"], "sort": list(ds["sort"]), "defaults": ds.get("defaults", {}), "detail": ds.get("detail"),
            "columns": [{k: c[k] for k in ("key", "label", "kind", "align", "ref")} for c in ds["columns"]],
            "facets": [{k: f[k] for k in ("key", "label", "kind", "limit", "labels")} for f in ds["facets"]],
        }
    return out


# ---------------------------------------------------------------- detail
def _clean(row, expose_private=False):
    hide = HIDDEN_DETAIL if expose_private else HIDDEN_DETAIL | NEVER_EXPOSE
    return {k: v for k, v in row.items() if k not in hide}


def detail(name, ident, include_archived=False, user=None):
    ds = dataset(name)
    cond = ds["base"] if ds.get("readonly") else f"({ds['base']} OR {archived_base(name, ds)})"
    params = [ident]
    scope = scope_for(name, ds, user)
    if scope:
        cond = f"({cond}) AND ({scope[0]})"
        params += list(scope[1])
    row = db.one(f"SELECT * FROM {ds['table']} WHERE {ds['pk']} = %s AND {cond}", params)
    if not row:
        return None
    archived = row.get("is_current") == 0
    # personal data (mobile, personal e-mail, DOB) goes to the browser only for an administrator, or an engineer looking at their own record
    # (needed so a self-edit form can show the current value, matching the same ownership rule as the edit itself)
    is_admin = bool(user) and user.get("role") == "ADMIN"
    expose_private = is_admin or (name == "engineers" and bool(user) and user.get("engineer_key") == row.get("engineer_key"))
    rel = {}
    if name == "employees":
        rel["assets"] = db.query("SELECT asset_key AS id, asset_class, asset_type, model, serial_no, asset_status FROM asset WHERE cpf_no=%s AND is_current=1 AND record_level='ASSET' ORDER BY asset_key", [row["cpf_no"]])
        rel["calls"] = db.query("SELECT sr_id AS id, cipl_call_date, problem_description, engineer, call_status FROM svc_call WHERE cpf_no=%s AND is_current=1 ORDER BY cipl_call_date DESC", [row["cpf_no"]])
        return {"id": ident, "dataset": name, "row": _clean(row, expose_private), "related": rel, "archived": False, "created_in_portal": False, "overrides": {}}
    if name == "engineers":
        k = row["engineer_key"]
        rel["calls"] = db.query("SELECT sr_id AS id, cipl_call_date, problem_description, priority, call_status FROM svc_call WHERE engineer=%s AND is_current=1 ORDER BY cipl_call_date DESC LIMIT 25", [k])
        rel["assets"] = db.query("SELECT asset_key AS id, asset_class, asset_type, model, asset_status FROM asset WHERE engineer_name=%s AND is_current=1 AND record_level='ASSET' ORDER BY asset_key LIMIT 50", [k])
        # falls through to the generic tail below (manual-edit overrides + audit) - edits write to cipl_employee keyed by ecode, but locks/audit
        # are recorded under this record's own identity (engineer_key), same as ident
    elif name == "assets":
        rel["components"] = db.query("SELECT asset_key AS id, asset_type, model, serial_no, asset_status FROM asset WHERE parent_asset_key=%s AND is_current=1 ORDER BY asset_key", [ident])
        rel["calls"] = db.query("SELECT sr_id AS id, cipl_call_date, problem_description, engineer, call_status, priority FROM svc_call WHERE asset_key=%s AND is_current=1 ORDER BY cipl_call_date DESC", [ident])
        rel["inward"] = db.query("SELECT inward_id AS id, inward_date, sr_id, part_description, received_date FROM spare_inward WHERE asset_key=%s AND is_current=1 ORDER BY inward_date DESC", [ident])
        rel["outward"] = db.query("SELECT outward_id AS id, outward_date, sr_id, part_description, gatepass_no, sent_date FROM spare_outward WHERE asset_key=%s AND is_current=1 ORDER BY outward_date DESC", [ident])
        rel["rma"] = db.query("SELECT rma_line_id AS id, rma_no, fault_item, call_log_date, return_status FROM oem_rma WHERE asset_key=%s AND is_current=1 ORDER BY call_log_date DESC", [ident])
        rel["serial_history"] = db.query("SELECT old_serial, new_serial, rma_no, change_date FROM oem_serial_history WHERE asset_key=%s ORDER BY change_date", [ident])
        rel["history"] = db.query("SELECT snapshot_date, change_type, field, old_value, new_value FROM asset_change_log WHERE asset_key=%s ORDER BY snapshot_date DESC LIMIT 50", [ident])
        rel["verifications"] = db.query("SELECT verified_on, result, verified_by, note FROM asset_verification WHERE asset_key=%s ORDER BY verified_on DESC, verification_id DESC LIMIT 20", [ident])
        rel["imac"] = imac.for_asset(ident)
    elif name == "calls":
        rel["asset"] = db.one("SELECT asset_key AS id, asset_class, asset_type, make, model, serial_no, location_code, room, user_name, engineer_name FROM asset WHERE asset_key=%s AND is_current=1", [row["asset_key"]]) if row["asset_key"] else None
        rel["inward"] = db.query("SELECT inward_id AS id, inward_date, part_description, part_no, received_date FROM spare_inward WHERE sr_id=%s AND is_current=1 ORDER BY inward_date", [ident])
        rel["outward"] = db.query("SELECT outward_id AS id, outward_date, part_description, gatepass_no, sent_date FROM spare_outward WHERE sr_id=%s AND is_current=1 ORDER BY outward_date", [ident])
        rel["rma"] = db.query("SELECT rma_line_id AS id, rma_no, fault_item, return_status FROM oem_rma WHERE call_sr_id=%s AND is_current=1", [ident])
        rel["history"] = db.query("SELECT snapshot_date, change_type, field, old_value, new_value FROM cm_change_log WHERE table_name='CALLS' AND record_key=%s ORDER BY snapshot_date DESC LIMIT 50", [ident])
    elif name in ("inward", "outward", "rma"):
        rel["asset"] = db.one("SELECT asset_key AS id, asset_class, asset_type, make, model, serial_no, location_code, user_name, engineer_name FROM asset WHERE asset_key=%s AND is_current=1", [row["asset_key"]]) if row.get("asset_key") else None
        sr = row.get("sr_id") or row.get("call_sr_id")
        rel["call"] = db.one("SELECT sr_id AS id, cipl_call_date, problem_description, engineer, call_status FROM svc_call WHERE sr_id=%s AND is_current=1", [sr]) if sr else None
        if name == "rma" and row.get("asset_key"):
            rel["serial_history"] = db.query("SELECT old_serial, new_serial, rma_no, change_date FROM oem_serial_history WHERE asset_key=%s ORDER BY change_date", [row["asset_key"]])
    locks = db.query("SELECT field, value, editor, edited_at, source_value, source_seen_at FROM portal_lock WHERE dataset=%s AND record_key=%s ORDER BY field", [name, ident])
    overrides = {r["field"]: {"editor": r["editor"], "at": r["edited_at"].isoformat(), "source": r["source_value"], "source_seen": r["source_seen_at"].isoformat() if r["source_seen_at"] else None}
                 for r in locks if not r["field"].startswith("__")}
    created = any(r["field"] == "__created__" for r in locks)
    rel["audit"] = db.query("SELECT at, editor, action, changes, reason FROM portal_audit WHERE dataset=%s AND record_key=%s ORDER BY audit_id DESC LIMIT 30", [name, ident])
    return {"id": ident, "dataset": name, "row": _clean(row, expose_private), "related": rel, "archived": archived, "created_in_portal": created, "overrides": overrides}


# ---------------------------------------------------------------- global search
def search_all(q, per=5, user=None):
    q = (q or "").strip()[:80]
    if len(q) < 2:
        return {"q": q, "groups": []}
    toks = [t.translate(_LIKE_ESC) for t in re.findall(r"\S+", q.lower())[:5]]
    groups = []
    specs = [
        ("assets", "Assets", "asset", "asset_key", "asset_key", "trim(coalesce(asset_class,'') || ' · ' || coalesce(make,'') || ' ' || coalesce(model,'') || ' · ' || coalesce(user_name,''))", "is_current = 1 AND record_level = 'ASSET'"),
        ("calls", "Calls", "svc_call", "sr_id", "sr_id", "coalesce(problem_description,'') || ' · ' || coalesce(asset_key,'')", "is_current = 1"),
        ("employees", "Employees", "employee", "cpf_no::text", "cpf_no::text", "coalesce(employee_name,'') || ' · ' || coalesce(designation,'')", "record_status = 'ACTIVE'"),
        ("rma", "OEM RMA", "oem_rma", "rma_line_id", "coalesce(rma_no, rma_line_id)", "coalesce(vendor,'') || ' ' || coalesce(device_model,'') || ' · ' || coalesce(fault_item,'')", "is_current = 1"),
        ("inward", "Inward", "spare_inward", "inward_id", "inward_id", "coalesce(part_description,'') || ' · ' || coalesce(sr_id,'')", "is_current = 1"),
        ("outward", "Outward", "spare_outward", "outward_id", "outward_id", "coalesce(part_description,'') || ' · ' || coalesce(sr_id,'')", "is_current = 1"),
    ]
    admin = bool(user) and user.get("role") == "ADMIN"
    has_parts_access = bool(user) and user.get("call_parts_access") in ("READ", "FULL")
    for name, label, table, pk, title, sub, base in specs:
        if name in ADMIN_ONLY_DATASETS and not admin and not has_parts_access:
            continue
        ds = DATASETS[name]
        hay = search_expr(ds["search"])
        scope = scope_for(name, ds, user)
        cond = " AND ".join([base] + ([scope[0]] if scope else []) + [f"{hay} LIKE %s"] * len(toks))
        params = (list(scope[1]) if scope else []) + ["%" + t + "%" for t in toks]
        rows = db.query(f"SELECT {pk} AS id, {title} AS title, {sub} AS sub, count(*) OVER () AS total FROM {table} WHERE {cond} ORDER BY {pk} LIMIT %s", params + [per])
        if rows:
            groups.append({"dataset": name, "label": label, "total": rows[0]["total"], "items": [{k: r[k] for k in ("id", "title", "sub")} for r in rows]})
    eng = db.query("SELECT engineer_key AS id, display_name AS title, coalesce(ecode,'') AS sub FROM portal_engineer WHERE lower(display_name) LIKE ALL(%s) ORDER BY display_name LIMIT %s",
                   [["%" + t + "%" for t in toks], per])
    if eng:
        groups.append({"dataset": "engineers", "label": "Engineers", "total": len(eng), "items": eng})
    return {"q": q, "groups": groups}


# ---------------------------------------------------------------- audit register
def audit_list(args):
    limit = max(1, min(int(args.get("limit", 100)), config.MAX_PAGE))
    offset = max(0, int(args.get("offset", 0)))
    parts, params = ["true"], []
    for k, col in (("dataset", "dataset"), ("action", "action"), ("editor", "editor")):
        if args.get(k):
            parts.append(f"{col} = ANY(%s)")
            params.append([x for x in args[k].split("|") if x])
    for tok in re.findall(r"\S+", (args.get("q") or "").lower())[:6]:
        parts.append("lower(editor || ' ' || record_key || ' ' || coalesce(reason,'') || ' ' || coalesce(changes::text,'')) LIKE %s")
        params.append("%" + tok.translate(_LIKE_ESC) + "%")
    w = " AND ".join(parts)
    rows = db.query(f"SELECT audit_id AS id, at, editor, client_ip, dataset, record_key, action, changes, reason FROM portal_audit WHERE {w} ORDER BY audit_id DESC LIMIT %s OFFSET %s", params + [limit, offset])
    out = {"rows": rows, "total": db.one(f"SELECT count(*) AS n FROM portal_audit WHERE {w}", params)["n"]}
    if args.get("facets") == "1":
        out["facets"] = {c: db.query(f"SELECT {c} AS v, count(*) AS n FROM portal_audit GROUP BY 1 ORDER BY 2 DESC LIMIT 20") for c in ("dataset", "action", "editor")}
    return out


EXPORT_MAX_ROWS = 50000


def export_table(name, args, user):
    """Every row matching the register's current search/filters (not just one page), as {"columns": [(key, label)], "rows": [...]} for
    export.csv_bytes. Same access and per-user scoping as the on-screen list."""
    ds = dataset(name)
    a = {**args, "limit": str(EXPORT_MAX_ROWS), "offset": "0"}
    a.pop("facets", None)
    out = list_rows(name, a, user=user, cap=EXPORT_MAX_ROWS)
    return {"columns": [(c["key"], c["label"]) for c in ds["columns"]], "rows": out["rows"], "total": out.get("total", len(out["rows"]))}
