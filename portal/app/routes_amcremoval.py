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
        auth.log(u["username"], ip, "AMC_REMOVE", r["removal_no"], {"assets": r["assets"]})
        return r
    return json_response(await run_in_threadpool(run))


routes = [
    Route("/api/amc-removal/preflight", read(preflight, admin="lead")),
    Route("/api/amc-removal/remove", write(remove, admin="lead"), methods=["POST"]),
]
