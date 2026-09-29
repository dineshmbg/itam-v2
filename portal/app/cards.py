"""Hover cards: every available detail of an asset, a call, an HR-master employee (CPF) or a CIPL engineer (ECODE).

Personal data (mobile number, personal e-mail, date of birth) is included for administrators only.
"""
from starlette.concurrency import run_in_threadpool
from starlette.routing import Route

from . import db
from .datasets import HIDDEN_DETAIL, NEVER_EXPOSE
from .export import asset_barcode_svg
from .web import json_response, read

PERSONAL = NEVER_EXPOSE          # same set queries.py's _clean() protects - kept as one alias so cards.py's own history/comments still read naturally
INTERNAL = HIDDEN_DETAIL | {"is_on_roster", "first_seen_date", "last_seen_date", "snapshot_date", "source_row"}


def _clean(row, admin):
    return {k: v for k, v in row.items() if v is not None and k not in INTERNAL and (admin or k not in PERSONAL)}


def _asset(ident, admin):
    row = db.one("SELECT * FROM asset WHERE asset_key = %s", [ident])
    if not row:
        return None
    x = db.one("""SELECT (SELECT count(*) FROM svc_call WHERE asset_key = %(k)s AND is_current = 1) AS calls,
                         (SELECT count(*) FROM svc_call WHERE asset_key = %(k)s AND is_current = 1 AND call_status = 'OPEN') AS open_calls,
                         (SELECT count(*) FROM oem_rma WHERE asset_key = %(k)s AND is_current = 1) AS rma,
                         (SELECT count(*) FROM asset WHERE parent_asset_key = %(k)s AND is_current = 1) AS components""", {"k": ident})
    return {"kind": "asset", "id": ident, "title": ident, "subtitle": " · ".join(filter(None, [row["asset_class"], row["make"], row["model"]])),
            "archived": row["is_current"] == 0, "row": _clean(row, admin), "extra": x, "link": {"path": "registers/assets", "open": ident},
            "barcode_svg": asset_barcode_svg(ident)}


def _call(ident, admin):
    row = db.one("SELECT * FROM svc_call WHERE sr_id = %s", [ident])
    if not row:
        return None
    a = db.one("SELECT asset_key, asset_class, asset_type, make, model, serial_no, location_code, room, user_name, engineer_name, asset_status FROM asset WHERE asset_key = %s", [row["asset_key"]]) if row["asset_key"] else None
    return {"kind": "call", "id": ident, "title": ident, "subtitle": (row["problem_description"] or "")[:90], "archived": row["is_current"] == 0,
            "row": _clean(row, admin), "extra": {"asset": a}, "link": {"path": "registers/calls", "open": ident}}


def _employee(ident, admin):
    try:
        cpf = int(ident)
    except ValueError:
        return None
    row = db.one("SELECT * FROM employee WHERE cpf_no = %s", [cpf])
    if not row:
        return None
    x = db.one("""SELECT (SELECT count(*) FROM asset WHERE cpf_no = %(c)s AND is_current = 1 AND record_level = 'ASSET') AS assets,
                         (SELECT count(*) FROM svc_call WHERE cpf_no = %(c)s AND is_current = 1) AS calls""", {"c": cpf})
    return {"kind": "cpf", "id": str(cpf), "title": row["employee_name"], "subtitle": f"CPF {cpf} · {row['designation'] or ''}", "row": _clean(row, admin), "extra": x,
            "link": {"path": "registers/assets", "q": str(cpf)}}


def _engineer(ident, admin, viewer_key=None):
    e = db.one("""SELECT e.engineer_key, e.display_name, e.ecode, e.source FROM portal_engineer e WHERE e.engineer_key = %s OR e.ecode = %s""", [ident.upper(), ident.upper()])
    if not e:
        return None
    show_personal = admin or e["engineer_key"] == viewer_key    # a non-admin sees full personal data only on their own card
    r = db.one("SELECT * FROM cipl_employee WHERE ecode = %s", [e["ecode"]]) if e["ecode"] else None
    x = db.one("""SELECT (SELECT count(*) FROM asset WHERE engineer_name = %(k)s AND is_current = 1 AND record_level = 'ASSET') AS assets,
                         (SELECT count(*) FROM svc_call WHERE engineer = %(k)s AND is_current = 1 AND call_status = 'OPEN') AS open_calls,
                         (SELECT count(*) FROM svc_call WHERE engineer = %(k)s AND is_current = 1) AS calls""", {"k": e["engineer_key"]})
    chk = db.query("SELECT item, status, item_value FROM cipl_onboarding WHERE ecode = %s ORDER BY item", [e["ecode"]]) if e["ecode"] else []
    return {"kind": "engineer", "id": e["engineer_key"], "title": e["display_name"], "subtitle": f"ECODE {e['ecode']}" if e["ecode"] else "NOT ON THE CIPL ROSTER",
            "row": _clean(r, show_personal) if r else {}, "extra": {**x, "checklist": chk, "source": e["source"]},
            "link": {"path": "dashboard/engineers", "eng": e["engineer_key"]}}


KINDS = {"asset": _asset, "call": _call, "cpf": _employee, "ecode": _engineer, "engineer": _engineer}


async def card(request):
    kind, ident = request.path_params["kind"], request.path_params["ident"]
    fn = KINDS.get(kind)
    if not fn:
        return json_response({"error": "unknown card"}, 400)
    user = request.state.user
    admin = user["role"] == "ADMIN"
    if kind in ("ecode", "engineer"):
        data = await run_in_threadpool(_engineer, ident, admin, user.get("engineer_key"))
    else:
        data = await run_in_threadpool(fn, ident, admin)
    if data is None:
        return json_response({"error": "not found"}, 404)
    if kind in ("ecode", "engineer") and not admin and data["id"] != user.get("engineer_key"):
        return json_response({"error": "not found"}, 404)   # a non-admin only sees their own hover card, never another engineer's
    return json_response(data)


routes = [Route("/api/card/{kind}/{ident:path}", read(card))]
