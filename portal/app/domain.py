"""Domain (Active Directory) sign-in for people who are not in the engineer register.

Engineers and every existing portal account sign in exactly as before (app/auth.py). Anyone else is sent here: their CPF number must be an
ACTIVE employee in the HR master, then the domain controller must accept their domain password. The password is handed to the directory
and forgotten - it is never stored, logged or compared here. The account created on first success has no usable portal password.

The directory sits behind a tiny interface (`bind`) so the flow is tested with `FakeDirectory`; the real LDAP client arrives with task D3.
"""
import re

from . import auth, db

DEFAULTS = {"domain_enabled": False, "domain_host": "", "domain_port": 389, "domain_netbios": "ONGC", "domain_base_dns": [], "domain_allow_simple": False}
NO_PASSWORD = "!"          # never a valid scrypt hash: verify_password() rejects it, so a domain account cannot use the portal password path


class DirectoryDown(Exception):
    """The domain controller could not be reached or answered nonsense - not the same as 'wrong password'."""


class FakeDirectory:
    """Stand-in for tests and the demo: {cpf: (password, {"mail": ...})}. `down=True` simulates an unreachable controller."""
    def __init__(self, accounts=None, down=False):
        self.accounts, self.down = accounts or {}, down

    def bind(self, cpf, password):
        if self.down:
            raise DirectoryDown("timed out")
        hit = self.accounts.get(cpf)
        return dict(hit[1]) if hit and password and hit[0] == password else None


_directory = None          # tests set this; D3 replaces the fallback with the real LDAP client


def get_directory():
    if _directory is not None:
        return _directory
    raise DirectoryDown("The domain connection is not set up yet.")


def config():
    out = dict(DEFAULTS)
    for r in db.query("SELECT key, value FROM portal_setting WHERE key LIKE 'domain\\_%'"):
        if r["key"] in DEFAULTS:
            out[r["key"]] = r["value"]
    return out


def route_for(username):
    """'local' | 'domain'. Local (today's sign-in) for anyone who already has a portal password account or is in the engineer register;
    domain for everyone else - but only while domain sign-in is switched on, so with it off nothing changes for anybody."""
    uname = re.sub(r"\s+", "", username or "").upper()
    if not uname or not config()["domain_enabled"]:
        return "local"
    u = db.one("SELECT auth_source FROM portal_user WHERE username = %s", [uname])
    if u:
        return "domain" if u["auth_source"] == "domain" else "local"
    return "local" if db.one("SELECT 1 AS x FROM portal_engineer WHERE upper(ecode) = %s", [uname]) else "domain"


def login(username, password, ip, user_agent):
    """-> session token. Raises auth.AuthError. Order matters: HR check first, so strangers never reach the domain controller."""
    generic = auth.AuthError("Sign-in failed. Check your user name and password.", 401, code="bad_credentials")
    auth._ip_throttle_check(ip or "")
    uname = re.sub(r"\s+", "", username or "")
    emp = db.one("SELECT cpf_no, employee_name FROM employee WHERE cpf_no = %s AND record_status = 'ACTIVE'", [int(uname)]) if uname.isdigit() and len(uname) <= 9 else None
    if not emp:
        auth._ip_throttle_fail(ip or "")
        auth.log("(UNKNOWN USER)", ip, "LOGIN_FAILED", detail={"via": "domain", "reason": "not an active employee"}, ok=False)    # what was typed is never recorded
        raise auth.AuthError("This user name is not on the HR employee list. Contact the portal administrator.", 403, code="not_employee")
    name = str(emp["cpf_no"])
    u = db.one("SELECT * FROM portal_user WHERE username = %s", [name])
    if u and (u["auth_source"] != "domain" or not u["active"]):
        raise generic                                       # a local account is signed in by auth.login, a deactivated one by nobody
    if u and u["locked_until"] and u["locked_until"] > auth._now():
        mins = int((u["locked_until"] - auth._now()).total_seconds() // 60) + 1
        raise auth.AuthError(f"This account is locked for about {mins} more minute(s) after repeated failed sign-ins.", 423, code="locked")
    try:
        info = get_directory().bind(name, password or "")
    except DirectoryDown as ex:
        auth.log(name, ip, "LOGIN_FAILED", detail={"via": "domain", "reason": "domain unreachable"}, ok=False)
        raise auth.AuthError("The domain is not reachable right now. Try again, or contact the administrator.", 503, code="domain_down") from ex
    if info is None:
        s = auth.settings()
        n, lock = 1, None
        if u:
            n = u["failed_attempts"] + 1
            lock = auth._now() + auth.dt.timedelta(minutes=s["lockout_minutes"]) if n >= s["lockout_attempts"] else None
            with db.write() as con:
                con.execute("UPDATE portal_user SET failed_attempts = %s, locked_until = %s WHERE user_id = %s", (0 if lock else n, lock, u["user_id"]))
        auth._ip_throttle_fail(ip or "")
        auth.log(name, ip, "LOGIN_FAILED", detail={"via": "domain", "attempt": n, "locked": bool(lock)}, ok=False)
        raise generic
    with db.write() as con:                                 # first success creates the account; every success refreshes name and address
        con.execute("""INSERT INTO portal_user (username, display_name, email, role, password_hash, must_change, created_by, auth_source)
                       VALUES (%s,%s,%s,'USER',%s,FALSE,'(domain)','domain')
                       ON CONFLICT (username) DO UPDATE SET display_name = EXCLUDED.display_name, email = COALESCE(EXCLUDED.email, portal_user.email)""",
                    (name, emp["employee_name"], info.get("mail"), NO_PASSWORD))
    return auth.open_session(db.one("SELECT * FROM portal_user WHERE username = %s", [name]), ip, user_agent, via="domain")
