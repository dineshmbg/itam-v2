"""Shared HTTP plumbing: JSON responses, error mapping, and the access-control decorators every route goes through.

  read(fn, admin=False)   - GET-style handler; needs a fully signed-in user (password changed, second factor done).
                            admin=True: administrators, and Users with extended access; admin="strict": real administrators only.
  write(fn, admin=False)  - POST handler; additionally requires the portal marker header, JSON body and a same-origin request (CSRF), and
                            counts as user activity for the idle timer.
Handlers keep the plain `async def h(request)` shape; the user is `request.state.user`, the parsed body `request.state.body`.
"""
import logging
from decimal import Decimal

import orjson
import psycopg
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from . import auth, edit, queries

log = logging.getLogger("itam")


def _default(o):
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(type(o))


def dumps(data):
    return orjson.dumps(data, default=_default, option=orjson.OPT_NON_STR_KEYS)


def json_response(data, status=200, headers=None):
    return Response(dumps(data), status_code=status, media_type="application/json", headers={"Cache-Control": "no-store", **(headers or {})})


def error(status, message, **extra):
    return json_response({"error": message, **extra}, status)


def client_ip(request):
    """The address of the machine the request came from (behind a local reverse proxy uvicorn already substitutes the forwarded one)."""
    ip = request.client.host if request.client else ""
    return ip[7:] if ip.lower().startswith("::ffff:") else ip


def guarded(handler):
    async def wrapper(request):
        try:
            return await handler(request)
        except auth.AuthError as e:
            return error(e.status, str(e), code=e.code, fields=e.fields)
        except queries.BadRequest as e:
            return error(400, str(e))
        except edit.Invalid as e:
            return error(400, str(e), fields=e.fields)
        except edit.Conflict as e:
            return error(409, str(e), current=e.current)
        except edit.NotFound as e:
            return error(404, str(e))
        except ValueError as e:
            return error(400, f"bad parameter: {e}")
        except psycopg.errors.DataError:               # a value the database cannot store (NUL byte, out-of-range number...) is the caller's mistake
            return error(400, "One of the values is not acceptable.")
        except Exception as e:  # noqa: BLE001 - never leak internals to the browser
            if getattr(e, "http_error", False):          # the tools' own, user-readable errors (PM, reports, mail, backup, import)
                return error(getattr(e, "status", 400), str(e))
            log.exception("request failed: %s", request.url)
            return error(503 if "connection" in str(e).lower() or "timeout" in str(e).lower() else 500, "The service could not complete the request.")
    wrapper.__name__ = getattr(handler, "__name__", "handler")
    return wrapper


async def current_user(request):
    return await run_in_threadpool(auth.session_user, request.cookies.get(auth.COOKIE))


def _same_origin(request):
    origin = request.headers.get("origin")
    return not origin or origin.split("://", 1)[-1] == request.headers.get("host")


def read(fn, admin=False, states=("ok",)):
    @guarded
    async def handler(request):
        user = await current_user(request)
        if not user:
            return error(401, "Sign in to continue.", code="auth")
        if user["state"] not in states:
            return error(403, "Finish signing in first.", code=user["state"])
        if admin:
            auth.require_admin(user, strict=admin == "strict")
        request.state.user = user
        return await fn(request)
    return handler


def write(fn, admin=False, states=("ok",), mutates=True):
    """`mutates=False`: a POST that never changes data (session keep-alive, page-view logging) - exempt from the read-only block,
    otherwise a read-only account's session would silently idle-timeout since its keep-alive heartbeat would never reach auth.touch()."""
    @guarded
    async def handler(request):
        if request.headers.get("x-requested-with") != "itam-portal" or "application/json" not in request.headers.get("content-type", ""):
            return error(403, "Rejected: not a portal request.")
        if not _same_origin(request):
            return error(403, "Rejected: cross-site request.")
        user = await current_user(request)
        if not user:
            return error(401, "Sign in to continue.", code="auth")
        if user.get("read_only") and mutates:
            return error(403, "This is a read-only account. Nothing can be changed while signed in as it.", code="read_only")
        if user["state"] not in states:
            return error(403, "Finish signing in first.", code=user["state"])
        if admin:
            auth.require_admin(user, strict=admin == "strict")
        if int(request.headers.get("content-length") or 0) > 1_000_000:
            return error(413, "Request is too large.")
        body = await request.json()
        if not isinstance(body, dict):
            return error(400, "Malformed request.")
        request.state.user, request.state.body = user, body
        response = await fn(request)
        if user["state"] == "ok":
            await run_in_threadpool(auth.touch, request.cookies.get(auth.COOKIE))
        return response
    return handler


def public_write(fn):
    """Pre-login POSTs (sign in, second factor): same CSRF checks, no session required."""
    @guarded
    async def handler(request):
        if request.headers.get("x-requested-with") != "itam-portal" or "application/json" not in request.headers.get("content-type", ""):
            return error(403, "Rejected: not a portal request.")
        if not _same_origin(request):
            return error(403, "Rejected: cross-site request.")
        body = await request.json()
        if not isinstance(body, dict):
            return error(400, "Malformed request.")
        request.state.body = body
        return await fn(request)
    return handler
