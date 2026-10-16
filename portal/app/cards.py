"""Hover cards: every available detail of an asset or a call, and a SMALL card for an HR-master employee (CPF) or a CIPL engineer (ECODE).

Asset and call cards hide personal columns from non-administrators (PERSONAL below). The employee and engineer cards are deliberately
small and the same for everyone signed in (decided 2026-10-16): name, designation, mobile number and, for an employee, the department
recorded on their assets; for an engineer, the gate pass number. Nothing else about a person - no date of birth, personal e-mail, bank
or statutory numbers, no e-mail address at all - is ever put on these two cards.
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
    row = db.one("SELECT cpf_no, employee_name, designation, mobile_no FROM employee WHERE cpf_no = %s", [cpf])
    if not row:
        return None
    # The HR record has no readable department (only a code-like org unit), so use the one written on the person's assets: the most common value.
    dept = db.one("""SELECT user_department FROM asset WHERE cpf_no = %s AND is_current = 1 AND record_level = 'ASSET' AND user_department IS NOT NULL
                     GROUP BY user_department ORDER BY count(*) DESC, user_department LIMIT 1""", [cpf])
    return {"kind": "cpf", "id": str(cpf), "title": row["employee_name"],
            "small": {"id": str(cpf), "name": row["employee_name"], "designation": row["designation"], "department": dept["user_department"] if dept else None,
                      "mobile": row["mobile_no"]}}


def _engineer(ident, admin=False):
    e = db.one("SELECT engineer_key, display_name, ecode, source FROM portal_engineer WHERE engineer_key = %s OR ecode = %s", [ident.upper(), ident.upper()])
    if not e:
        return None
    r = db.one("SELECT designation, mobile_no, ongc_gatepass_no FROM cipl_employee WHERE ecode = %s", [e["ecode"]]) if e["ecode"] else None
    small = {"id": e["ecode"], "name": e["display_name"], "designation": r and r["designation"], "mobile": r and r["mobile_no"], "gate_pass": r and r["ongc_gatepass_no"]}
    if not r:
        small["note"] = "Not on the CIPL roster, no contact details held."
    return {"kind": "engineer", "id": e["engineer_key"], "title": e["display_name"], "small": small}


KINDS = {"asset": _asset, "call": _call, "cpf": _employee, "ecode": _engineer, "engineer": _engineer}


async def card(request):
    kind, ident = request.path_params["kind"], request.path_params["ident"]
    fn = KINDS.get(kind)
    if not fn:
        return json_response({"error": "unknown card"}, 400)
    admin = request.state.user["role"] == "ADMIN"
    data = await run_in_threadpool(fn, ident, admin)
    if data is None:
        return json_response({"error": "not found"}, 404)
    return json_response(data)


routes = [Route("/api/card/{kind}/{ident:path}", read(card))]
