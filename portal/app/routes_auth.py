"""Sign-in, second factor, password, account self-service, administration of users and settings, activity log."""
import re

from starlette.concurrency import run_in_threadpool
from starlette.routing import Route

from . import auth, db
from .web import client_ip, current_user, error, guarded, json_response, public_write, read, write


def _me(user):
    s = auth.settings()
    return {"authenticated": True, "state": user["state"], "user": auth.public_user(user), "idle_timeout_s": s["idle_timeout_min"] * 60, "idle_left_s": user.get("idle_left"),
            "policy": auth.policy_text(), "policy_min": s["pw_min_length"], "is_admin": user["role"] == "ADMIN",
            "is_full_admin": auth.is_full_admin(user)}


def _set_cookie(request, response, token):
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(auth.COOKIE, token, httponly=True, samesite="strict", secure=secure, path="/")
    return response


async def me(request):
    user = await current_user(request)
    if not user:
        return json_response({"authenticated": False, "policy": auth.policy_text(), "policy_min": auth.settings()["pw_min_length"]})
    return json_response(_me(user))


async def login(request):
    b = request.state.body
    ip = client_ip(request)
    token = await run_in_threadpool(auth.login, str(b.get("username") or ""), str(b.get("password") or ""), ip, request.headers.get("user-agent"))
    user = await run_in_threadpool(auth.session_user, token)
    return _set_cookie(request, json_response(_me(user)), token)


async def forgot_password(request):
    """Same answer whether or not the user name exists, has an address, or the mail went - the page must not reveal which accounts exist."""
    await run_in_threadpool(auth.request_password_reset, str(request.state.body.get("username") or ""), client_ip(request))
    return json_response({"ok": True, "message": "If that user name is valid, a temporary password has been e-mailed to the address on the account. "
                          "If the account has no e-mail address, your request has been passed to the portal administrator, who will give you a temporary password."})


async def second_factor(request):
    token = request.cookies.get(auth.COOKIE)
    await run_in_threadpool(auth.verify_second_factor, token, str(request.state.body.get("code") or ""), client_ip(request))
    user = await run_in_threadpool(auth.session_user, token)
    return json_response(_me(user))


async def logout(request):
    user = await current_user(request)
    await run_in_threadpool(auth.logout, request.cookies.get(auth.COOKIE), user["username"] if user else None, client_ip(request))
    r = json_response({"ok": True})
    r.delete_cookie(auth.COOKIE, path="/")
    return r


async def logout_others(request):
    u = request.state.user
    n = await run_in_threadpool(auth.logout_others, u, request.cookies.get(auth.COOKIE))
    await run_in_threadpool(auth.log, u["username"], client_ip(request), "LOGOUT_OTHERS", detail={"sessions": n})
    return json_response({"ended": n})


async def keepalive(request):
    return json_response({"ok": True, "idle_timeout_s": auth.settings()["idle_timeout_min"] * 60})


async def change_password(request):
    b, u = request.state.body, request.state.user
    await run_in_threadpool(auth.change_password, u, str(b.get("current") or ""), str(b.get("new") or ""), request.cookies.get(auth.COOKIE))
    await run_in_threadpool(auth.log, u["username"], client_ip(request), "PASSWORD_CHANGED")
    return json_response(_me(await run_in_threadpool(auth.session_user, request.cookies.get(auth.COOKIE))))


async def totp_begin(request):
    return json_response(await run_in_threadpool(auth.totp_begin, request.state.user))


async def totp_enable(request):
    u = request.state.user
    out = await run_in_threadpool(auth.totp_enable, u, str(request.state.body.get("code") or ""))
    await run_in_threadpool(auth.log, u["username"], client_ip(request), "2FA_ENABLED")
    return json_response(out)


async def totp_disable(request):
    b, u = request.state.body, request.state.user
    await run_in_threadpool(auth.totp_disable, u, str(b.get("password") or ""), str(b.get("code") or ""))
    await run_in_threadpool(auth.log, u["username"], client_ip(request), "2FA_DISABLED")
    return json_response({"ok": True})


async def view_log(request):
    """The browser reports which page a person opened - part of the per-user activity trail."""
    path = re.sub(r"[^\w/#?=&.%|~:-]", "", str(request.state.body.get("path") or ""))[:200]
    await run_in_threadpool(auth.log, request.state.user["username"], client_ip(request), "VIEW", path)
    return json_response({"ok": True})


# ---------------------------------------------------------------- administration
async def users_list(request):
    return json_response({"users": await run_in_threadpool(auth.list_users), "settings": await run_in_threadpool(auth.settings, True), "policy": auth.policy_text(), "reset_requests": await run_in_threadpool(auth.reset_requests),
                          "engineers": [r["engineer_key"] for r in await run_in_threadpool(db.query, "SELECT engineer_key FROM portal_engineer ORDER BY 1")]})


async def user_create(request):
    """A manual account for someone not on the CIPL roster (a general ONGC employee, say) - sync_from_roster covers
    everyone who is. `engineer_key` is optional either way."""
    b, u = request.state.body, request.state.user
    out = await run_in_threadpool(auth.create_user, b.get("username"), b.get("display_name"), b.get("email"), b.get("role") or "USER", u["username"], b.get("engineer_key") or None)
    await run_in_threadpool(auth.log, u["username"], client_ip(request), "USER_CREATED", out["username"])
    return json_response({**out, "users": await run_in_threadpool(auth.list_users)})


async def user_sync(request):
    u = request.state.user
    out = await run_in_threadpool(auth.sync_from_roster, u["username"])
    await run_in_threadpool(auth.log, u["username"], client_ip(request), "USERS_SYNCED",
                            detail={"created": len(out["created"]), "updated": len(out["updated"]), "skipped": len(out["skipped_no_login"]), "errors": len(out["errors"])})
    return json_response({**out, "users": await run_in_threadpool(auth.list_users)})


async def user_update(request):
    b, u = request.state.body, request.state.user
    uid = int(b.get("user_id"))
    if uid == u["user_id"] and (b.get("active") is False or (b.get("role") and b["role"] != u["role"])):
        return error(409, "You cannot deactivate or demote your own account.")
    changes = {k: b[k] for k in ("display_name", "email", "role", "active", "unlock", "reset_2fa", "engineer_key", "call_parts_access", "extended_access") if k in b}
    out = await run_in_threadpool(auth.update_user, uid, changes, u["username"], True)
    await run_in_threadpool(auth.log, u["username"], client_ip(request), "USER_UPDATED", out["username"], changes)
    return json_response({"user": out})


async def user_reset_password(request):
    b, u = request.state.body, request.state.user
    out = await run_in_threadpool(auth.reset_password, int(b.get("user_id")), u["username"], bool(b.get("email")))
    await run_in_threadpool(auth.log, u["username"], client_ip(request), "PASSWORD_RESET", out["username"],
                            {"emailed_to": out["emailed_to"]} if "emailed_to" in out else None)
    return json_response(out)


async def settings_save(request):
    u = request.state.user
    out = await run_in_threadpool(auth.save_settings, request.state.body.get("settings") or {}, u["username"])
    await run_in_threadpool(auth.log, u["username"], client_ip(request), "SETTINGS_CHANGED", "security", request.state.body.get("settings"))
    return json_response({"settings": out, "policy": auth.policy_text()})


async def activity_list(request):
    a = dict(request.query_params)
    limit = max(1, min(int(a.get("limit", 100)), 200))
    offset = max(0, int(a.get("offset", 0)))
    parts, params = ["true"], []
    for k, col in (("user", "username"), ("action", "action"), ("hostname", "hostname")):
        if a.get(k):
            parts.append(f"{col} = ANY(%s)")
            params.append([x for x in a[k].split("|") if x])
    if a.get("ok") in ("0", "1"):
        parts.append("ok = %s")
        params.append(a["ok"] == "1")
    for tok in re.findall(r"\S+", (a.get("q") or "").lower())[:6]:
        parts.append("lower(coalesce(username,'') || ' ' || action || ' ' || coalesce(target,'') || ' ' || coalesce(ip,'') || ' ' || coalesce(hostname,'') || ' ' || coalesce(detail::text,'')) LIKE %s")
        params.append("%" + tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    w = " AND ".join(parts)

    def run():
        rows = db.query(f"SELECT activity_id AS id, at, username, ip, hostname, action, target, detail, ok FROM portal_activity WHERE {w} ORDER BY activity_id DESC LIMIT %s OFFSET %s", params + [limit, offset])
        out = {"rows": rows, "total": db.one(f"SELECT count(*) AS n FROM portal_activity WHERE {w}", params)["n"]}
        if a.get("facets") == "1":
            out["facets"] = {"user": db.query("SELECT username AS v, count(*) AS n FROM portal_activity WHERE username IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 30"),
                             "action": db.query("SELECT action AS v, count(*) AS n FROM portal_activity GROUP BY 1 ORDER BY 2 DESC LIMIT 30"),
                             "hostname": db.query("SELECT hostname AS v, count(*) AS n FROM portal_activity WHERE hostname IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 30")}
        return out
    return json_response(await run_in_threadpool(run))


routes = [
    Route("/api/auth/me", guarded(me)),
    Route("/api/auth/login", public_write(login), methods=["POST"]),
    Route("/api/auth/forgot", public_write(forgot_password), methods=["POST"]),
    Route("/api/auth/totp", public_write(second_factor), methods=["POST"]),
    Route("/api/auth/logout", public_write(logout), methods=["POST"]),
    Route("/api/auth/logout-others", write(logout_others), methods=["POST"]),
    Route("/api/auth/keepalive", write(keepalive, mutates=False), methods=["POST"]),
    Route("/api/auth/password", write(change_password, states=("ok", "change_password", "setup_2fa")), methods=["POST"]),
    Route("/api/auth/2fa/begin", write(totp_begin, states=("ok", "setup_2fa")), methods=["POST"]),
    Route("/api/auth/2fa/enable", write(totp_enable, states=("ok", "setup_2fa")), methods=["POST"]),
    Route("/api/auth/2fa/disable", write(totp_disable), methods=["POST"]),
    Route("/api/activity/view", write(view_log, mutates=False), methods=["POST"]),
    Route("/api/admin/users", read(users_list, admin="strict")),
    Route("/api/admin/users/create", write(user_create, admin="strict"), methods=["POST"]),
    Route("/api/admin/users/sync", write(user_sync, admin="strict"), methods=["POST"]),
    Route("/api/admin/users/update", write(user_update, admin="strict"), methods=["POST"]),
    Route("/api/admin/users/reset-password", write(user_reset_password, admin="strict"), methods=["POST"]),
    Route("/api/admin/settings", write(settings_save, admin="strict"), methods=["POST"]),
    Route("/api/admin/activity", read(activity_list, admin="strict")),
]
