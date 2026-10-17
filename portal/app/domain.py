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


class LdapDirectory:
    """Active Directory over plain LDAP. NTLM first (the password is never sent readable); a simple bind only if the administrator allowed it.
    The bind name is DOMAIN-slash-cpf (for example ONGC then a backslash then the CPF number), so no search is needed to sign in. Only a bind that the controller itself rejects as invalidCredentials is
    'wrong password'; every other failure (no route, timeout, NTLM refused) is DirectoryDown."""
    def __init__(self, host, port, netbios, base_dns=(), allow_simple=False, timeout=5):
        self.host, self.port, self.netbios, self.base_dns, self.allow_simple, self.timeout = host, int(port), netbios, list(base_dns), allow_simple, timeout

    def _bind(self, ldap3, user, password, how):
        server = ldap3.Server(self.host, port=self.port, get_info=ldap3.NONE, connect_timeout=self.timeout)
        conn = ldap3.Connection(server, user=user, password=password, authentication=how, receive_timeout=self.timeout, raise_exceptions=False)
        try:
            ok = conn.bind()
        except Exception as ex:                      # noqa: BLE001 - socket errors, timeouts, protocol errors: all 'cannot reach / cannot talk'
            raise DirectoryDown(type(ex).__name__) from ex
        return conn if ok else (None if (conn.result or {}).get("description") == "invalidCredentials" else DirectoryDown((conn.result or {}).get("description") or "bind failed"))

    def bind(self, cpf, password):
        import ldap3
        from ldap3.utils.conv import escape_filter_chars
        if not password:                             # an empty password would be an anonymous bind, which 'succeeds'
            return None
        user = self.netbios + "\\" + cpf
        res = self._bind(ldap3, user, password, ldap3.NTLM)
        if isinstance(res, DirectoryDown) and self.allow_simple:
            res = self._bind(ldap3, user, password, ldap3.SIMPLE)
        if isinstance(res, DirectoryDown):
            raise res
        if res is None:
            return None
        info = {}
        try:                                         # the address is a courtesy; failing to read it never fails the sign-in
            for base in self.base_dns:
                if res.search(base, f"(sAMAccountName={escape_filter_chars(cpf)})", attributes=["mail"], size_limit=1) and res.entries:
                    info["mail"] = str(res.entries[0].mail.value or "") or None
                    break
        except Exception:                            # noqa: BLE001
            pass
        finally:
            res.unbind()
        return info


    def probe(self):
        """Test connection: one bind per method with a made-up account (no real password is used). -> {"ntlm": ..., "simple": ...}, each
        'ok' (the controller answered and understands the method) or 'refused or no answer: <why>'."""
        import ldap3
        out = {}
        for key, how in (("ntlm", ldap3.NTLM), ("simple", ldap3.SIMPLE)):
            r = self._bind(ldap3, self.netbios + "\\" + "0000001", "not-a-real-password", how)
            out[key] = "ok" if r is None else "refused or no answer: " + str(r)
        return out


_directory = None          # tests set this


def get_directory():
    if _directory is not None:
        return _directory
    c = config()
    if not c["domain_host"]:
        raise DirectoryDown("The domain connection is not set up yet.")
    return LdapDirectory(c["domain_host"], c["domain_port"], c["domain_netbios"], c["domain_base_dns"], c["domain_allow_simple"])


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


def save_config(values, editor):
    """Validate and store the domain settings (portal_setting). Raises auth.AuthError naming the offending field."""
    import json
    c = config()
    c.update({k: v for k, v in values.items() if k in DEFAULTS})
    bad = {}
    host = str(c["domain_host"] or "").strip()
    if host and not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
        bad["domain_host"] = "a host name or IP address"
    try:
        port = int(c["domain_port"])
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        port, bad["domain_port"] = 389, "1 to 65535"
    net = str(c["domain_netbios"] or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9-]{1,15}", net):
        bad["domain_netbios"] = "short domain name, up to 15 letters or digits (for example ONGC)"
    dns = [str(x).strip() for x in (c["domain_base_dns"] or []) if str(x).strip()]
    if len(dns) > 10 or any(len(x) > 300 or "=" not in x for x in dns):
        bad["domain_base_dns"] = "up to 10 distinguished names, one per line (ou=...,dc=...)"
    if c["domain_enabled"] and not host:
        bad["domain_host"] = "required while domain sign-in is on"
    if bad:
        raise auth.AuthError("Check: " + "; ".join(f"{k.replace('domain_', '')} - {v}" for k, v in bad.items()) + ".", 400, fields=bad)
    clean = {"domain_enabled": bool(c["domain_enabled"]), "domain_host": host, "domain_port": port, "domain_netbios": net, "domain_base_dns": dns,
             "domain_allow_simple": bool(c["domain_allow_simple"])}
    with db.write() as con:
        for k, v in clean.items():
            con.execute("INSERT INTO portal_setting (key, value, updated_by) VALUES (%s,%s::jsonb,%s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by",
                        (k, json.dumps(v), editor))
    return clean


def sweep_left(editor="scheduler"):
    """Domain accounts whose person is no longer an ACTIVE employee in the HR master: deactivated and signed out (never deleted - their
    history stays). The sign-in itself already refuses them; this ends a session that was open when they left. -> [username]"""
    with db.write() as con:
        rows = con.execute("""UPDATE portal_user u SET active = FALSE
                              WHERE u.auth_source = 'domain' AND u.active
                                AND NOT EXISTS (SELECT 1 FROM employee e WHERE e.cpf_no::text = u.username AND e.record_status = 'ACTIVE')
                              RETURNING u.user_id, u.username""").fetchall()
        for r in rows:
            con.execute("DELETE FROM portal_session WHERE user_id = %s", (r[0] if not isinstance(r, dict) else r["user_id"],))
    names = [r[1] if not isinstance(r, dict) else r["username"] for r in rows]
    for n in names:
        auth.log(editor, None, "USER_DEACTIVATED", n, {"reason": "no longer an active employee in the HR master"})
    return names
