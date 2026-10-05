"""Edit routes: permission checks by group, then app/edit.py (validation, audit, overrides)."""
import logging
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.routing import Route

from . import auth, db, edit, mailer, queries, views_pref
from .datasets import ADMIN_ONLY_DATASETS
from .web import client_ip, json_response, read, write

log = logging.getLogger("itam")

NEW_ASSET_NOTIFY_COLUMNS = [
    ("asset_key", "Asset (CI)"), ("asset_class", "Class"), ("asset_type", "Type"), ("make", "Make"), ("model", "Model"),
    ("location_code", "Location"), ("room", "Room"), ("asset_status", "Status"),
]


def _notify_engineer_of_new_asset(detail, editor):
    """Best-effort: an asset just created with an engineer assigned gets a notification e-mail to that engineer. Never lets a mail
    problem fail the asset creation itself - the asset is already saved by the time this runs."""
    row = (detail or {}).get("row") or {}
    eng_key = row.get("engineer_name")
    if not eng_key:
        return
    try:
        if not mailer.get_smtp()["enabled"]:
            return
        to = mailer.engineer_email(eng_key)
        if not to:
            return
        html_body, text = mailer.page(f"New asset assigned to you: {detail['id']}",
                                       f"{editor['display_name']} added this asset in the ITAM Portal and assigned it to you.",
                                       rows=[row], columns=NEW_ASSET_NOTIFY_COLUMNS)
        mailer.send(to, f"New asset assigned - {detail['id']}", text, html_body)
    except Exception:  # noqa: BLE001 - a notification failure must never surface as a create failure
        log.exception("could not e-mail the engineer about new asset %s", detail.get("id"))


def _key(b):
    k = b.get("key")
    if not isinstance(k, str) or not k:
        raise edit.Invalid("Missing record key.")
    return k


def _ctx(request):
    return request.path_params["name"], request.state.body, request.state.user, client_ip(request)


def _act(user, ip, action, target, detail=None):
    auth.log(user["username"], ip, action, target, detail)


async def update(request):
    name, b, u, ip = _ctx(request)
    auth.check_edit(u, name, (b.get("changes") or {}).keys(), _key(b))
    def run():
        r = edit.update(name, _key(b), b.get("changes"), b.get("expected"), u["username"], ip, b.get("reason"))
        _act(u, ip, "EDIT", f"{name}:{_key(b)}", {"fields": r["changed"]})
        return {**r, "detail": queries.detail(name, _key(b), include_archived=True, user=u)}
    return json_response(await run_in_threadpool(run))


async def reassign(request):
    """Bulk (re)assignment of assets to an engineer. Administrators and Users with full asset access only."""
    b, u, ip = request.state.body, request.state.user, client_ip(request)
    if not (auth.is_admin(u) or u.get("asset_access") == "FULL"):
        raise auth.AuthError("Only administrators can assign assets to engineers in bulk.", 403, code="forbidden")
    def run():
        r = edit.reassign_assets(b.get("asset_keys"), b.get("engineer"), b.get("from_engineer"), u["username"], ip, b.get("reason"))
        _act(u, ip, "REASSIGN_ASSETS", f"assets:{r['changed']}", {"to": b.get("engineer"), "from": b.get("from_engineer"), "skipped": r["skipped"]})
        return r
    return json_response(await run_in_threadpool(run))


async def create(request):
    name, b, u, ip = _ctx(request)
    auth.check_create(u, name)
    def run():
        if name == "engineers":
            r = edit.create_engineer(b.get("values"), u["username"], ip, b.get("reason"))
        else:
            r = edit.create(name, b.get("values"), u["username"], ip, b.get("reason"))
        _act(u, ip, "CREATE", f"{name}:{r['id']}")
        detail = queries.detail(name, r["id"], include_archived=True, user=u)
        if name == "assets":
            _notify_engineer_of_new_asset(detail, u)
        return {**r, "detail": detail}
    return json_response(await run_in_threadpool(run))


async def archive(request):
    name, b, u, ip = _ctx(request)
    auth.check_archive(u, name)
    def run():
        r = edit.archive(name, _key(b), u["username"], ip, b.get("reason"))
        _act(u, ip, "ARCHIVE", f"{name}:{_key(b)}", {"reason": b.get("reason")})
        return r
    return json_response(await run_in_threadpool(run))


async def restore(request):
    name, b, u, ip = _ctx(request)
    auth.check_archive(u, name)
    def run():
        r = edit.restore(name, _key(b), u["username"], ip, b.get("reason"))
        _act(u, ip, "RESTORE", f"{name}:{_key(b)}")
        return {**r, "detail": queries.detail(name, _key(b), include_archived=True, user=u)}
    return json_response(await run_in_threadpool(run))


async def reset(request):
    name, b, u, ip = _ctx(request)
    auth.check_edit(u, name, b.get("fields") or [], _key(b))
    def run():
        r = edit.reset(name, _key(b), b.get("fields"), u["username"], ip)
        _act(u, ip, "RESET_OVERRIDE", f"{name}:{_key(b)}", {"fields": r["reset"]})
        return {**r, "detail": queries.detail(name, _key(b), include_archived=True, user=u)}
    return json_response(await run_in_threadpool(run))


async def verify(request):
    """Record a physical check of an asset. An administrator may verify any asset; an engineer only assets assigned to them."""
    b, u, ip = request.state.body, request.state.user, client_ip(request)
    key = _key(b)
    auth.check_verify(u, key)
    r = await run_in_threadpool(edit.verify_asset, key, b.get("result"), b.get("note"), u["username"], ip)
    await run_in_threadpool(_act, u, ip, "VERIFY", f"assets:{key}", {"result": r["result"]})
    return json_response({**r, "detail": await run_in_threadpool(queries.detail, "assets", key, False, u)})


async def qr_label(request):
    """QR code for an asset label: it opens the asset's record in this portal, on the address the request came in on."""
    import io

    import segno
    key, u = request.path_params["key"], request.state.user
    d = await run_in_threadpool(queries.detail, "assets", key, False, u)
    if not d:
        return json_response({"error": "not found"}, 404)
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    url = f"{scheme}://{host}/#/registers/assets?open={quote(key, safe='')}"
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=6, border=2, xmldecl=False, svgns=True, nl=False)
    row = d["row"]
    return json_response({"key": key, "url": url, "svg": buf.getvalue().decode(),
                          "lines": [row.get("asset_class"), row.get("asset_type"), " ".join(filter(None, [row.get("make"), row.get("model")])), row.get("serial_no")]})


async def engineer_event(request):
    b, u, ip = request.state.body, request.state.user, client_ip(request)
    key = request.path_params["key"]
    r = await run_in_threadpool(edit.engineer_event, key, b.get("type"), b.get("date"), b.get("reason"), u["username"], ip)
    await run_in_threadpool(_act, u, ip, "ENGINEER_EVENT", key, {"type": b.get("type")})
    return json_response(r)


async def schema(request):
    def build():
        s = edit.schema()
        u = request.state.user
        if u["role"] != "ADMIN":
            if u.get("asset_access") == "FULL":
                locked = set()   # every field editable, same as check_edit's asset_access=FULL bypass
            else:
                locked = auth.ASSET_LOCKED_FIELDS
            for f in s["assets"]["fields"]:
                f["readonly"] = f["key"] in locked
            # engineers: every field is editable for a non-admin - but only on their own record, enforced by auth.check_edit's
            # ownership check (and by record.js only showing the Edit button on the signed-in engineer's own record) rather than
            # by field, since the person is allowed to change all of their own details, not just a fixed contact-details subset
        full_access = u["role"] == "ADMIN" or u.get("call_parts_access") == "FULL"
        return {"datasets": s, "role": u["role"], "read_only": bool(u.get("read_only")), "engineer_key": u.get("engineer_key"),
                "can_create": {k: (u["role"] == "ADMIN" if k in ("engineers", "assets") else (full_access if k in ADMIN_ONLY_DATASETS else True)) for k in s},
                "engineers": [r["engineer_key"] for r in db.query("SELECT engineer_key FROM portal_engineer ORDER BY engineer_key")]}
    return json_response(await run_in_threadpool(build))


async def audit_list(request):
    return json_response(await run_in_threadpool(queries.audit_list, dict(request.query_params)))


# ---------------------------------------------------------------- per-user saved column layout
async def view_get(request):
    name, u = request.path_params["name"], request.state.user
    queries.dataset(name)     # 400 if not a real register
    return json_response({"columns": await run_in_threadpool(views_pref.get, u["user_id"], name)})


async def view_save(request):
    name, b, u, _ip = _ctx(request)
    queries.dataset(name)
    try:
        cols = await run_in_threadpool(views_pref.save, u["user_id"], name, b.get("columns") or [])
    except ValueError as e:
        return json_response({"error": str(e)}, 400)
    return json_response({"columns": cols})


async def view_reset(request):
    name, u = request.path_params["name"], request.state.user
    queries.dataset(name)
    await run_in_threadpool(views_pref.reset, u["user_id"], name)
    return json_response({"ok": True})


# field -> (table, column, column to filter by, or None for an unfiltered list) - used by the new-asset form's cascading Class->Type,
# Make->Model and OS family->OS suggestions, and the call/inward Part no. suggestions (from that asset's own part history).
# A fixed whitelist: table and column names are never taken from the request.
LOOKUP_FIELDS = {
    "asset_type": ("asset", "asset_type", "asset_class"), "make": ("asset", "make", None), "model": ("asset", "model", "make"),
    "asset_class": ("asset", "asset_class", "asset_type"),   # reverse of "asset_type" above - used to suggest Class once a Type is typed
    "part_no": ("spare_inward", "part_no", "asset_key"),
}


async def lookup(request):
    field = request.query_params.get("field", "")
    filter_value = (request.query_params.get("filter") or "").strip().upper() or None
    spec = LOOKUP_FIELDS.get(field)
    if not spec:
        return json_response({"error": f"unknown lookup field '{field}'"}, 400)
    table, col, filter_col = spec

    def run():
        if filter_col and filter_value:
            rows = db.query(f"SELECT DISTINCT {col} AS v FROM {table} WHERE is_current = 1 AND {col} IS NOT NULL AND {filter_col} = %s ORDER BY 1 LIMIT 200", [filter_value])
        else:
            rows = db.query(f"SELECT DISTINCT {col} AS v FROM {table} WHERE is_current = 1 AND {col} IS NOT NULL ORDER BY 1 LIMIT 200")
        return [r["v"] for r in rows]
    return json_response({"values": await run_in_threadpool(run)})


async def rates(request):
    """Rate component + value pairs already used on assets of a Type (or carrying a component), most common first - the new-asset form
    fills the rate from these. A component's value is its most common value."""
    t = (request.query_params.get("type") or "").strip().upper() or None
    c = (request.query_params.get("component") or "").strip().upper() or None

    def run():
        where, params = ["is_current = 1", "rate_component IS NOT NULL"], []
        for col, v in (("asset_type", t), ("rate_component", c)):
            if v:
                where.append(f"{col} = %s")
                params.append(v)
        rows = db.query(f"SELECT rate_component AS component, rate_value AS value, count(*) AS n FROM asset WHERE {' AND '.join(where)} "
                        "GROUP BY 1, 2 ORDER BY 3 DESC, 1 LIMIT 200", params)
        out, seen = [], set()
        for r in rows:
            if r["component"] not in seen:
                seen.add(r["component"])
                out.append({"component": r["component"], "value": float(r["value"]) if r["value"] is not None else None})
        return out
    return json_response({"rates": await run_in_threadpool(run)})


routes = [
    Route("/api/edit/schema", read(schema)),
    Route("/api/edit/assets/rates", read(rates)),
    Route("/api/edit/assets/reassign", write(reassign), methods=["POST"]),
    Route("/api/edit/assets/verify", write(verify), methods=["POST"]),
    Route("/api/assets/{key:path}/qr", read(qr_label)),
    Route("/api/edit/assets/lookup", read(lookup)),
    Route("/api/edit/{name}/update", write(update), methods=["POST"]),
    Route("/api/edit/{name}/create", write(create), methods=["POST"]),
    Route("/api/edit/{name}/archive", write(archive), methods=["POST"]),
    Route("/api/edit/{name}/restore", write(restore), methods=["POST"]),
    Route("/api/edit/{name}/reset", write(reset), methods=["POST"]),
    Route("/api/engineers/{key}/event", write(engineer_event, admin=True), methods=["POST"]),
    Route("/api/audit", read(audit_list, admin="strict")),
    Route("/api/views/{name}", read(view_get)),
    Route("/api/views/{name}/save", write(view_save), methods=["POST"]),
    Route("/api/views/{name}/reset", write(view_reset), methods=["POST"]),
]
