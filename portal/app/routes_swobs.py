"""Routes for the software check (see swobs.py): the script posts its result with a token (no session - it runs on an ordinary PC), a person can
upload saved result files, and an administrator downloads the script with the portal address and token filled in."""

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.routing import Route

from . import auth, swobs
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


routes = [
    Route("/api/software/check", check_post, methods=["POST"]),
    Route("/api/software/check-upload", check_upload, methods=["POST"]),
    Route("/api/software/script", write(script_download, admin=True), methods=["POST"]),
]
