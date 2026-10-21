"""Remove-from-AMC routes. Administrators and Team Leader/SI users (the people who decide) - not ordinary engineers."""
from starlette.concurrency import run_in_threadpool
from starlette.routing import Route

from . import amcremoval, auth
from .web import client_ip, json_response, read, write


async def preflight(request):
    return json_response(await run_in_threadpool(amcremoval.preflight, (request.query_params.get("key") or "").strip().upper()))


async def remove(request):
    b, u, ip = request.state.body, request.state.user, client_ip(request)
    keys = b.get("keys")
    if not isinstance(keys, list):
        raise amcremoval.Invalid("Choose at least one asset.", {"keys": "required"})
    def run():
        r = amcremoval.remove(keys, b, u["username"], ip)
        auth.log(u["username"], ip, "AMC_REMOVE", r["removal_no"], {"assets": r["assets"], "status": r["status"]})
        return r
    return json_response(await run_in_threadpool(run))


async def impact(request):
    q = request.query_params
    return json_response(await run_in_threadpool(amcremoval.impact, [k for k in (q.get("keys") or "").split(",") if k.strip()], q.get("date")))


async def requests(request):
    return json_response({"rows": await run_in_threadpool(amcremoval.requests)})


async def cancel(request):
    b, u, ip = request.state.body, request.state.user, client_ip(request)
    def run():
        r = amcremoval.cancel(b.get("removal_no"), u["username"], ip)
        auth.log(u["username"], ip, "AMC_REMOVE_CANCEL", r["removal_no"])
        return r
    return json_response(await run_in_threadpool(run))


routes = [
    Route("/api/amc-removal/preflight", read(preflight, admin="lead")),
    Route("/api/amc-removal/impact", read(impact, admin="lead")),
    Route("/api/amc-removal/requests", read(requests, admin="lead")),
    Route("/api/amc-removal/cancel", write(cancel, admin="lead"), methods=["POST"]),
    Route("/api/amc-removal/remove", write(remove, admin="lead"), methods=["POST"]),
]
