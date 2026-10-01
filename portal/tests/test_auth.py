"""Sign-in, sessions, password policy, two-factor, lock-out, groups and the activity trail. Runs inside one rolled-back transaction."""
import contextlib
import sys
import time
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import itam_locks  # noqa: E402
from portal.app import auth, config, db  # noqa: E402
from portal.app.main import app  # noqa: E402

HDR = {"X-Requested-With": "itam-portal", "Content-Type": "application/json"}
STRONG = "Vk7#mQ2!zLp9Rw"


@pytest.fixture()
def sandbox(monkeypatch):
    con = psycopg.connect(psycopg.conninfo.make_conninfo(**config.PG, password=db.password(), connect_timeout=8))
    itam_locks.ensure_tables(con)
    auth.ensure_tables(con)
    auth._settings_cache["value"] = None

    @contextlib.contextmanager
    def write():
        with con.transaction():
            yield con

    def rows(sql, params=None):
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    monkeypatch.setattr(db, "write", write)
    monkeypatch.setattr(db, "query", rows)
    monkeypatch.setattr(db, "one", lambda sql, params=None: (rows(sql, params) or [None])[0])
    try:
        yield con
    finally:
        con.rollback()
        con.close()
        auth._settings_cache["value"] = None


def make(sandbox, name="TESTER1", role="USER"):
    """A user that has already changed the temporary password."""
    out = auth.create_user(name, "Test Person", "t@example.com", role, "SETUP")
    sandbox.execute("UPDATE portal_user SET must_change = FALSE, password_hash = %s WHERE username = %s", (auth.hash_password(STRONG), out["username"]))
    return out["username"]


def post(c, path, body):
    return c.post(path, json=body, headers=HDR)


def n(sandbox, sql, params=()):
    return sandbox.execute(sql, params).fetchone()[0]


# ---------------------------------------------------------------- building blocks
def test_totp_matches_rfc6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"          # ASCII "12345678901234567890"
    assert auth._hotp(secret, 59 // 30) == "287082"        # RFC 6238 SHA-1 table: 94287082 -> last six digits
    assert auth._hotp(secret, 1111111109 // 30) == "081804"
    assert auth.totp_match(secret, "287082", t=59) == 1
    assert auth.totp_match(secret, "287082", last_step=1, t=59) is None      # the same step cannot be used twice
    assert auth.totp_match(secret, "000000", t=59) is None and auth.totp_match(secret, "abc", t=59) is None


def test_password_hash_and_policy(sandbox):
    h = auth.hash_password(STRONG)
    assert auth.verify_password(STRONG, h) and not auth.verify_password(STRONG + "x", h) and not auth.verify_password("", "garbage")
    assert auth.policy_problems(STRONG, "TESTER1") == []
    bad = " ".join(auth.policy_problems("short", "TESTER1"))
    min_len = auth.settings(True)["pw_min_length"]   # read live, not hardcoded - an admin may have changed this setting on the real database
    assert f"{min_len} characters" in bad and "digit" in bad and "symbol" in bad and "upper-case" in bad
    assert any("user name" in p for p in auth.policy_problems("Tester1-Abcd#123", "tester1"))
    assert any("common word" in p for p in auth.policy_problems("MyPassword#12345", "x"))
    assert any("differ" in p for p in auth.policy_problems(STRONG, "x", [h]))
    assert auth.policy_problems(auth.generate_temp_password(), "") == []


# ---------------------------------------------------------------- sign in over HTTP
def test_first_sign_in_forces_password_change_and_gates_everything_else(sandbox):
    temp = auth.create_user("TESTER1", "Test Person", None, "USER", "SETUP")["temporary_password"]
    with TestClient(app) as c:
        assert c.get("/api/meta").status_code == 401                                             # no session
        assert c.get("/api/auth/me").json()["authenticated"] is False
        r = post(c, "/api/auth/login", {"username": "tester1", "password": temp})                # user name is not case sensitive
        assert r.status_code == 200 and r.json()["state"] == "change_password"
        assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
        assert c.get("/api/meta").status_code == 403 and c.get("/api/meta").json()["code"] == "change_password"
        assert post(c, "/api/auth/password", {"current": temp, "new": "weak"}).status_code == 400
        r = post(c, "/api/auth/password", {"current": "wrong", "new": STRONG})
        assert r.status_code == 400 and "current" in r.json()["fields"]
        r = post(c, "/api/auth/password", {"current": temp, "new": STRONG})
        assert r.status_code == 200 and r.json()["state"] == "ok"
        assert c.get("/api/meta").status_code == 200
        r = post(c, "/api/auth/password", {"current": STRONG, "new": temp})                       # cannot go back to a recent password
        assert r.status_code == 400
        assert post(c, "/api/auth/logout", {}).status_code == 200
        assert c.get("/api/meta").status_code == 401
    assert n(sandbox, "SELECT count(*) FROM portal_activity WHERE username = 'TESTER1' AND action IN ('LOGIN','LOGOUT','PASSWORD_CHANGED')") == 3


def test_failed_sign_ins_do_not_reveal_which_part_was_wrong_and_lock_the_account(sandbox):
    make(sandbox)
    with TestClient(app) as c:
        a = post(c, "/api/auth/login", {"username": "NOBODY", "password": "x"})
        b = post(c, "/api/auth/login", {"username": "TESTER1", "password": "x"})
        assert a.status_code == b.status_code == 401 and a.json()["error"] == b.json()["error"]
        for _ in range(auth.settings()["lockout_attempts"] - 1):
            post(c, "/api/auth/login", {"username": "TESTER1", "password": "x"})
        r = post(c, "/api/auth/login", {"username": "TESTER1", "password": STRONG})               # even the right password is refused while locked
        assert r.status_code == 423 and "locked" in r.json()["error"].lower()
        auth.update_user(n(sandbox, "SELECT user_id FROM portal_user WHERE username = 'TESTER1'"), {"unlock": True}, "ADMIN")
        assert post(c, "/api/auth/login", {"username": "TESTER1", "password": STRONG}).status_code == 200
    assert n(sandbox, "SELECT count(*) FROM portal_activity WHERE action = 'LOGIN_FAILED' AND username IN ('TESTER1', '(UNKNOWN USER)')") >= auth.settings()["lockout_attempts"]
    assert n(sandbox, "SELECT count(*) FROM portal_activity WHERE username = 'NOBODY'") == 0        # what was typed for an unknown name is never recorded


def test_csrf_guards_on_sign_in(sandbox):
    make(sandbox)
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "TESTER1", "password": STRONG}).status_code == 403
        assert c.post("/api/auth/login", json={"username": "TESTER1", "password": STRONG}, headers={**HDR, "Origin": "http://evil.example"}).status_code == 403


def test_idle_timeout_and_keepalive(sandbox):
    make(sandbox)
    with TestClient(app) as c:
        post(c, "/api/auth/login", {"username": "TESTER1", "password": STRONG})
        assert c.get("/api/meta").status_code == 200
        idle = auth.settings()["idle_timeout_min"]
        sandbox.execute("UPDATE portal_session SET last_active_at = now() - make_interval(mins => %s)", (idle - 1,))
        assert post(c, "/api/auth/keepalive", {}).status_code == 200                              # user activity resets the idle timer
        assert c.get("/api/meta").status_code == 200
        sandbox.execute("UPDATE portal_session SET last_active_at = now() - make_interval(mins => %s)", (idle + 1,))
        assert c.get("/api/meta").status_code == 401                                              # idle too long
        assert post(c, "/api/auth/keepalive", {}).status_code == 401


def test_two_factor_sign_in(sandbox):
    make(sandbox)
    with TestClient(app) as c:
        post(c, "/api/auth/login", {"username": "TESTER1", "password": STRONG})
        b = post(c, "/api/auth/2fa/begin", {}).json()
        assert b["secret"] and b["uri"].startswith("otpauth://totp/") and "<svg" in b["qr_svg"]
        assert post(c, "/api/auth/2fa/enable", {"code": "000000"}).status_code == 400
        code = auth._hotp(b["secret"], auth.totp_step())
        rec = post(c, "/api/auth/2fa/enable", {"code": code}).json()["recovery_codes"]
        assert len(rec) == 8
        post(c, "/api/auth/logout", {})

        r = post(c, "/api/auth/login", {"username": "TESTER1", "password": STRONG})
        assert r.json()["state"] == "2fa"
        assert c.get("/api/meta").status_code == 403 and c.get("/api/meta").json()["code"] == "2fa"   # password alone is not a session
        assert post(c, "/api/auth/totp", {"code": "123456"}).status_code == 401
        good = auth._hotp(b["secret"], auth.totp_step() + 1)                                        # next window; the enrolment code's step is already used
        assert post(c, "/api/auth/totp", {"code": good}).json()["state"] == "ok"
        assert c.get("/api/meta").status_code == 200
        post(c, "/api/auth/logout", {})

        post(c, "/api/auth/login", {"username": "TESTER1", "password": STRONG})
        assert post(c, "/api/auth/totp", {"code": good}).status_code == 401                         # replay of a used code
        assert post(c, "/api/auth/totp", {"code": rec[0]}).json()["state"] == "ok"                  # a recovery code works...
        post(c, "/api/auth/logout", {})
        post(c, "/api/auth/login", {"username": "TESTER1", "password": STRONG})
        assert post(c, "/api/auth/totp", {"code": rec[0]}).status_code == 401                       # ...once
        uid = n(sandbox, "SELECT user_id FROM portal_user WHERE username = 'TESTER1'")
        assert auth.update_user(uid, {"reset_2fa": True}, "ADMIN")["totp_enabled"] is False        # admin can reset a lost device


def test_guessing_the_second_factor_locks_the_account(sandbox):
    make(sandbox)
    u = auth.session_user  # noqa: F841
    with TestClient(app) as c:
        post(c, "/api/auth/login", {"username": "TESTER1", "password": STRONG})
        post(c, "/api/auth/2fa/enable", {"code": auth._hotp(post(c, "/api/auth/2fa/begin", {}).json()["secret"], auth.totp_step())})
        post(c, "/api/auth/logout", {})
        post(c, "/api/auth/login", {"username": "TESTER1", "password": STRONG})
        for _ in range(auth.settings()["lockout_attempts"]):
            post(c, "/api/auth/totp", {"code": "000000"})
        r = post(c, "/api/auth/login", {"username": "TESTER1", "password": STRONG})
        assert r.status_code == 423


# ---------------------------------------------------------------- groups and administration
def test_only_administrators_reach_administration(sandbox):
    make(sandbox, "TESTER1", "USER")
    make(sandbox, "TESTER2", "ADMIN")
    with TestClient(app) as u, TestClient(app) as a:
        post(u, "/api/auth/login", {"username": "TESTER1", "password": STRONG})
        post(a, "/api/auth/login", {"username": "TESTER2", "password": STRONG})
        for path in ("/api/admin/users", "/api/admin/activity"):
            assert u.get(path).status_code == 403 and a.get(path).status_code == 200
        # manual account creation (Add user) needs a real administrator - anyone not on the CIPL roster still needs a login somehow
        assert post(u, "/api/admin/users/create", {"username": "X1X", "display_name": "x y", "role": "USER"}).status_code == 403
        r = post(a, "/api/admin/users/create", {"username": "NEWMANUAL", "display_name": "new person", "role": "USER"})
        assert r.status_code == 200, r.text
        assert r.json()["username"] == "NEWMANUAL" and r.json()["temporary_password"]
        me = a.get("/api/auth/me").json()["user"]
        assert post(a, "/api/admin/users/update", {"user_id": me["user_id"], "active": False}).status_code == 409   # cannot lock yourself out
        assert post(a, "/api/admin/users/update", {"user_id": me["user_id"], "role": "USER"}).status_code == 409
        first = a.get("/api/admin/users").json()["users"]
        assert {"TESTER1", "TESTER2"} <= {x["username"] for x in first} and all("password_hash" not in x and "totp_secret" not in x for x in first)


def test_last_administrator_is_protected_and_deactivation_ends_sessions(sandbox):
    sandbox.execute("UPDATE portal_user SET role = 'USER' WHERE role = 'ADMIN'")           # rolled back at the end
    make(sandbox, "TESTER2", "ADMIN")
    uid = n(sandbox, "SELECT user_id FROM portal_user WHERE username = 'TESTER2'")
    with pytest.raises(auth.AuthError):
        auth.update_user(uid, {"active": False}, "X")
    with pytest.raises(auth.AuthError):
        auth.update_user(uid, {"role": "USER"}, "X")
    make(sandbox, "TESTER3", "USER")
    with TestClient(app) as c:
        post(c, "/api/auth/login", {"username": "TESTER3", "password": STRONG})
        assert c.get("/api/meta").status_code == 200
        auth.update_user(n(sandbox, "SELECT user_id FROM portal_user WHERE username = 'TESTER3'"), {"active": False}, "TESTER2")
        assert c.get("/api/meta").status_code == 401


def test_admin_password_reset_and_settings_validation(sandbox):
    make(sandbox, "TESTER1", "USER")
    make(sandbox, "TESTER2", "ADMIN")
    with TestClient(app) as a:
        post(a, "/api/auth/login", {"username": "TESTER2", "password": STRONG})
        uid = n(sandbox, "SELECT user_id FROM portal_user WHERE username = 'TESTER1'")
        temp = post(a, "/api/admin/users/reset-password", {"user_id": uid}).json()["temporary_password"]
        assert n(sandbox, "SELECT must_change FROM portal_user WHERE user_id = %s", (uid,)) is True
        with TestClient(app) as u:
            r = post(u, "/api/auth/login", {"username": "TESTER1", "password": temp})
            assert r.json()["state"] == "change_password"
        assert post(a, "/api/admin/settings", {"settings": {"idle_timeout_min": 0}}).status_code == 400
        assert post(a, "/api/admin/settings", {"settings": {"nope": 1}}).status_code == 400
        r = post(a, "/api/admin/settings", {"settings": {"idle_timeout_min": 5, "pw_min_length": 14}})
        assert r.status_code == 200 and r.json()["settings"]["idle_timeout_min"] == 5 and "14 characters" in r.json()["policy"]


def test_extended_access_user_works_as_admin_except_control_data_tools_and_administration(sandbox):
    make(sandbox, "TESTER1", "USER")
    sandbox.execute("UPDATE portal_user SET extended_access = TRUE WHERE username = 'TESTER1'")
    with TestClient(app) as c:
        post(c, "/api/auth/login", {"username": "TESTER1", "password": STRONG})
        me = c.get("/api/auth/me").json()
        assert me["is_admin"] is True and me["is_full_admin"] is False
        assert me["user"]["role"] == "USER" and me["user"]["extended_access"] is True     # still shown as a User everywhere
        for path in ("/api/pm/cycles", "/api/dash/engineers", "/api/registers/calls"):
            assert c.get(path).status_code == 200, path
        for path in ("/api/integrity", "/api/audit", "/api/admin/import", "/api/admin/backups",
                     "/api/admin/users", "/api/admin/activity", "/api/admin/email", "/api/admin/email/log", "/api/admin/update"):
            assert c.get(path).status_code == 403, path
        assert post(c, "/api/admin/users/sync", {}).status_code == 403
        assert post(c, "/api/admin/settings", {"settings": {"idle_timeout_min": 5}}).status_code == 403
        assert post(c, "/api/admin/backups/create", {}).status_code == 403
        assert c.post("/api/admin/import/upload", content=b"x", headers={"X-Requested-With": "itam-portal", "X-Filename": "a.xlsx"}).status_code == 403
        assert c.post("/api/admin/update/upload", content=b"x", headers={"X-Requested-With": "itam-portal", "X-Filename": "a.tar"}).status_code == 403
    make(sandbox, "TESTER2", "USER")                                  # without the grant, a User is still kept out
    with TestClient(app) as c:
        post(c, "/api/auth/login", {"username": "TESTER2", "password": STRONG})
        assert c.get("/api/pm/cycles").status_code == 403 and c.get("/api/auth/me").json()["is_admin"] is False


def test_group_set_by_hand_survives_roster_sync(sandbox):
    _clear_roster_accounts(sandbox)
    make(sandbox, "TESTER2", "ADMIN")
    auth.sync_from_roster("SETUP")
    uid = n(sandbox, "SELECT user_id FROM portal_user WHERE username = 'A003550'")          # TEAM LEADER/SI -> ADMIN by designation
    assert n(sandbox, "SELECT role FROM portal_user WHERE user_id = %s", (uid,)) == "ADMIN"
    with TestClient(app) as a:
        post(a, "/api/auth/login", {"username": "TESTER2", "password": STRONG})
        assert post(a, "/api/admin/users/update", {"user_id": uid, "role": "USER", "extended_access": True}).status_code == 200
    assert n(sandbox, "SELECT role_locked FROM portal_user WHERE user_id = %s", (uid,)) is True
    out = auth.sync_from_roster("SETUP")
    assert "A003550" not in out["updated"]
    assert n(sandbox, "SELECT role FROM portal_user WHERE user_id = %s", (uid,)) == "USER"
    assert n(sandbox, "SELECT extended_access FROM portal_user WHERE user_id = %s", (uid,)) is True


def test_manage_user_route_persists_asset_access_and_reassign_grant(sandbox):
    """Real login, real route (not a monkeypatched user) - routes_auth.user_update's field allowlist must actually
    forward these two grants to auth.update_user, same as every other Manage-user field. Caught a real bug on
    2026-10-01: asset_access (and, at the time, the not-yet-added can_reassign_assets) were silently dropped here -
    the Manage dialog's Save button showed "User saved" and the value simply never reached the database."""
    admin = make(sandbox, "TESTER2", "ADMIN")
    target = make(sandbox, "TESTER3", "USER")
    uid = n(sandbox, "SELECT user_id FROM portal_user WHERE username = %s", (target,))
    with TestClient(app) as c:
        post(c, "/api/auth/login", {"username": admin, "password": STRONG})
        r = post(c, "/api/admin/users/update", {"user_id": uid, "asset_access": "READ", "can_reassign_assets": True})
        assert r.status_code == 200, r.text
        assert r.json()["user"]["asset_access"] == "READ" and r.json()["user"]["can_reassign_assets"] is True
    assert n(sandbox, "SELECT asset_access FROM portal_user WHERE user_id = %s", (uid,)) == "READ"
    assert n(sandbox, "SELECT can_reassign_assets FROM portal_user WHERE user_id = %s", (uid,)) is True


def test_password_reset_by_email_sends_it_or_changes_nothing(sandbox, monkeypatch):
    from portal.app import mailer
    make(sandbox, "TESTER1", "USER")
    uid = n(sandbox, "SELECT user_id FROM portal_user WHERE username = 'TESTER1'")
    before = n(sandbox, "SELECT password_hash FROM portal_user WHERE user_id = %s", (uid,))
    sandbox.execute("UPDATE portal_user SET email = NULL WHERE user_id = %s", (uid,))
    with pytest.raises(auth.AuthError):                              # no address on the account
        auth.reset_password(uid, "TESTER2", email=True)
    sandbox.execute("UPDATE portal_user SET email = 'tester1@example.com' WHERE user_id = %s", (uid,))
    cfg = {**mailer.DEFAULT_SMTP, "enabled": True, "host": "mail.local", "from_addr": "itam@example.com", "has_password": False}
    monkeypatch.setattr(mailer, "get_smtp", lambda: cfg)

    def refuse(*a, **k):
        raise mailer.MailError("connection refused")
    monkeypatch.setattr(mailer, "send", refuse)
    with pytest.raises(mailer.MailError, match="not changed"):
        auth.reset_password(uid, "TESTER2", email=True)
    assert n(sandbox, "SELECT password_hash FROM portal_user WHERE user_id = %s", (uid,)) == before   # rolled back

    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, text, html_body=None, cfg=None: sent.append((to, text)))
    out = auth.reset_password(uid, "TESTER2", email=True)
    assert out == {"username": "TESTER1", "emailed_to": "tester1@example.com"}   # the password itself is never returned to the browser
    temp = sent[0][1].split("Temporary password: ")[1].split("\n")[0]
    assert sent[0][0] == "tester1@example.com" and auth.verify_password(temp, n(sandbox, "SELECT password_hash FROM portal_user WHERE user_id = %s", (uid,)))
    assert n(sandbox, "SELECT must_change FROM portal_user WHERE user_id = %s", (uid,)) is True


def test_secrets_never_leave_the_server(sandbox):
    make(sandbox, "TESTER2", "ADMIN")
    with TestClient(app) as a:
        r = post(a, "/api/auth/login", {"username": "TESTER2", "password": STRONG})
        blob = r.text + a.get("/api/auth/me").text + a.get("/api/admin/users").text
        for word in ("password_hash", "totp_secret", "recovery_hashes", "scrypt$"):
            assert word not in blob


def test_activity_trail_is_written_and_only_admins_can_read_it(sandbox):
    make(sandbox, "TESTER2", "ADMIN")
    with TestClient(app) as a:
        post(a, "/api/auth/login", {"username": "TESTER2", "password": STRONG})
        assert post(a, "/api/activity/view", {"path": "#/registers/assets?f.make=HP"}).status_code == 200
        r = a.get("/api/admin/activity?q=registers&facets=1").json()
        assert r["total"] >= 1 and r["rows"][0]["action"] == "VIEW" and r["rows"][0]["username"] == "TESTER2"
        assert {x["v"] for x in r["facets"]["action"]} >= {"LOGIN", "VIEW"} and "TESTER2" in {x["v"] for x in r["facets"]["user"]}
        time.sleep(0)


# ---------------------------------------------------------------- CIPL roster sync
def test_roster_role_matches_site_incharge_si_and_sr_server_engineer():
    assert auth.roster_role("SR SERVER ENGINEER") == "ADMIN"
    assert auth.roster_role("TEAM LEADER/SI") == "ADMIN"
    assert auth.roster_role("SITE INCHARGE") == "ADMIN"
    assert auth.roster_role("SITE IN-CHARGE") == "ADMIN"
    assert auth.roster_role("CUSTOMER SUPPORT ENGINEER") == "USER"
    assert auth.roster_role("DESKTOP SUPPORT ENGINEER") == "USER"
    assert auth.roster_role("BUSINESS ANALYST") == "USER"           # no false positive on a bare "SI" substring
    assert auth.roster_role(None) == "USER" and auth.roster_role("") == "USER"


def _clear_roster_accounts(sandbox):
    """Accounts made by a previous real sync are permanent (production data, with real activity against them - password changes,
    live sessions - that grows over time); clear every table with a foreign key to portal_user.user_id first, inside this
    rolled-back transaction only, so a sync test sees a clean slate without touching the committed rows."""
    for table in ("portal_password_history", "portal_session"):
        sandbox.execute(f"""DELETE FROM {table} WHERE user_id IN
                            (SELECT user_id FROM portal_user WHERE username IN (SELECT ecode FROM cipl_employee WHERE ecode IS NOT NULL))""")
    sandbox.execute("DELETE FROM portal_user WHERE username IN (SELECT ecode FROM cipl_employee WHERE ecode IS NOT NULL)")


def test_sync_from_roster_creates_accounts_with_correct_group_and_skips_office_boy(sandbox):
    _clear_roster_accounts(sandbox)
    out = auth.sync_from_roster("SETUP")
    assert "A003549" in out["skipped_no_login"]                     # Office Boy: no login
    assert n(sandbox, "SELECT count(*) FROM portal_user WHERE username = 'A003549'") == 0
    assert n(sandbox, "SELECT role FROM portal_user WHERE username = 'A003541'") == "ADMIN"   # Dinesh Gadaria, Sr Server Engineer
    assert n(sandbox, "SELECT role FROM portal_user WHERE username = 'A003550'") == "ADMIN"   # Kaushikkumar Sarvan, Team Leader/SI
    assert n(sandbox, "SELECT role FROM portal_user WHERE username = 'A004018'") == "USER"    # Minhaz Patel, Customer Support Engineer
    pw = n(sandbox, "SELECT password_hash FROM portal_user WHERE username = 'A004018'")
    assert auth.verify_password("A004018", pw)                      # default password for a sync-created account = its own user name
    assert n(sandbox, "SELECT must_change FROM portal_user WHERE username = 'A004018'") is True
    assert n(sandbox, "SELECT engineer_key FROM portal_user WHERE username = 'A003541'") == "DINESH GADARIA"
    assert len(out["created"]) == 18 and out["updated"] == [] and out["errors"] == []
    # a malformed roster e-mail never blocks the account: it is left blank and reported (which addresses are malformed depends on the live roster)
    assert n(sandbox, "SELECT count(*) FROM portal_user WHERE email IS NULL AND username = ANY(%s)", (list(out["bad_email"]),)) == len(out["bad_email"])


def test_sync_is_idempotent_and_updates_changed_details_without_touching_password(sandbox):
    _clear_roster_accounts(sandbox)
    auth.sync_from_roster("SETUP")
    again = auth.sync_from_roster("SETUP")
    assert again["created"] == [] and again["updated"] == []
    sandbox.execute("UPDATE cipl_employee SET company_email = 'changed@example.com' WHERE ecode = 'A004018'")
    changed = auth.sync_from_roster("SETUP")
    assert changed["created"] == [] and "A004018" in changed["updated"]
    assert n(sandbox, "SELECT email FROM portal_user WHERE username = 'A004018'") == "changed@example.com"
    pw = n(sandbox, "SELECT password_hash FROM portal_user WHERE username = 'A004018'")
    assert auth.verify_password("A004018", pw)                      # sync never touches an existing password


def test_sync_keeps_a_good_account_email_when_the_roster_one_is_malformed(sandbox):
    _clear_roster_accounts(sandbox)
    auth.sync_from_roster("SETUP")
    sandbox.execute("UPDATE portal_user SET email = 'good@example.com' WHERE username = 'A004018'")
    sandbox.execute("UPDATE cipl_employee SET company_email = 'good2example.com' WHERE ecode = 'A004018'")
    out = auth.sync_from_roster("SETUP")
    assert "A004018" in out["bad_email"] and "A004018" not in out["updated"]
    assert n(sandbox, "SELECT email FROM portal_user WHERE username = 'A004018'") == "good@example.com"


def test_sync_uses_a_custom_password_for_new_accounts(sandbox):
    _clear_roster_accounts(sandbox)
    auth.sync_from_roster("SETUP", password_for_new=lambda ecode: "*ongc123")
    pw = n(sandbox, "SELECT password_hash FROM portal_user WHERE username = 'A003541'")
    assert auth.verify_password("*ongc123", pw)


def test_sync_endpoint_needs_an_administrator_and_logs(sandbox):
    _clear_roster_accounts(sandbox)
    make(sandbox, "TESTER1", "USER")
    make(sandbox, "TESTER2", "ADMIN")
    with TestClient(app) as u, TestClient(app) as a:
        post(u, "/api/auth/login", {"username": "TESTER1", "password": STRONG})
        post(a, "/api/auth/login", {"username": "TESTER2", "password": STRONG})
        assert post(u, "/api/admin/users/sync", {}).status_code == 403
        r = post(a, "/api/admin/users/sync", {})
        assert r.status_code == 200, r.text
        j = r.json()
        assert len(j["created"]) >= 1 and "A003549" in j["skipped_no_login"]
        assert any(x["username"] == "A003541" and x["role"] == "ADMIN" for x in j["users"])
    assert n(sandbox, "SELECT count(*) FROM portal_activity WHERE action = 'USERS_SYNCED' AND username = 'TESTER2'") == 1


def test_one_address_cannot_spray_sign_ins_across_accounts(sandbox):
    for i in range(auth.IP_FAIL_MAX):
        with pytest.raises(auth.AuthError) as e:
            auth.login(f"NOSUCHUSER{i}", "wrong", "10.9.8.7", "test")
        assert e.value.status == 401
    with pytest.raises(auth.AuthError) as e:
        auth.login("ADMIN", "anything", "10.9.8.7", "test")
    assert e.value.status == 429 and e.value.code == "throttled"
    with pytest.raises(auth.AuthError) as e:             # another address is unaffected
        auth.login("NOSUCHUSER", "wrong", "10.9.8.8", "test")
    assert e.value.status == 401


def test_sign_out_other_sessions_keeps_only_the_current_one(sandbox):
    uid = n(sandbox, "SELECT user_id FROM portal_user WHERE active AND NOT read_only ORDER BY user_id LIMIT 1")
    mine = "TOKEN-MINE"
    for tok in ("TOKEN-MINE", "TOKEN-OTHER-1", "TOKEN-OTHER-2"):
        sandbox.execute("INSERT INTO portal_session (token_hash, user_id, stage, expires_at) VALUES (%s, %s, 'ACTIVE', now() + interval '1 hour')", (auth._token_hash(tok), uid))
    assert auth.logout_others({"user_id": uid}, mine) == 2
    left = [r[0] for r in sandbox.execute("SELECT token_hash FROM portal_session WHERE user_id = %s", (uid,)).fetchall()]
    assert auth._token_hash(mine) in left and auth._token_hash("TOKEN-OTHER-1") not in left


def test_demo_user_can_be_switched_off_for_production(sandbox, monkeypatch):
    auth.ensure_demo_user(sandbox)
    assert n(sandbox, "SELECT active FROM portal_user WHERE username = %s", (auth.DEMO_USERNAME,)) is True
    monkeypatch.setenv("PORTAL_DEMO_USER", "off")
    auth.ensure_demo_user(sandbox)
    assert n(sandbox, "SELECT active FROM portal_user WHERE username = %s", (auth.DEMO_USERNAME,)) is False
    monkeypatch.delenv("PORTAL_DEMO_USER")
    auth.ensure_demo_user(sandbox)          # switching it back on is a deliberate act in Users and security, never automatic
    assert n(sandbox, "SELECT active FROM portal_user WHERE username = %s", (auth.DEMO_USERNAME,)) is False
    sandbox.execute("UPDATE portal_user SET active = TRUE WHERE username = %s", (auth.DEMO_USERNAME,))


def test_recover_account_reactivates_unlocks_and_issues_a_new_temporary_password(sandbox):
    uid = n(sandbox, "SELECT user_id FROM portal_user WHERE role = 'ADMIN' AND NOT read_only ORDER BY user_id LIMIT 1")
    name = n(sandbox, "SELECT username FROM portal_user WHERE user_id = %s", (uid,))
    sandbox.execute("UPDATE portal_user SET active = FALSE, failed_attempts = 4, locked_until = now() + interval '1 hour', must_change = FALSE WHERE user_id = %s", (uid,))
    out = auth.recover_account(name.lower())
    assert out["username"] == name and len(out["temporary_password"]) >= 12
    row = sandbox.execute("SELECT active, locked_until, failed_attempts, must_change, role, password_hash FROM portal_user WHERE user_id = %s", (uid,)).fetchone()
    assert row[0] is True and row[1] is None and row[2] == 0 and row[3] is True and row[4] == "ADMIN"
    assert auth.verify_password(out["temporary_password"], row[5])
    assert n(sandbox, "SELECT count(*) FROM portal_activity WHERE action = 'ACCOUNT_RECOVERY' AND target = %s", (name,)) >= 1
    with pytest.raises(auth.AuthError):
        auth.recover_account("NO.SUCH.ACCOUNT")


def test_recover_account_can_set_a_chosen_starting_password(sandbox):
    name = n(sandbox, "SELECT username FROM portal_user WHERE role = 'ADMIN' AND NOT read_only ORDER BY user_id LIMIT 1")
    out = auth.recover_account(name, password="*ongc123")          # weaker than the policy on purpose: it must be changed at the next sign-in
    assert out["temporary_password"] == "*ongc123"
    row = sandbox.execute("SELECT password_hash, must_change, active FROM portal_user WHERE username = %s", (name,)).fetchone()
    assert auth.verify_password("*ongc123", row[0]) and row[1] is True and row[2] is True
