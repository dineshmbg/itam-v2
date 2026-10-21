"""Remove-from-AMC routes. Administrators and Team Leader/SI users decide and remove; an engineer can only request for their own assets and read what happened."""
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.routing import Route

from . import amcremoval, auth
from .web import client_ip, current_user, error, guarded, json_response, read, write


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


async def meta(request):
    return json_response(await run_in_threadpool(amcremoval.meta))


async def candidates(request):
    q = request.query_params
    split = lambda name: [x for x in (q.get(name) or "").split(",") if x.strip()]  # noqa: E731
    return json_response(await run_in_threadpool(amcremoval.candidates, {"q": q.get("q"), "classes": split("classes"), "types": split("types"), "engineer": q.get("engineer"),
                                                                       "location": q.get("location"), "components": q.get("components") == "1", "all": q.get("all") == "1"}))


async def impact(request):
    q = request.query_params
    return json_response(await run_in_threadpool(amcremoval.impact, [k for k in (q.get("keys") or "").split(",") if k.strip()], q.get("date")))


async def requests(request):
    return json_response({"rows": await run_in_threadpool(amcremoval.requests, 100, request.query_params.get("status") or None)})


async def cancel(request):
    b, u, ip = request.state.body, request.state.user, client_ip(request)
    def run():
        r = amcremoval.cancel(b.get("removal_no"), u["username"], ip)
        auth.log(u["username"], ip, "AMC_REMOVE_CANCEL", r["removal_no"])
        return r
    return json_response(await run_in_threadpool(run))


def _decider(u):
    return auth.is_full_admin(u) or auth.is_lead(u)


def _owner(u):
    """None = sees every asset (administrator / team leader); otherwise the engineer whose assets the person may use."""
    if _decider(u):
        return None
    if not u.get("engineer_key"):
        raise amcremoval.Invalid("Only an engineer, a team leader or an administrator can use this.")
    return u["engineer_key"]


async def mine(request):
    u = request.state.user
    return json_response(await run_in_threadpool(amcremoval.mine, u.get("engineer_key"), u["username"]))


async def make_request(request):
    b, u, ip = request.state.body, request.state.user, client_ip(request)
    keys = b.get("keys")
    if not isinstance(keys, list):
        raise amcremoval.Invalid("Choose at least one asset.", {"keys": "required"})
    def run():
        r = amcremoval.request(keys, b, u.get("engineer_key"), u["username"], ip)
        auth.log(u["username"], ip, "AMC_REMOVE_REQUEST", r["removal_no"], {"assets": r["assets"]})
        return r
    return json_response(await run_in_threadpool(run))


async def withdraw(request):
    b, u, ip = request.state.body, request.state.user, client_ip(request)
    def run():
        r = amcremoval.cancel(b.get("removal_no"), u["username"], ip, only_own=True)
        auth.log(u["username"], ip, "AMC_REMOVE_CANCEL", r["removal_no"])
        return r
    return json_response(await run_in_threadpool(run))


async def match(request):
    b, u = request.state.body, request.state.user
    tokens = b.get("tokens") if isinstance(b.get("tokens"), list) else amcremoval.parse_list(b.get("text"))
    owner = _owner(u)
    return json_response(await run_in_threadpool(amcremoval.match_list, tokens, owner))


@guarded
async def match_file(request):
    """A saved list (.txt / .csv / .xlsx, first column) as the raw request body; answers like match."""
    u = await current_user(request)
    if not u or u["state"] != "ok" or u.get("read_only"):
        return error(403, "Sign in to continue.")
    if request.headers.get("x-requested-with") != "itam-portal":
        return error(403, "Rejected: not a portal request.")
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        return error(403, "Rejected: cross-site request.")
    if int(request.headers.get("content-length") or 0) > 2 * 1024 * 1024:
        return error(413, "The file is too large.")
    owner = _owner(u)
    data = await request.body()
    def run():
        names, reasons = amcremoval.read_list(data)
        return amcremoval.match_list(names, owner, reasons)
    return json_response(await run_in_threadpool(run))


async def reinstate(request):
    b, u, ip = request.state.body, request.state.user, client_ip(request)
    keys = b.get("keys")
    if not isinstance(keys, list):
        raise amcremoval.Invalid("Choose at least one asset.", {"keys": "required"})
    def run():
        r = amcremoval.reinstate(keys, b, u["username"], ip)
        auth.log(u["username"], ip, "AMC_REINSTATE", ", ".join(r["removals"]), {"assets": r["reinstated"]})
        return r
    return json_response(await run_in_threadpool(run))


async def summary(request):
    return json_response(await run_in_threadpool(amcremoval.summary))


async def export(request):
    data = await run_in_threadpool(amcremoval.report_bytes, request.query_params.get("quarter") or None)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="AMC_removals.xlsx"', "Cache-Control": "no-store"})


async def template(request):
    return Response(await run_in_threadpool(amcremoval.template_bytes), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="AMC_Removal_Upload_Template.xlsx"', "Cache-Control": "no-store"})


async def approve(request):
    b, u, ip = request.state.body, request.state.user, client_ip(request)
    def run():
        r = amcremoval.approve(b.get("removal_no"), b, u["username"], ip)
        auth.log(u["username"], ip, "AMC_REMOVE_APPROVE", r["removal_no"], {"status": r["status"]})
        return r
    return json_response(await run_in_threadpool(run))


async def reject(request):
    b, u, ip = request.state.body, request.state.user, client_ip(request)
    def run():
        r = amcremoval.reject(b.get("removal_no"), b, u["username"], ip)
        auth.log(u["username"], ip, "AMC_REMOVE_REJECT", r["removal_no"])
        return r
    return json_response(await run_in_threadpool(run))


routes = [
    Route("/api/amc-removal/mine", read(mine)),
    Route("/api/amc-removal/template", read(template)),
    Route("/api/amc-removal/request", write(make_request), methods=["POST"]),
    Route("/api/amc-removal/withdraw", write(withdraw), methods=["POST"]),
    Route("/api/amc-removal/match", write(match, mutates=False), methods=["POST"]),
    Route("/api/amc-removal/match-file", match_file, methods=["POST"]),
    Route("/api/amc-removal/reinstate", write(reinstate, admin="lead"), methods=["POST"]),
    Route("/api/amc-removal/summary", read(summary, admin="lead")),
    Route("/api/amc-removal/export", read(export, admin="lead")),
    Route("/api/amc-removal/approve", write(approve, admin="lead"), methods=["POST"]),
    Route("/api/amc-removal/reject", write(reject, admin="lead"), methods=["POST"]),
    Route("/api/amc-removal/preflight", read(preflight, admin="lead")),
    Route("/api/amc-removal/meta", read(meta, admin="lead")),
    Route("/api/amc-removal/candidates", read(candidates, admin="lead")),
    Route("/api/amc-removal/impact", read(impact, admin="lead")),
    Route("/api/amc-removal/requests", read(requests, admin="lead")),
    Route("/api/amc-removal/cancel", write(cancel, admin="lead"), methods=["POST"]),
    Route("/api/amc-removal/remove", write(remove, admin="lead"), methods=["POST"]),
]
