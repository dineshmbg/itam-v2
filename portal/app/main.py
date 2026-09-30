"""ITAM Portal - ASGI application (Starlette).

Every /api route goes through app/web.py (sign-in required, groups enforced). Reads use a read-only pool; changes go through app/edit.py
(validated, audited); pages update live over Server-Sent Events.
"""
import asyncio
import contextlib
import datetime as dt
import hashlib
import re

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import auth, cards, config, dashboards, db, engineers, export, integrity, queries, routes_auth, routes_edit, routes_tools, routes_update, scheduler
from .live import hub, sse
from .web import client_ip, dumps, error, guarded, read

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")


def qs_key(request):
    # a non-admin engineer's own registers/detail/search responses are scoped to them - the shared server-side cache must be keyed per user too,
    # or one person's request could be served another person's (differently-scoped) cached response.
    u = getattr(request.state, "user", None)
    scope = (u.get("username", "") if u else "") + "|"
    return scope + request.url.path + "?" + "&".join(f"{k}={v}" for k, v in sorted(request.query_params.multi_items()))


async def _cached_threaded(request, fn):
    key = qs_key(request)
    etag = f'W/"{hub.version}-{hashlib.md5(key.encode()).hexdigest()[:12]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, no-cache"})
    hit = hub.cache_get(key)
    if hit is None:
        data = await run_in_threadpool(fn)
        if data is None:
            return error(404, "not found")
        body = dumps(data)
        hub.cache_put(key, (body,))
    else:
        body = hit[1][0]
    return Response(body, media_type="application/json", headers={"ETag": etag, "Cache-Control": "private, no-cache"})


# ---------------------------------------------------------------- reads
async def register_list(request):
    name = request.path_params["name"]
    queries.dataset(name)
    queries.check_access(name, request.state.user)
    args = dict(request.query_params)
    return await _cached_threaded(request, lambda: queries.list_rows(name, args, request.state.user))


async def register_export(request):
    """The register as a CSV file - everything matching the current search and filters, not just the page on screen."""
    name = request.path_params["name"]
    queries.dataset(name)
    user = request.state.user
    queries.check_access(name, user)
    table = await run_in_threadpool(queries.export_table, name, dict(request.query_params), user)
    await run_in_threadpool(auth.log, user["username"], client_ip(request), "EXPORT", f"register:{name}", {"rows": len(table["rows"])})
    fname = f"{name}_{dt.date.today():%Y-%m-%d}.csv"
    return Response(export.csv_bytes(table), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{fname}"', "Cache-Control": "no-store"})


async def asset_labels(request):
    """Printable barcode stickers for the assets matching the current search and filters - same scoping as the CSV export (a User
    only gets the assets assigned to them)."""
    user = request.state.user
    queries.check_access("assets", user)
    table = await run_in_threadpool(queries.export_table, "assets", dict(request.query_params), user)
    await run_in_threadpool(auth.log, user["username"], client_ip(request), "PRINT_LABELS", "register:assets", {"rows": len(table["rows"])})
    fname = f"asset_labels_{dt.date.today():%Y-%m-%d}.pdf"
    return Response(await run_in_threadpool(export.asset_labels_pdf, table["rows"]), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"', "Cache-Control": "no-store"})


async def register_detail(request):
    name, ident = request.path_params["name"], request.path_params["ident"]
    queries.dataset(name)
    queries.check_access(name, request.state.user)
    return await _cached_threaded(request, lambda: queries.detail(name, ident, user=request.state.user))


async def search(request):
    q = request.query_params.get("q", "")
    return await _cached_threaded(request, lambda: queries.search_all(q, user=request.state.user))


async def meta(request):
    body = dumps(queries.meta())
    etag = f'W/"meta-{hashlib.md5(body).hexdigest()[:12]}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, no-cache"})
    return Response(body, media_type="application/json", headers={"Cache-Control": "private, no-cache", "ETag": etag})


async def dash_assets(request):
    u = request.state.user
    eng = None if u["role"] == "ADMIN" or u.get("asset_access") in ("READ", "FULL") else u.get("engineer_key")
    return await _cached_threaded(request, lambda: dashboards.assets(eng))


async def dash_calls(request):
    u = request.state.user
    eng = None if u["role"] == "ADMIN" else u.get("engineer_key")
    return await _cached_threaded(request, lambda: dashboards.calls(eng))


async def dash_engineers(request):
    return await _cached_threaded(request, engineers.overview)


async def engineer_detail(request):
    key, u = request.path_params["key"], request.state.user
    admin = u["role"] == "ADMIN" or u.get("engineer_key") == key    # an engineer also sees their own mobile / personal e-mail here
    return await _cached_threaded(request, lambda: engineers.detail(key, admin))


async def integrity_report(request):
    return await _cached_threaded(request, integrity.report)


async def status(request):
    return Response(dumps({**hub.snapshot()}), media_type="application/json", headers={"Cache-Control": "no-store"})


@guarded
async def health(request):
    try:
        ok = db.one("SELECT 1 AS ok")["ok"] == 1
    except Exception:  # noqa: BLE001
        return error(503, "database unavailable")
    return Response(dumps({"ok": ok}), media_type="application/json", headers={"Cache-Control": "no-store"})


async def events(request):
    token = request.cookies.get(auth.COOKIE)
    q = asyncio.Queue(maxsize=50)
    hub.clients.add(q)

    async def gen():
        try:
            yield b"retry: 3000\n\n" + sse("hello", hub.snapshot())
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    yield sse("change", ev)
                except asyncio.TimeoutError:
                    yield b": ping\n\n"
                    if not await run_in_threadpool(auth.session_user, token):      # signed out / timed out / deactivated: stop pushing data
                        yield sse("auth", {"reason": "session"})
                        break
                if await request.is_disconnected():
                    break
        finally:
            hub.clients.discard(q)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


async def index(request):
    return FileResponse(config.STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


class Static(StaticFiles):
    """Hashed / vendored files are immutable for a year; everything else revalidates (ETag -> 304)."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code in (200, 304):
            immutable = path.startswith(("fonts/", "vendor/")) or re.search(r"-[A-Z0-9]{8}\.[a-z0-9]+$", path)
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable" if immutable else "no-cache"
        return resp


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                h = message.setdefault("headers", [])
                h += [(b"content-security-policy", CSP.encode()), (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                      (b"x-frame-options", b"DENY"), (b"permissions-policy", b"camera=(), microphone=(), geolocation=()")]
            await send(message)
        await self.app(scope, receive, send_wrapper)


def _warm():
    try:
        integrity.masters()
    except Exception:  # noqa: BLE001 - warm-up is best effort
        pass


extra_startup = [scheduler.start]      # background jobs: automatic backups, notification rules, PM upkeep


@contextlib.asynccontextmanager
async def lifespan(app):
    db.open_pool()
    hub.start(asyncio.get_running_loop())
    asyncio.get_running_loop().run_in_executor(None, _warm)   # read the masters workbooks once so the integrity page opens instantly
    stops = [start() for start in extra_startup]
    yield
    for stop in stops:
        if stop:
            stop()
    hub.stop()
    db.close_pool()


routes = [
    Route("/", index),
    Route("/api/health", health),
    Route("/api/meta", read(meta)),
    Route("/api/search", read(search)),
    Route("/api/registers/{name}", read(register_list)),
    Route("/api/registers/{name}/export", read(register_export)),
    Route("/api/registers/assets/labels", read(asset_labels)),
    Route("/api/registers/{name}/{ident:path}", read(register_detail)),
    Route("/api/dash/assets", read(dash_assets)),
    Route("/api/dash/calls", read(dash_calls)),
    Route("/api/dash/engineers", read(dash_engineers, admin=True)),
    Route("/api/engineers/{key}", read(engineer_detail)),
    Route("/api/integrity", read(integrity_report, admin="strict")),
    Route("/api/status", read(status)),
    Route("/api/events", read(events)),
    *routes_auth.routes,
    *routes_edit.routes,
    *cards.routes,
    *routes_tools.routes,
    *routes_update.routes,
    Mount("/static", Static(directory=config.STATIC_DIR, check_dir=False), name="static"),
]

app = Starlette(routes=routes, lifespan=lifespan,
                middleware=[Middleware(SecurityHeaders), Middleware(GZipMiddleware, minimum_size=700, compresslevel=5)])
