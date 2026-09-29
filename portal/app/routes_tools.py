"""Routes for the working tools: preventive maintenance, reports and exports, data import, backup and restore, e-mail and sharing."""
import datetime as dt
import re
from urllib.parse import unquote

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.routing import Route

from . import auth, backup, db, export, importer, mailer, packs, pm, reports, scheduler
from .web import client_ip, current_user, error, guarded, json_response, read, write

EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


_tool_errors = guarded


def R_(fn, **kw):
    return read(fn, **kw)


def W_(fn, **kw):
    return write(fn, **kw)


def _file(data, mime, name):
    return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


def _log(request, action, target=None, detail=None):
    u = request.state.user
    return run_in_threadpool(auth.log, u["username"], client_ip(request), action, target, detail)


# ---------------------------------------------------------------- reports
async def report_meta(request):
    admin = request.state.user["role"] == "ADMIN"
    return json_response({"datasets": {k: reports.describe(k, admin) for k in reports.SETS}, "saved": await run_in_threadpool(reports.list_saved, request.state.user)})


async def report_run(request):
    b = request.state.body
    admin = request.state.user["role"] == "ADMIN"
    return json_response(await run_in_threadpool(reports.run, b.get("definition") or {}, admin, int(b.get("limit") or 200)))


async def report_values(request):
    b = request.state.body
    return json_response({"values": await run_in_threadpool(reports.values, b.get("dataset"), b.get("field"), request.state.user["role"] == "ADMIN", str(b.get("q") or "")[:40])})


async def report_export(request):
    b = request.state.body
    fmt = b.get("format", "xlsx")
    defn = b.get("definition") or {}
    admin = request.state.user["role"] == "ADMIN"
    title = f"{reports.SETS[defn.get('dataset', 'assets')][2]} report" if defn.get("dataset") in reports.SETS else "Report"

    def run():
        table, total = reports.table_for_export(defn, admin, title)
        data, mime, ext = export.render(fmt, title.upper(), f"{total:,} rows - {dt.date.today():%d %b %Y} - {request.state.user['display_name']}", [table],
                                        footer=request.state.user["username"])
        return data, mime, f"{reports.filename(defn)}.{ext}", total
    data, mime, name, total = await run_in_threadpool(run)
    await _log(request, "EXPORT_REPORT", name, {"format": fmt, "rows": total, "dataset": defn.get("dataset")})
    return _file(data, mime, name)


async def report_save(request):
    b = request.state.body
    out = await run_in_threadpool(reports.save, request.state.user, b.get("name"), b.get("definition") or {}, b.get("shared"))
    await _log(request, "REPORT_SAVED", out["name"])
    return json_response({**out, "saved": await run_in_threadpool(reports.list_saved, request.state.user)})


async def report_delete(request):
    await run_in_threadpool(reports.delete, request.state.user, int(request.state.body.get("id")))
    return json_response({"saved": await run_in_threadpool(reports.list_saved, request.state.user)})


# ---------------------------------------------------------------- dashboard packs: download and share
def _pack(name):
    fn = packs.PACKS.get(name)
    if not fn:
        raise reports.ReportError("Unknown dashboard.")
    return fn()


def _render_pack(p, fmt, user):
    return export.render(fmt, p["title"], p["subtitle"], p["tables"], p["kpis"], p["charts"], footer=user["username"])


async def dash_export(request):
    b = request.state.body
    fmt = b.get("format", "pdf")
    if fmt not in ("pdf", "xlsx"):
        raise reports.ReportError("Dashboards download as PDF or Excel.")
    p = await run_in_threadpool(_pack, b.get("pack"))
    data, mime, ext = await run_in_threadpool(_render_pack, p, fmt, request.state.user)
    name = export.safe_name(f"{b['pack']}_dashboard_{dt.date.today():%Y%m%d}") + "." + ext
    await _log(request, "EXPORT_DASHBOARD", name, {"format": fmt})
    return _file(data, mime, name)


async def dash_share(request):
    b, u = request.state.body, request.state.user
    to = [a.strip().lower() for a in (b.get("to") or []) if str(a).strip()][:20]
    bad = [a for a in to if not EMAIL.fullmatch(a)]
    if not to or bad:
        raise mailer.MailError("Enter at least one valid e-mail address." if not bad else f"'{bad[0]}' is not a valid e-mail address.")
    if not (await run_in_threadpool(mailer.get_smtp))["enabled"]:
        raise mailer.MailError("E-mail is not switched on. Ask an administrator to set it up (Administration > E-mail).")
    recent = await run_in_threadpool(db.one, "SELECT count(*) AS n FROM portal_activity WHERE username = %s AND action = 'SHARE_EMAIL' AND at > now() - interval '1 hour'", [u["username"]])
    if recent["n"] >= 10:
        raise mailer.MailError("You have shared 10 dashboards in the last hour. Please wait before sending more.")
    formats = [f for f in (b.get("formats") or ["pdf"]) if f in ("pdf", "xlsx")] or ["pdf"]

    def run():
        p = _pack(b.get("pack"))
        att = []
        for f in formats:
            data, mime, ext = _render_pack(p, f, u)
            att.append((export.safe_name(f"{b['pack']}_dashboard_{dt.date.today():%Y%m%d}") + "." + ext, data, mime))
        note = str(b.get("message") or "").strip()[:1000]
        intro = f"{u['display_name']} shared the {p['title'].lower()} with you." + (f"\n\n{note}" if note else "")
        body, text = mailer.page(p["title"], intro, [dict(zip(("k", "v"), kv)) for kv in p["kpis"]], [("k", "Figure"), ("v", "Value")], footer=f"{p['subtitle']} - sent from the ITAM Portal.")
        mailer.send(to, f"{p['title']} - {dt.date.today():%d %b %Y}", text, body, att)
    await run_in_threadpool(run)
    await _log(request, "SHARE_EMAIL", b.get("pack"), {"to": to, "formats": formats})
    return json_response({"sent_to": to})


# ---------------------------------------------------------------- preventive maintenance
async def pm_dashboard(request):
    u = request.state.user
    eng = None if u["role"] == "ADMIN" else u.get("engineer_key")
    return json_response(await run_in_threadpool(pm.dashboard, eng))


async def pm_cycles(request):
    d = await run_in_threadpool(pm.cycles)
    d["rollover"] = await run_in_threadpool(pm.rollover_preview)
    return json_response(d)


async def pm_record(request):
    b, u = request.state.body, request.state.user
    out = await run_in_threadpool(pm.record, b.get("asset_keys"), b.get("pm_date"), b.get("done_by"), b.get("signed_by"), b.get("remarks"), u, client_ip(request))
    await _log(request, "PM_RECORDED", out["quarter"], {"assets": out["recorded"]})
    return json_response(out)


async def pm_snapshot(request):
    out = await run_in_threadpool(pm.snapshot_now, request.state.user, client_ip(request))
    await _log(request, "PM_SNAPSHOT", out["quarter"], out)
    return json_response(out)


async def pm_rollover(request):
    b, u = request.state.body, request.state.user
    if b.get("confirm") != "ROLL OVER":
        raise pm.PmError('Type ROLL OVER to confirm.')
    await run_in_threadpool(backup.run_backup, "MANUAL", u["username"], "safety copy before PM roll-over")
    out = await run_in_threadpool(pm.rollover, u, client_ip(request), bool(b.get("early")))
    await _log(request, "PM_ROLLOVER", out["opened"], out)
    return json_response(out)


# ---------------------------------------------------------------- backup and restore
async def backup_list(request):
    return json_response({"backups": await run_in_threadpool(backup.list_backups), "auto": await run_in_threadpool(scheduler.backup_settings), "folder": str(backup.BACKUP_DIR)})


async def backup_now(request):
    u = request.state.user
    out = await run_in_threadpool(backup.run_backup, "MANUAL", u["username"], str(request.state.body.get("note") or "")[:200] or None)
    await _log(request, "BACKUP_MANUAL", out["file"], {"size": out["size_bytes"]})
    return json_response(out)


async def backup_verify(request):
    file = str(request.state.body.get("file") or "")
    return json_response(await run_in_threadpool(backup.verify, file))


async def backup_settings(request):
    u = request.state.user
    out = await run_in_threadpool(scheduler.save_backup_settings, request.state.body, u["username"])
    await _log(request, "BACKUP_SETTINGS", None, out)
    return json_response({"auto": out})


async def backup_restore(request):
    b, u = request.state.body, request.state.user
    file = str(b.get("file") or "")
    if b.get("confirm") != "RESTORE":
        raise backup.BackupError("Type RESTORE to confirm.")
    await _log(request, "RESTORE_STARTED", file)
    out = await run_in_threadpool(backup.restore, file, u["username"])
    await run_in_threadpool(auth.log, u["username"], client_ip(request), "RESTORE_DONE", file, out)
    return json_response(out)


async def backup_download(request):
    file = str(request.state.body.get("file") or "")
    p = backup._resolve(file)
    await _log(request, "BACKUP_DOWNLOAD", file)
    return _file(await run_in_threadpool(p.read_bytes), "application/octet-stream", file)


# ---------------------------------------------------------------- data import
async def import_meta(request):
    return json_response({"kinds": importer.kinds(), "history": await run_in_threadpool(importer.history)})


async def import_upload(request):
    """Raw file as the request body; kind and file name travel in headers."""
    user = await current_user(request)
    if not user or user["state"] != "ok" or not auth.is_full_admin(user):
        return error(403, "This needs an administrator.")
    if request.headers.get("x-requested-with") != "itam-portal":
        return error(403, "Rejected: not a portal request.")
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        return error(403, "Rejected: cross-site request.")
    if int(request.headers.get("content-length") or 0) > importer.MAX_BYTES:
        return error(413, "The file is too large.")
    data = await request.body()
    request.state.user = user
    out = await run_in_threadpool(importer.save_upload, request.headers.get("x-kind", ""), unquote(request.headers.get("x-filename", "")), data, user["username"])
    await _log(request, "IMPORT_UPLOAD", out["filename"], {"kind": request.headers.get("x-kind"), "bytes": out["size_bytes"]})
    return json_response(out)


async def import_check(request):
    b = request.state.body
    out = await run_in_threadpool(importer.check, str(b.get("job_id") or ""), b.get("as_of") or None)
    await _log(request, "IMPORT_CHECK", b.get("job_id"), {"ok": out["ok"]})
    return json_response(out)


async def import_load(request):
    b, u = request.state.body, request.state.user
    out = await run_in_threadpool(importer.load, str(b.get("job_id") or ""), u["username"], b.get("as_of") or None, bool(b.get("force")))
    await _log(request, "IMPORT_LOAD", b.get("job_id"), {"ok": out["ok"], "backup": out["safety_backup"]})
    if out["ok"]:
        await run_in_threadpool(_after_import)
    return json_response(out)


def _after_import():
    with db.write() as con:
        pm.ensure_cycle(con)
        pm.capture_snapshot(con, pm.quarter(dt.date.today())["label"], dt.date.today())


async def import_detail(request):
    return json_response(await run_in_threadpool(importer.detail, request.path_params["job"]))


# ---------------------------------------------------------------- e-mail
async def email_state(request):
    return json_response({"smtp": await run_in_threadpool(mailer.get_smtp), "rules": await run_in_threadpool(mailer.rules_state), "log": await run_in_threadpool(mailer.log_list, 30)})


async def email_save(request):
    b, u = request.state.body, request.state.user
    out = await run_in_threadpool(mailer.save_smtp, b.get("smtp") or {}, b.get("password") or None, u["username"])
    await _log(request, "EMAIL_SETTINGS", None, {"host": out["host"], "enabled": out["enabled"]})
    return json_response({"smtp": out})


async def email_test(request):
    b, u = request.state.body, request.state.user
    to = str(b.get("to") or u["email"] or "").strip()
    if not EMAIL.fullmatch(to):
        raise mailer.MailError("Enter the address to send the test to.")
    body, text = mailer.page("Test message", "If you can read this, the ITAM Portal can send e-mail.", footer="Sent from Administration > E-mail.")
    await run_in_threadpool(mailer.send, to, "ITAM Portal test message", text, body)
    await _log(request, "EMAIL_TEST", to)
    return json_response({"sent_to": to})


async def email_rule(request):
    b, u = request.state.body, request.state.user
    await run_in_threadpool(mailer.save_rule, str(b.get("key") or ""), b.get("enabled"), b.get("extra_to"), b.get("params"), u["username"])
    await _log(request, "EMAIL_RULE", b.get("key"), {"enabled": bool(b.get("enabled"))})
    return json_response({"rules": await run_in_threadpool(mailer.rules_state)})


async def email_run(request):
    b = request.state.body
    dry = bool(b.get("dry_run", True))
    out = await run_in_threadpool(mailer.run_rules, dry, b.get("only") or None)
    await _log(request, "EMAIL_RUN" if not dry else "EMAIL_PREVIEW", b.get("only"), {"items": len(out)})
    return json_response({"dry_run": dry, "items": out})


async def email_log(request):
    return json_response(await run_in_threadpool(mailer.log_list, 100, int(request.query_params.get("offset", 0))))


routes = [
    Route("/api/reports/meta", R_(report_meta)),
    Route("/api/reports/run", W_(report_run, mutates=False), methods=["POST"]),         # preview only - reads data, writes nothing
    Route("/api/reports/values", W_(report_values, mutates=False), methods=["POST"]),   # filter-value autocomplete - read-only
    Route("/api/reports/export", W_(report_export, mutates=False), methods=["POST"]),   # generates a file from current data - no data is written
    Route("/api/reports/save", W_(report_save), methods=["POST"]),
    Route("/api/reports/delete", W_(report_delete), methods=["POST"]),
    Route("/api/dashboard/export", W_(dash_export, mutates=False), methods=["POST"]),   # same as report export - a download, not a write
    Route("/api/dashboard/share", W_(dash_share), methods=["POST"]),                    # sends a real e-mail - stays blocked for a read-only account
    Route("/api/pm/dashboard", R_(pm_dashboard)),
    Route("/api/pm/cycles", R_(pm_cycles, admin=True)),
    Route("/api/pm/record", W_(pm_record), methods=["POST"]),
    Route("/api/pm/snapshot", W_(pm_snapshot, admin=True), methods=["POST"]),
    Route("/api/pm/rollover", W_(pm_rollover, admin=True), methods=["POST"]),
    Route("/api/admin/backups", R_(backup_list, admin="strict")),
    Route("/api/admin/backups/create", W_(backup_now, admin="strict"), methods=["POST"]),
    Route("/api/admin/backups/verify", W_(backup_verify, admin="strict"), methods=["POST"]),
    Route("/api/admin/backups/settings", W_(backup_settings, admin="strict"), methods=["POST"]),
    Route("/api/admin/backups/restore", W_(backup_restore, admin="strict"), methods=["POST"]),
    Route("/api/admin/backups/download", W_(backup_download, admin="strict"), methods=["POST"]),
    Route("/api/admin/import", R_(import_meta, admin="strict")),
    Route("/api/admin/import/upload", _tool_errors(import_upload), methods=["POST"]),
    Route("/api/admin/import/check", W_(import_check, admin="strict"), methods=["POST"]),
    Route("/api/admin/import/load", W_(import_load, admin="strict"), methods=["POST"]),
    Route("/api/admin/import/{job}", R_(import_detail, admin="strict")),
    Route("/api/admin/email", R_(email_state, admin="strict")),
    Route("/api/admin/email/save", W_(email_save, admin="strict"), methods=["POST"]),
    Route("/api/admin/email/test", W_(email_test, admin="strict"), methods=["POST"]),
    Route("/api/admin/email/rule", W_(email_rule, admin="strict"), methods=["POST"]),
    Route("/api/admin/email/run", W_(email_run, admin="strict"), methods=["POST"]),
    Route("/api/admin/email/log", R_(email_log, admin="strict")),
]
