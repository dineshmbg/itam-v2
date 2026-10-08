"""Routes for the working tools: preventive maintenance, reports and exports, data import, backup and restore, e-mail and sharing."""
import datetime as dt
import re
from urllib.parse import unquote

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.routing import Route

from . import auth, backup, db, export, importer, inventory_match, mailer, packs, pm, pmwo, reports, scheduler
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
    u = request.state.user
    datasets = {}
    for k in reports.SETS:
        try:
            datasets[k] = await run_in_threadpool(reports.describe, k, u)
        except auth.AuthError:
            pass    # a dataset this user cannot open at all (e.g. Calls with call_parts_access=NONE) is just left out, not a 403 for the whole page
    return json_response({"datasets": datasets, "saved": await run_in_threadpool(reports.list_saved, u)})


async def report_run(request):
    b = request.state.body
    return json_response(await run_in_threadpool(reports.run, b.get("definition") or {}, request.state.user, int(b.get("limit") or 200)))


async def report_values(request):
    b = request.state.body
    return json_response({"values": await run_in_threadpool(reports.values, b.get("dataset"), b.get("field"), request.state.user, str(b.get("q") or "")[:40])})


async def report_export(request):
    b = request.state.body
    fmt = b.get("format", "xlsx")
    defn = b.get("definition") or {}
    u = request.state.user
    title = f"{reports.SETS[defn.get('dataset', 'assets')][2]} report" if defn.get("dataset") in reports.SETS else "Report"

    def run():
        table, total = reports.table_for_export(defn, u, title)
        data, mime, ext = export.render(fmt, title.upper(), f"{total:,} rows - {dt.date.today():%d %b %Y} - {u['display_name']}", [table],
                                        footer=u["username"])
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


def _pm_scope(u):
    """None for an administrator; otherwise the user's own engineer_key. A user with no linked engineer sees nothing (never everything)."""
    return None if u["role"] == "ADMIN" else (u.get("engineer_key") or "~no-engineer~")


def _history_args(request, source):
    g = source.get
    as_of = dt.date.fromisoformat(str(g("as_of"))[:10]) if g("as_of") else None
    return str(g("quarter") or ""), as_of, _pm_scope(request.state.user), g("status") or None, str(g("q") or "")[:60] or None


async def pm_history(request):
    return json_response(await run_in_threadpool(pm.history, _pm_scope(request.state.user)))


async def pm_history_detail(request):
    label, as_of, eng, status, q = _history_args(request, request.query_params)
    return json_response(await run_in_threadpool(pm.history_detail, label, as_of, eng, status, q))


async def pm_history_export(request):
    b, u = request.state.body, request.state.user
    label, as_of, eng, status, q = _history_args(request, b)
    fmt = b.get("format", "xlsx")

    def run():
        d, tables, kpis = pm.history_tables(label, as_of, eng, status, q)
        title = f"PM - {label}"
        data, mime, ext = export.render(fmt, title.upper(), f"Snapshot of {d['as_of']:%d %b %Y} - {d['assets']['total']:,} assets - {dt.date.today():%d %b %Y} - {u['display_name']}", tables, kpis, footer=u["username"])
        return data, mime, f"{export.safe_name(title)}_{d['as_of']:%Y%m%d}.{ext}"
    data, mime, name = await run_in_threadpool(run)
    await _log(request, "EXPORT_PM_HISTORY", name, {"format": fmt, "quarter": label})
    return _file(data, mime, name)


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
    # Case-insensitive, trimmed: the confirm box (like every text input on this page) displays uppercase via CSS regardless of
    # what was actually typed, so an exact-match check here would silently reject a correctly-typed lower-case "roll over".
    if str(b.get("confirm") or "").strip().upper() != "ROLL OVER":
        raise pm.PmError('Type ROLL OVER to confirm.')
    await run_in_threadpool(backup.run_backup, "MANUAL", u["username"], "safety copy before PM roll-over")
    out = await run_in_threadpool(pm.rollover, u, client_ip(request), bool(b.get("early")))
    await run_in_threadpool(pmwo.generate, u["username"])      # the new quarter's work orders, ready at once
    await _log(request, "PM_ROLLOVER", out["opened"], out)
    return json_response(out)


# ---------------------------------------------------------------- preventive maintenance: work orders
def _wo_args(request):
    g = request.query_params.get
    return dict(eng=_pm_scope(request.state.user), label=g("quarter") or None)


async def wo_summary(request):
    return json_response(await run_in_threadpool(pmwo.summary, **_wo_args(request)))


async def wo_list(request):
    g = request.query_params.get
    return json_response(await run_in_threadpool(lambda: pmwo.list_orders(**_wo_args(request), bucket=g("bucket"), q=(g("q") or "").strip()[:60] or None, engineer=g("engineer") or None,
                                                                          cls=g("cls") or None, location=g("location") or None, limit=min(int(g("limit") or 300), 1000), offset=int(g("offset") or 0))))


async def wo_detail(request):
    return json_response(await run_in_threadpool(pmwo.detail, int(request.query_params.get("id", 0)), _pm_scope(request.state.user)))


async def wo_generate(request):
    out = await run_in_threadpool(pmwo.generate, request.state.user["username"])
    await _log(request, "PM_WO_GENERATED", out["quarter"], out)
    return json_response(out)


async def wo_save(request):
    b, u = request.state.body, request.state.user
    return json_response(await run_in_threadpool(pmwo.save, int(b.get("id", 0)), b.get("results"), b.get("minutes"), b.get("remarks"), u, _pm_scope(u)))


async def wo_complete(request):
    b, u = request.state.body, request.state.user
    out = await run_in_threadpool(pmwo.complete, int(b.get("id", 0)), b.get("pm_date"), u, client_ip(request), _pm_scope(u))
    await _log(request, "PM_WO_COMPLETED", out["wo_no"], {"asset": out["asset_key"]})
    return json_response(out)


async def wo_batch(request):
    b, u = request.state.body, request.state.user
    out = await run_in_threadpool(pmwo.batch_complete, b.get("ids"), b.get("pm_date"), b.get("minutes"), u, client_ip(request), _pm_scope(u))
    await _log(request, "PM_WO_BATCH", None, {"completed": out["completed"], "skipped": len(out["skipped"])})
    return json_response(out)


async def wo_defer_request(request):
    b, u = request.state.body, request.state.user
    return json_response(await run_in_threadpool(pmwo.defer_request, int(b.get("id", 0)), b.get("reason"), b.get("to"), u, _pm_scope(u)))


async def wo_defer_decide(request):
    b, u = request.state.body, request.state.user
    return json_response(await run_in_threadpool(pmwo.defer_decide, int(b.get("id", 0)), bool(b.get("approve")), b.get("note"), u))


async def wo_verify(request):
    b, u = request.state.body, request.state.user
    out = await run_in_threadpool(pmwo.verify, b.get("ids"), bool(b.get("approve", True)), b.get("note"), u)
    await _log(request, "PM_WO_VERIFY" if b.get("approve", True) else "PM_WO_REJECT", None, {"done": out["done"]})
    return json_response(out)


async def wo_ack(request):
    b, u = request.state.body, request.state.user
    out = await run_in_threadpool(pmwo.acknowledge, b.get("ids"), b.get("action"), b.get("by"), b.get("note"), u, client_ip(request))
    await _log(request, "PM_WO_OWNER_" + str(b.get("action", "")).upper(), None, {"done": out["done"]})
    return json_response(out)


async def wo_cancel(request):
    b, u = request.state.body, request.state.user
    out = await run_in_threadpool(pmwo.cancel, int(b.get("id", 0)), b.get("reason"), u)
    await _log(request, "PM_WO_CANCELLED", out["wo_no"], {"reason": b.get("reason")})
    return json_response(out)


async def wo_findings(request):
    g = request.query_params.get
    return json_response(await run_in_threadpool(pmwo.findings, _pm_scope(request.state.user), (g("status") or "OPEN").upper(), g("quarter") or None))


async def wo_finding_update(request):
    b, u = request.state.body, request.state.user
    return json_response(await run_in_threadpool(pmwo.finding_update, int(b.get("id", 0)), b, u, _pm_scope(u)))


async def wo_checklists(request):
    return json_response(await run_in_threadpool(pmwo.checklists))


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


# ---------------------------------------------------------------- inventory match (report from the centre vs the asset register)
async def match_meta(request):
    return json_response({"history": await run_in_threadpool(inventory_match.history), "classes": inventory_match.ALL_CLASSES, "default_classes": inventory_match.DEFAULT_CLASSES})


async def match_upload(request):
    """Raw file as the request body; the file name travels in a header. The file is only read - nothing is loaded into the database."""
    user = await current_user(request)
    if not user or user["state"] != "ok" or not (auth.is_full_admin(user) or auth.is_lead(user)):
        return error(403, "This needs an administrator.")
    if request.headers.get("x-requested-with") != "itam-portal":
        return error(403, "Rejected: not a portal request.")
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        return error(403, "Rejected: cross-site request.")
    if int(request.headers.get("content-length") or 0) > inventory_match.MAX_BYTES:
        return error(413, "The file is too large.")
    data = await request.body()
    request.state.user = user
    out = await run_in_threadpool(inventory_match.stage, unquote(request.headers.get("x-filename", "")), data, user["username"])
    await _log(request, "MATCH_UPLOAD", out["filename"], {"rows": out["rows"]})
    return json_response(out)


async def match_run(request):
    b, u = request.state.body, request.state.user
    out = await run_in_threadpool(inventory_match.run, b.get("stage_ids") or str(b.get("stage_id") or ""), b.get("mapping") or {}, b.get("params") or {}, u["username"])
    await _log(request, "MATCH_RUN", out["filename"], {"tool": out["summary"]["tool"], "coverage": out["summary"]["coverage"]})
    out["mail"] = await run_in_threadpool(inventory_match.distribute, out["run_id"], u)          # shared with the engineers and mailed to each, automatically
    await _log(request, "MATCH_MAIL", out["filename"], {"run": out["run_id"], "sent": sum(1 for x in out["mail"]["plan"] if x["status"] == "SENT"), "failed": sum(1 for x in out["mail"]["plan"] if x["status"] == "FAILED"),
                                                       "error": out["mail"]["error"]})
    return json_response(out)


def _my_engineer(request, source):
    """Whose machines this person may see: an engineer sees their own (from the linked engineer record); an administrator may preview any engineer's view."""
    u = request.state.user
    if u["role"] == "ADMIN":
        return str(source.get("as") or "") or None
    if not u.get("engineer_key"):
        raise inventory_match.MatchError("Your account is not linked to an engineer record, so there is no personal list to show. Ask an administrator.", 403)
    return u["engineer_key"]


async def _shared(u, run_id):
    if u["role"] != "ADMIN" and not await run_in_threadpool(db.one, "SELECT 1 AS x FROM portal_match_run WHERE run_id = %s AND published_at IS NOT NULL", [run_id]):
        raise inventory_match.MatchError("That analysis has not been shared with engineers.", 403)


async def match_mine(request):
    u = request.state.user
    eng = _my_engineer(request, request.query_params)
    runs = await run_in_threadpool(inventory_match.history, 10) if u["role"] == "ADMIN" else await run_in_threadpool(inventory_match.published)
    out = {"runs": runs, "engineer": eng, "admin": u["role"] == "ADMIN"}
    if u["role"] == "ADMIN" and runs:
        out["engineers"] = await run_in_threadpool(inventory_match.engineer_choices, str(request.query_params.get("run") or runs[0]["run_id"]))
    return json_response(out)


async def match_mine_run(request):
    u = request.state.user
    eng = _my_engineer(request, request.query_params)
    if not eng:
        raise inventory_match.MatchError("Choose an engineer.")
    await _shared(u, request.path_params["run"])
    d = await run_in_threadpool(inventory_match.get, request.path_params["run"])
    return json_response(inventory_match.scoped(d, eng))


async def match_mine_export(request):
    b, u = request.state.body, request.state.user
    eng = _my_engineer(request, b)
    if not eng:
        raise inventory_match.MatchError("Choose an engineer.")
    await _shared(u, str(b.get("run_id") or ""))
    d = await run_in_threadpool(inventory_match.get, str(b.get("run_id") or ""))
    data = await run_in_threadpool(inventory_match.engineer_workbook, d, eng, u)
    name = export.safe_name(f"{d['summary']['tool']}_{eng}_{d['summary']['as_of'].replace('-', '')}") + ".xlsx"
    await _log(request, "MATCH_MY_EXPORT", name, {"run": d["run_id"], "engineer": eng})
    return _file(data, export.MIME["xlsx"], name)


async def match_get(request):
    return json_response(await run_in_threadpool(inventory_match.get, request.path_params["run"]))


async def match_export(request):
    b, u = request.state.body, request.state.user
    fmt = b.get("format", "xlsx")
    if fmt not in ("xlsx", "pdf"):
        raise inventory_match.MatchError("Download as Excel or PDF.")
    d = await run_in_threadpool(inventory_match.get, str(b.get("run_id") or ""))
    data, mime, ext = await run_in_threadpool(inventory_match.render, d, fmt, u)
    name = export.safe_name(f"{d['summary']['tool']}_coverage_{'_'.join(d['summary']['prefixes'])}_{d['summary']['as_of'].replace('-', '')}") + "." + ext
    await _log(request, "MATCH_EXPORT", name, {"format": fmt, "run": d["run_id"]})
    return _file(data, mime, name)


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
        pm.capture_snapshot(con, pm.active_quarter(con)["label"], dt.date.today())


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
    Route("/api/pm/cycles", R_(pm_cycles, admin="lead")),
    Route("/api/pm/history", R_(pm_history)),
    Route("/api/pm/history/detail", R_(pm_history_detail)),
    Route("/api/pm/history/export", W_(pm_history_export, mutates=False), methods=["POST"]),   # a download, not a write
    Route("/api/pm/record", W_(pm_record), methods=["POST"]),
    Route("/api/pm/snapshot", W_(pm_snapshot, admin="lead"), methods=["POST"]),
    Route("/api/pm/rollover", W_(pm_rollover, admin="lead"), methods=["POST"]),
    Route("/api/pmwo/summary", R_(wo_summary)),
    Route("/api/pmwo/list", R_(wo_list)),
    Route("/api/pmwo/detail", R_(wo_detail)),
    Route("/api/pmwo/findings", R_(wo_findings)),
    Route("/api/pmwo/checklists", R_(wo_checklists)),
    Route("/api/pmwo/generate", W_(wo_generate, admin=True), methods=["POST"]),
    Route("/api/pmwo/save", W_(wo_save), methods=["POST"]),
    Route("/api/pmwo/complete", W_(wo_complete), methods=["POST"]),
    Route("/api/pmwo/batch", W_(wo_batch), methods=["POST"]),
    Route("/api/pmwo/defer-request", W_(wo_defer_request), methods=["POST"]),
    Route("/api/pmwo/defer-decide", W_(wo_defer_decide, admin=True), methods=["POST"]),
    Route("/api/pmwo/verify", W_(wo_verify, admin=True), methods=["POST"]),
    Route("/api/pmwo/ack", W_(wo_ack, admin=True), methods=["POST"]),
    Route("/api/pmwo/cancel", W_(wo_cancel, admin=True), methods=["POST"]),
    Route("/api/pmwo/finding", W_(wo_finding_update), methods=["POST"]),
    Route("/api/admin/backups", R_(backup_list, admin="strict")),
    Route("/api/admin/backups/create", W_(backup_now, admin="strict"), methods=["POST"]),
    Route("/api/admin/backups/verify", W_(backup_verify, admin="strict"), methods=["POST"]),
    Route("/api/admin/backups/settings", W_(backup_settings, admin="strict"), methods=["POST"]),
    Route("/api/admin/backups/restore", W_(backup_restore, admin="strict"), methods=["POST"]),
    Route("/api/admin/backups/download", W_(backup_download, admin="strict"), methods=["POST"]),
    Route("/api/match/meta", R_(match_meta, admin="lead_strict")),
    Route("/api/match/upload", _tool_errors(match_upload), methods=["POST"]),
    Route("/api/match/run", W_(match_run, admin="lead_strict"), methods=["POST"]),
    Route("/api/match/mine", R_(match_mine)),
    Route("/api/match/mine/export", W_(match_mine_export, mutates=False), methods=["POST"]),     # a download, not a write
    Route("/api/match/mine/{run}", R_(match_mine_run)),
    Route("/api/match/runs/{run}", R_(match_get, admin="lead_strict")),
    Route("/api/match/export", W_(match_export, admin="lead_strict", mutates=False), methods=["POST"]),   # a download, not a write
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
