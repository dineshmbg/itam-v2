"""Routes for engineer documents (see engdocs.py). Upload is the raw file as the request body, like the data importer."""
from urllib.parse import unquote

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.routing import Route

from . import auth, engdocs, web
from .web import client_ip, error, guarded, json_response, read, write

DENIED = "You can only see and add your own documents."


def _log(request, action, key, detail=None):
    return run_in_threadpool(auth.log, request.state.user["username"], client_ip(request), action, key, detail)


def _check(request, key):
    if not engdocs.can_access(request.state.user, key):
        raise auth.AuthError(DENIED, 403, code="forbidden")


async def docs_list(request):
    key = request.path_params["key"]
    _check(request, key)
    return json_response({**await run_in_threadpool(engdocs.listing, key), "can_remove": engdocs.can_remove(request.state.user)})


async def docs_file(request):
    key, doc_id = request.path_params["key"], int(request.path_params["doc_id"])
    _check(request, key)
    name, mime, data, doc_type = await run_in_threadpool(engdocs.fetch, key, doc_id)
    await _log(request, "ENG_DOC_DOWNLOAD", key, {"doc": doc_id, "type": doc_type, "file": name})
    return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@guarded
async def docs_upload(request):
    user = await web.current_user(request)
    if not user or user["state"] != "ok":
        return error(401, "Sign in to continue.", code="auth")
    request.state.user = user
    key = request.path_params["key"]
    if request.headers.get("x-requested-with") != "itam-portal":
        return error(403, "Rejected: not a portal request.")
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        return error(403, "Rejected: cross-site request.")
    _check(request, key)
    if int(request.headers.get("content-length") or 0) > engdocs.MAX_BYTES + 4096:
        return error(413, f"The file is larger than {engdocs.MAX_BYTES // (1024 * 1024)} MB.")
    h = request.headers
    out = await run_in_threadpool(engdocs.add, key, h.get("x-doc-type"), unquote(h.get("x-label", "")), h.get("x-expiry"), unquote(h.get("x-filename", "")), await request.body(), user["username"])
    await _log(request, "ENG_DOC_UPLOAD", key, {"doc": out["doc_id"], "type": out["type"], "file": out["filename"], "bytes": out["size"]})
    return json_response(out)


async def docs_remove(request):
    key, b = request.path_params["key"], request.state.body
    await run_in_threadpool(engdocs.remove, key, int(b.get("doc_id") or 0), request.state.user["username"])
    await _log(request, "ENG_DOC_REMOVE", key, {"doc": b.get("doc_id")})
    return json_response({"ok": True})


routes = [
    Route("/api/engineers/{key}/docs", read(docs_list)),
    Route("/api/engineers/{key}/docs/upload", docs_upload, methods=["POST"]),
    Route("/api/engineers/{key}/docs/remove", write(docs_remove, admin="lead"), methods=["POST"]),
    Route("/api/engineers/{key}/docs/{doc_id:int}/file", read(docs_file)),
]
