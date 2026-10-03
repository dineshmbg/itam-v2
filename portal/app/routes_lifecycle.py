"""Replace / Redeploy asset routes. Real administrators only (not Users with extended access): these re-key an asset across the whole database."""
from starlette.concurrency import run_in_threadpool
from starlette.routing import Route

from . import auth, lifecycle, queries
from .web import client_ip, json_response, read, write


def _key(b, name="key"):
    k = b.get(name)
    if not isinstance(k, str) or not k.strip():
        raise lifecycle.Invalid("Missing record key.", {name: "required"})
    return k


async def preflight(request):
    return json_response(await run_in_threadpool(lifecycle.preflight, (request.query_params.get("key") or "").strip().upper()))


async def candidates(request):
    q = request.query_params
    return json_response({"rows": await run_in_threadpool(lifecycle.candidates, q.get("q"), (q.get("exclude") or "").strip().upper())})


async def replace(request):
    b, u, ip = request.state.body, request.state.user, client_ip(request)
    def run():
        r = lifecycle.replace(_key(b), _key(b, "replacement_key"), b, u["username"], ip)
        auth.log(u["username"], ip, "REPLACE_ASSET", f"assets:{r['live_key']}", {"retired": r["retired_key"]})
        return {**r, "detail": queries.detail("assets", r["live_key"], include_archived=True, user=u)}
    return json_response(await run_in_threadpool(run))


async def redeploy(request):
    b, u, ip = request.state.body, request.state.user, client_ip(request)
    def run():
        r = lifecycle.redeploy(_key(b), _key(b, "new_key"), b, u["username"], ip)
        auth.log(u["username"], ip, "REDEPLOY_ASSET", f"assets:{r['live_key']}", {"former": r["former_key"]})
        return {**r, "detail": queries.detail("assets", r["live_key"], include_archived=True, user=u)}
    return json_response(await run_in_threadpool(run))


routes = [
    Route("/api/lifecycle/preflight", read(preflight, admin="strict")),
    Route("/api/lifecycle/candidates", read(candidates, admin="strict")),
    Route("/api/lifecycle/replace", write(replace, admin="strict"), methods=["POST"]),
    Route("/api/lifecycle/redeploy", write(redeploy, admin="strict"), methods=["POST"]),
]
