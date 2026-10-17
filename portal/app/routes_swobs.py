"""Routes for the software check (see swobs.py): the script posts its result with a token (no session - it runs on an ordinary PC), a person can
upload saved result files, and an administrator downloads the script with the portal address and token filled in."""

import datetime as dt

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.routing import Route

from . import auth, db, swobs
from .web import client_ip, current_user, error, guarded, json_response, write


@guarded
async def check_post(request):
    """From the script on a PC. Raw text body; the token travels in a header. 200 only when the result was recorded."""
    if not swobs.token_ok(request.headers.get("x-check-token")):
        return error(403, "Rejected.")
    if int(request.headers.get("content-length") or 0) > swobs.MAX_CHECK_BYTES:
        return error(413, "Too large.")
    out = await run_in_threadpool(swobs.ingest_check, (await request.body()).decode("utf-8", "replace"), "check script")
    if not out["matched"]:
        return error(422, f"{out['computer']} is not a desktop, workstation or server in the asset register.")
    return json_response(out)


@guarded
async def check_upload(request):
    """A saved result file (the script's fallback), uploaded by an administrator or team lead."""
    user = await current_user(request)
    if not user or user["state"] != "ok" or not (auth.is_full_admin(user) or auth.is_lead(user)):
        return error(403, "This needs an administrator.")
    if request.headers.get("x-requested-with") != "itam-portal":
        return error(403, "Rejected: not a portal request.")
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        return error(403, "Rejected: cross-site request.")
    if int(request.headers.get("content-length") or 0) > swobs.MAX_CHECK_BYTES:
        return error(413, "That is not a check-script result (too large).")
    out = await run_in_threadpool(swobs.ingest_check, (await request.body()).decode("utf-8", "replace"), user["username"])
    await run_in_threadpool(auth.log, user["username"], client_ip(request), "SW_CHECK_UPLOAD", out["computer"], {"matched": out["matched"]})
    return json_response(out)


async def script_download(request):
    """The .bat for the share. `rotate` replaces the token (every copy already on the share stops working)."""
    u, b = request.state.user, request.state.body
    tok = await run_in_threadpool(swobs.check_token, True, u["username"], bool(b.get("rotate")))
    base = f"{request.url.scheme}://{request.headers.get('host') or request.url.netloc}"
    text = swobs.script_text(base, tok, str(b.get("drop") or swobs.DEFAULT_DROP)[:200])
    await run_in_threadpool(auth.log, u["username"], client_ip(request), "SW_SCRIPT_DOWNLOAD", None, {"rotated": bool(b.get("rotate"))})
    return Response(text.encode("utf-8"), media_type="application/octet-stream", headers={"Content-Disposition": 'attachment; filename="ankcheck.bat"', "Cache-Control": "no-store"})


async def manual_post(request):
    """One observation typed in from the asset's record window: the administrator, a team lead, or the asset's own engineer."""
    u, b = request.state.user, request.state.body
    key = str(b.get("key") or "")
    asset = await run_in_threadpool(db.one, "SELECT asset_key, asset_class, engineer_name FROM asset WHERE is_current = 1 AND record_level = 'ASSET' AND asset_key = %s", [key])
    mine = bool(asset) and bool(u.get("engineer_key")) and u["engineer_key"] == asset["engineer_name"]
    if not asset or not (auth.is_full_admin(u) or auth.is_lead(u) or mine):
        return error(403, "Only an administrator, a team lead, or the asset's engineer can add this.")
    await run_in_threadpool(swobs.record_manual, key, b.get("facts") or {}, u["username"])
    await run_in_threadpool(auth.log, u["username"], client_ip(request), "SW_MANUAL", key, None)
    return json_response({"software": await run_in_threadpool(swobs.panel, asset["asset_key"], asset["asset_class"])})


async def template_download(request):
    return Response(await run_in_threadpool(swobs.template_bytes), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="Software_Observation_Upload_Template.xlsx"', "Cache-Control": "no-store"})


@guarded
async def bulk_upload(request):
    """The filled Excel template as the raw request body (administrator, team lead, or an engineer for their own machines). All or nothing."""
    user = await current_user(request)
    broad = bool(user) and (auth.is_full_admin(user) or auth.is_lead(user))
    if not user or user["state"] != "ok" or user.get("read_only") or not (broad or user.get("engineer_key")):
        return error(403, "Only an administrator, a team lead or an engineer can upload this.")
    if request.headers.get("x-requested-with") != "itam-portal":
        return error(403, "Rejected: not a portal request.")
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        return error(403, "Rejected: cross-site request.")
    if int(request.headers.get("content-length") or 0) > 5 * 1024 * 1024:
        return error(413, "The file is too large.")
    out = await run_in_threadpool(swobs.ingest_workbook, await request.body(), user["username"], None if broad else user["engineer_key"])
    await run_in_threadpool(auth.log, user["username"], client_ip(request), "SW_BULK_UPLOAD", None, {"recorded": out["recorded"], "errors": len(out["errors"])})
    return json_response(out)


async def report_download(request):
    """The software report (Excel). An administrator or team lead gets every machine; an engineer gets the machines assigned to them."""
    u = request.state.user
    broad = auth.is_full_admin(u) or auth.is_lead(u)
    if not (broad or u.get("engineer_key")):
        return error(403, "Only an administrator, a team lead or an engineer can download this.")
    data = await run_in_threadpool(swobs.report_bytes, None if broad else u["engineer_key"], u["username"])
    await run_in_threadpool(auth.log, u["username"], client_ip(request), "SW_REPORT_DOWNLOAD", None, {"scope": "all" if broad else u["engineer_key"]})
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="Software_Report_{dt.date.today():%Y%m%d}.xlsx"', "Cache-Control": "no-store"})


routes = [
    Route("/api/software/report", write(report_download, mutates=False), methods=["POST"]),
    Route("/api/software/manual", write(manual_post), methods=["POST"]),
    Route("/api/software/template", write(template_download, mutates=False), methods=["POST"]),
    Route("/api/software/bulk", bulk_upload, methods=["POST"]),
    Route("/api/software/check", check_post, methods=["POST"]),
    Route("/api/software/check-upload", check_upload, methods=["POST"]),
    Route("/api/software/script", write(script_download, admin=True), methods=["POST"]),
]
