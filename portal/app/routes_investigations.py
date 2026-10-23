"""Routes for investigations (see investigations.py). Upload is the raw file as the request body, like the data importer and engineer documents."""
from urllib.parse import unquote

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.routing import Route

from . import auth, investigations, web
from .web import client_ip, error, guarded, json_response, read, write

DENIED = "Investigations are for administrators and Team Leaders/SI."


def _log(request, action, key, detail=None):
    return run_in_threadpool(auth.log, request.state.user["username"], client_ip(request), action, key, detail)


def _check(request):
    if not investigations.can_access(request.state.user):
        raise auth.AuthError(DENIED, 403, code="forbidden")


async def inv_list(request):
    _check(request)
    return json_response(await run_in_threadpool(investigations.listing))


async def inv_sheet(request):
    _check(request)
    return json_response(await run_in_threadpool(investigations.sheet, int(request.path_params["inv_id"]), int(request.path_params["idx"])))


async def inv_file(request):
    _check(request)
    inv_id, which = int(request.path_params["inv_id"]), request.path_params["which"]
    name, mime, data = await run_in_threadpool(investigations.fetch, inv_id, which)
    await _log(request, "INVESTIGATION_DOWNLOAD", str(inv_id), {"file": name})
    return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


async def _raw_upload(request, limit):
    """Shared checks for the two raw-body uploads: signed in, a portal request from this site, allowed, and not too large."""
    user = await web.current_user(request)
    if not user or user["state"] != "ok":
        raise auth.AuthError("Sign in to continue.", 401, code="auth")
    request.state.user = user
    if request.headers.get("x-requested-with") != "itam-portal":
        raise auth.AuthError("Rejected: not a portal request.", 403, code="forbidden")
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        raise auth.AuthError("Rejected: cross-site request.", 403, code="forbidden")
    _check(request)
    if int(request.headers.get("content-length") or 0) > limit + 4096:
        raise investigations.InvError(f"The file is larger than {limit // (1024 * 1024)} MB.", 413)
    return user, await request.body()


@guarded
async def inv_upload(request):
    user, data = await _raw_upload(request, investigations.MAX_XLSX)
    h = request.headers
    out = await run_in_threadpool(investigations.add, unquote(h.get("x-title", "")), unquote(h.get("x-note", "")), unquote(h.get("x-filename", "")), data, user["username"])
    await _log(request, "INVESTIGATION_UPLOAD", str(out["inv_id"]), {"title": out["title"], "sheets": out["sheets"], "bytes": out["size"]})
    return json_response(out)


@guarded
async def inv_pdf(request):
    user, data = await _raw_upload(request, investigations.MAX_PDF)
    inv_id = int(request.path_params["inv_id"])
    out = await run_in_threadpool(investigations.attach_pdf, inv_id, unquote(request.headers.get("x-filename", "")), data, user["username"])
    await _log(request, "INVESTIGATION_PDF", str(inv_id), {"bytes": len(data)})
    return json_response(out)


async def inv_remove(request):
    _check(request)
    inv_id = int(request.state.body.get("inv_id") or 0)
    await run_in_threadpool(investigations.remove, inv_id, request.state.user["username"])
    await _log(request, "INVESTIGATION_REMOVE", str(inv_id))
    return json_response({"ok": True})


routes = [
    Route("/api/investigations", read(inv_list, admin="lead_strict")),
    Route("/api/investigations/upload", inv_upload, methods=["POST"]),
    Route("/api/investigations/remove", write(inv_remove, admin="lead_strict"), methods=["POST"]),
    Route("/api/investigations/{inv_id:int}/sheet/{idx:int}", read(inv_sheet, admin="lead_strict")),
    Route("/api/investigations/{inv_id:int}/file/{which}", read(inv_file, admin="lead_strict")),
    Route("/api/investigations/{inv_id:int}/pdf", inv_pdf, methods=["POST"]),
]
