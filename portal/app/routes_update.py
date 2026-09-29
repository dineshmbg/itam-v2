"""Routes for Software update (administrators only) - see app/update.py."""
from urllib.parse import unquote

from starlette.concurrency import run_in_threadpool
from starlette.routing import Route

from . import auth, update, web
from .web import client_ip, error, guarded, json_response, read, write


def _err(e):
    return error(e.status, str(e))


def _log(request, action, target=None, detail=None):
    u = request.state.user
    return run_in_threadpool(auth.log, u["username"], client_ip(request), action, target, detail)


async def state(request):
    return json_response(await run_in_threadpool(update.state))


@guarded
async def upload(request):
    """The release package as the raw request body (streamed to disk, not held in memory); its file name is in X-Filename."""
    user = await web.current_user(request)
    if not user or user["state"] != "ok" or not auth.is_full_admin(user):
        return error(403, "This needs an administrator.")
    if user.get("read_only"):
        return error(403, "This is a read-only account. Nothing can be changed while signed in as it.", code="read_only")
    if request.headers.get("x-requested-with") != "itam-portal":
        return error(403, "Rejected: not a portal request.")
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        return error(403, "Rejected: cross-site request.")
    if int(request.headers.get("content-length") or 0) > update.MAX_PACKAGE_BYTES:
        return error(413, "The file is too large.")
    request.state.user = user
    name = unquote(request.headers.get("x-filename", ""))
    try:
        up = update.Upload(name)
        try:
            async for chunk in request.stream():
                if chunk:
                    await run_in_threadpool(up.write, chunk)
        except BaseException:
            up.abort()
            raise
        out = await run_in_threadpool(up.finish)
    except update.UpdateError as e:
        return _err(e)
    await _log(request, "UPDATE_UPLOAD", name, {"version": out.get("version"), "bytes": out.get("size")})
    return json_response(out)


async def start(request):
    b, u = request.state.body, request.state.user
    if str(b.get("confirm") or "").strip().upper() != "UPDATE":
        return error(400, "Type UPDATE to confirm.")
    try:
        out = await run_in_threadpool(update.request_install, str(b.get("package") or ""), u["username"], bool(b.get("allow_older")))
    except update.UpdateError as e:
        return _err(e)
    await _log(request, "UPDATE_REQUESTED", str(b.get("package")), {"version": out["version"]})
    return json_response(out)


async def rollback(request):
    b, u = request.state.body, request.state.user
    if str(b.get("confirm") or "").strip().upper() != "ROLLBACK":
        return error(400, "Type ROLLBACK to confirm.")
    try:
        out = await run_in_threadpool(update.request_rollback, u["username"])
    except update.UpdateError as e:
        return _err(e)
    await _log(request, "UPDATE_ROLLBACK_REQUESTED", out["version"])
    return json_response(out)


async def discard(request):
    b = request.state.body
    try:
        await run_in_threadpool(update.discard, str(b.get("package") or ""))
    except update.UpdateError as e:
        return _err(e)
    await _log(request, "UPDATE_DISCARD", str(b.get("package")))
    return json_response({"ok": True})


routes = [
    Route("/api/admin/update", read(state, admin="strict")),
    Route("/api/admin/update/upload", upload, methods=["POST"]),
    Route("/api/admin/update/start", write(start, admin="strict"), methods=["POST"]),
    Route("/api/admin/update/rollback", write(rollback, admin="strict"), methods=["POST"]),
    Route("/api/admin/update/discard", write(discard, admin="strict"), methods=["POST"]),
]
