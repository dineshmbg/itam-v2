"""Accounts, sessions, password policy, two-factor authentication (TOTP, RFC 6238) and the activity log.

Design notes
  * Passwords: scrypt (stdlib) with a per-user random salt; policy (length, character classes, no username, history, maximum age) is stored in
    portal_setting and enforced on every change. A new or reset account must change its password at first sign-in.
  * Sessions: a random 256-bit token in an HttpOnly, SameSite=Strict cookie; only its SHA-256 is stored. Idle time-out and a hard maximum
    lifetime are enforced on the server. Background refreshes do not extend a session - only explicit keep-alive from user activity does.
  * Two-factor: authenticator-app codes (TOTP) plus single-use recovery codes. A password alone never yields a full session for a 2FA account.
  * Lock-out: repeated failures lock the account for a period; failures are logged with the client address.
  * Every sign-in, sign-out, failure, export, edit, import, backup and administrative action goes to portal_activity.
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import threading
import time

from . import db, netid
from .datasets import ADMIN_ONLY_DATASETS

ROLES = ("ADMIN", "USER")
COOKIE = "itam_session"

DEFAULT_SETTINGS = {
    "idle_timeout_min": 15, "session_max_hours": 12, "pw_min_length": 12, "pw_history": 5, "pw_max_age_days": 90,
    "lockout_attempts": 5, "lockout_minutes": 15, "require_2fa_admin": False,
    # An account that still has its starting password (never signed in, or reset by an administrator) stops being usable after this many days
    # (0 = never). A known starting password sitting unused is the easiest way into the portal; an administrator's Reset password re-arms it.
    "starting_pw_days": 0,
}

DDL = [
    """CREATE TABLE IF NOT EXISTS portal_user (
         user_id SERIAL PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL, email TEXT,
         role TEXT NOT NULL CHECK (role IN ('ADMIN','USER')), password_hash TEXT NOT NULL, password_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         must_change BOOLEAN NOT NULL DEFAULT TRUE, failed_attempts INT NOT NULL DEFAULT 0, locked_until TIMESTAMPTZ,
         totp_secret TEXT, totp_enabled BOOLEAN NOT NULL DEFAULT FALSE, totp_last_step BIGINT, recovery_hashes JSONB,
         active BOOLEAN NOT NULL DEFAULT TRUE, engineer_key TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), created_by TEXT, last_login_at TIMESTAMPTZ,
         read_only BOOLEAN NOT NULL DEFAULT FALSE, call_parts_access TEXT NOT NULL DEFAULT 'NONE' CHECK (call_parts_access IN ('NONE','READ','FULL')))""",
    "ALTER TABLE portal_user ADD COLUMN IF NOT EXISTS read_only BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE portal_user ADD COLUMN IF NOT EXISTS call_parts_access TEXT NOT NULL DEFAULT 'NONE'",
    "ALTER TABLE portal_user DROP CONSTRAINT IF EXISTS portal_user_call_parts_access_check",
    "ALTER TABLE portal_user ADD CONSTRAINT portal_user_call_parts_access_check CHECK (call_parts_access IN ('NONE','READ','FULL'))",
    # extended_access: a User-group account that works like an administrator in dashboards, registers, people, PM and reports - never
    # Control, Data tools or Administration.
    # role_locked: the group was set by hand in Manage user, so roster sync no longer derives it from the designation.
    "ALTER TABLE portal_user ADD COLUMN IF NOT EXISTS extended_access BOOLEAN NOT NULL DEFAULT FALSE",
    # asset_access (2026-09-30): unlike extended_access, this never bumps the effective role to ADMIN - it only widens the Asset
    # dashboard/register/report SCOPE (queries.scope_for, dash_assets, reports.py) to every asset, unscoped. Editing stays governed
    # separately: READ grants no write beyond the usual USER_ASSET_FIELDS-on-their-own-assets; FULL also lets check_edit/check_create/
    # check_archive/check_verify treat any asset as if it were assigned to them. Deliberately narrower than extended_access, which
    # conflates "sees everything" with "can edit everything" across every register, not just Assets.
    "ALTER TABLE portal_user ADD COLUMN IF NOT EXISTS asset_access TEXT NOT NULL DEFAULT 'NONE'",
    "ALTER TABLE portal_user DROP CONSTRAINT IF EXISTS portal_user_asset_access_check",
    "ALTER TABLE portal_user ADD CONSTRAINT portal_user_asset_access_check CHECK (asset_access IN ('NONE','READ','FULL'))",
    "ALTER TABLE portal_user ADD COLUMN IF NOT EXISTS role_locked BOOLEAN NOT NULL DEFAULT FALSE",
    # Forgotten password (2026-10-05): a temporary password e-mailed to the account is stored here, NOT in password_hash, so the old password
    # keeps working until the temporary one is actually used (a stranger typing someone's user name cannot lock them out). With no usable
    # e-mail the request waits in portal_reset_request for an administrator.
    "ALTER TABLE portal_user ADD COLUMN IF NOT EXISTS reset_hash TEXT",
    "ALTER TABLE portal_user ADD COLUMN IF NOT EXISTS reset_expires_at TIMESTAMPTZ",
    "ALTER TABLE portal_user ADD COLUMN IF NOT EXISTS reset_requested_at TIMESTAMPTZ",
    """CREATE TABLE IF NOT EXISTS portal_reset_request (
         request_id SERIAL PRIMARY KEY, user_id INT NOT NULL REFERENCES portal_user(user_id), username TEXT NOT NULL, requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         ip TEXT, reason TEXT NOT NULL, handled_at TIMESTAMPTZ, handled_by TEXT)""",
    "CREATE TABLE IF NOT EXISTS portal_password_history (user_id INT NOT NULL REFERENCES portal_user(user_id), password_hash TEXT NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT now())",
    """CREATE TABLE IF NOT EXISTS portal_session (
         token_hash TEXT PRIMARY KEY, user_id INT NOT NULL REFERENCES portal_user(user_id), stage TEXT NOT NULL CHECK (stage IN ('PENDING_2FA','ACTIVE')),
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_active_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL, ip TEXT, user_agent TEXT)""",
    "CREATE TABLE IF NOT EXISTS portal_setting (key TEXT PRIMARY KEY, value JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_by TEXT)",
    """CREATE TABLE IF NOT EXISTS portal_activity (
         activity_id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now(), username TEXT, ip TEXT, action TEXT NOT NULL, target TEXT, detail JSONB, ok BOOLEAN NOT NULL DEFAULT TRUE)""",
    # the DHCP-assigned IP alone does not reliably identify a PC over time; the hostname (NetBIOS name service, or reverse DNS - see
    # netid.py) is captured at the same moment for exactly that reason.
    "ALTER TABLE portal_activity ADD COLUMN IF NOT EXISTS hostname TEXT",
    "CREATE INDEX IF NOT EXISTS ix_portal_activity_at ON portal_activity (at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_portal_activity_user ON portal_activity (username, at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_portal_session_user ON portal_session (user_id)",
]


class AuthError(Exception):
    def __init__(self, message, status=401, code=None, fields=None):
        super().__init__(message)
        self.status, self.code, self.fields = status, code, fields or {}


def ensure_tables(con):
    for stmt in DDL:
        con.execute(stmt)


# ---------------------------------------------------------------- settings
_settings_cache = {"at": 0, "value": None}


def settings(fresh=False):
    if not fresh and _settings_cache["value"] is not None and time.time() - _settings_cache["at"] < 30:
        return _settings_cache["value"]
    out = dict(DEFAULT_SETTINGS)
    for r in db.query("SELECT key, value FROM portal_setting"):
        if r["key"] in DEFAULT_SETTINGS:
            out[r["key"]] = r["value"]
    _settings_cache.update(at=time.time(), value=out)
    return out


LIMITS = {"idle_timeout_min": (1, 480), "session_max_hours": (1, 72), "pw_min_length": (8, 64), "pw_history": (0, 24), "pw_max_age_days": (0, 730),
          "lockout_attempts": (3, 20), "lockout_minutes": (1, 1440), "starting_pw_days": (0, 365)}


def save_settings(values, editor):
    clean = {}
    for k, v in values.items():
        if k not in DEFAULT_SETTINGS:
            raise AuthError(f"Unknown setting '{k}'.", 400)
        if k == "require_2fa_admin":
            clean[k] = bool(v)
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise AuthError(f"{k}: enter a whole number.", 400, fields={k: "whole number"}) from None
        lo, hi = LIMITS[k]
        if not lo <= n <= hi:
            raise AuthError(f"{k}: must be between {lo} and {hi}.", 400, fields={k: f"{lo} to {hi}"})
        clean[k] = n
    with db.write() as con:
        for k, v in clean.items():
            con.execute("INSERT INTO portal_setting (key, value, updated_by) VALUES (%s,%s::jsonb,%s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by",
                        (k, json.dumps(v), editor))
    _settings_cache["value"] = None
    return settings(True)


# ---------------------------------------------------------------- passwords
def hash_password(password):
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    h = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def verify_password(password, stored):
    try:
        _, n, r, p, salt, h = stored.split("$")
        calc = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except Exception:  # noqa: BLE001 - malformed hash never authenticates
        return False


COMMON = {"password", "passw0rd", "admin", "welcome", "qwerty", "letmein", "changeme", "ongc", "ankleshwar", "iloveyou", "administrator"}


def policy_problems(password, username, history=()):
    s = settings()
    p = []
    if len(password) < s["pw_min_length"]:
        p.append(f"at least {s['pw_min_length']} characters")
    if not re.search(r"[a-z]", password):
        p.append("a lower-case letter")
    if not re.search(r"[A-Z]", password):
        p.append("an upper-case letter")
    if not re.search(r"\d", password):
        p.append("a digit")
    if not re.search(r"[^A-Za-z0-9]", password):
        p.append("a symbol")
    low = password.lower()
    if username and username.lower() in low:
        p.append("must not contain your user name")
    if any(c in low for c in COMMON):
        p.append("must not contain a common word")
    if re.search(r"(.)\1{3,}", password):
        p.append("must not repeat one character 4+ times")
    for old in history:
        if verify_password(password, old):
            p.append(f"must differ from your last {s['pw_history']} passwords")
            break
    return p


def policy_text():
    s = settings()
    return (f"At least {s['pw_min_length']} characters with upper- and lower-case letters, a digit and a symbol; no user name or common word; "
            f"not one of your last {s['pw_history']} passwords." + (f" Expires after {s['pw_max_age_days']} days." if s["pw_max_age_days"] else ""))


def generate_temp_password():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(12)) + secrets.choice("!@#$%*+-=") + str(secrets.randbelow(10))
        if not policy_problems(pw, ""):
            return pw


# ---------------------------------------------------------------- TOTP (RFC 6238, SHA-1, 6 digits, 30 s)
def totp_new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret, counter):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    d = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = d[-1] & 15
    return f"{(struct.unpack('>I', d[o:o + 4])[0] & 0x7FFFFFFF) % 1000000:06d}"


def totp_step(t=None):
    return int((t or time.time()) // 30)


def totp_match(secret, code, last_step=None, t=None):
    """-> matching step or None. Accepts the previous/current/next window; a step already used is refused (replay protection)."""
    code = re.sub(r"\s+", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        return None
    now = totp_step(t)
    for step in (now, now - 1, now + 1):
        if hmac.compare_digest(_hotp(secret, step), code) and (last_step is None or step > last_step):
            return step
    return None


def totp_uri(secret, username):
    from urllib.parse import quote
    return f"otpauth://totp/ITAM%20Portal:{quote(username)}?secret={secret}&issuer=ITAM%20Portal&algorithm=SHA1&digits=6&period=30"


def qr_svg(text):
    import io

    import segno
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=4, border=2, xmldecl=False, svgns=True, nl=False)
    return buf.getvalue().decode()


def new_recovery_codes():
    codes = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}".upper() for _ in range(8)]
    return codes, [hashlib.sha256(c.encode()).hexdigest() for c in codes]


# ---------------------------------------------------------------- activity log
def log(username, ip, action, target=None, detail=None, ok=True):
    """Never raises - logging must not break the action being logged."""
    try:
        hostname = netid.resolve_hostname(ip)
    except Exception:  # noqa: BLE001 - resolve_hostname already guards itself; this is a second layer so a lookup failure never costs the whole audit row
        hostname = None
    try:
        with db.write() as con:
            con.execute("INSERT INTO portal_activity (username, ip, hostname, action, target, detail, ok) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)",
                        (username, ip, hostname, action, target, json.dumps(detail, default=str) if detail is not None else None, ok))
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger("itam").exception("activity log failed")


# ---------------------------------------------------------------- users
def _now():
    return dt.datetime.now(dt.timezone.utc)


def _norm_username(u):
    u = re.sub(r"\s+", "", (u or "")).upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{2,29}", u):
        raise AuthError("User name: 3 to 30 letters, digits, . _ - (no spaces).", 400, fields={"username": "3 to 30 letters, digits, . _ -"})
    return u


def public_user(u):
    # "group" is the real group when u came from session_user() (whose "role" is the effective one) - the browser always sees the real group
    return {"user_id": u["user_id"], "username": u["username"], "display_name": u["display_name"], "email": u["email"], "role": u.get("group") or u["role"], "active": u["active"],
            "totp_enabled": u["totp_enabled"], "must_change": u["must_change"], "locked": bool(u["locked_until"] and u["locked_until"] > _now()),
            "last_login_at": u["last_login_at"].isoformat() if u["last_login_at"] else None, "created_at": u["created_at"].isoformat(), "engineer_key": u["engineer_key"],
            "read_only": bool(u.get("read_only")), "call_parts_access": u.get("call_parts_access") or "NONE",
            "extended_access": bool(u.get("extended_access")), "asset_access": u.get("asset_access") or "NONE", "role_locked": bool(u.get("role_locked")),
            "lead_tools": bool(u.get("lead_tools")), "starting_pw_expired": _starting_pw_expired(u, settings())}


def create_user(username, display_name, email, role, editor, engineer_key=None, password=None):
    username = _norm_username(username)
    display_name = re.sub(r"\s+", " ", (display_name or "").strip()).upper()
    if not 2 <= len(display_name) <= 60:
        raise AuthError("Enter the person's full name.", 400, fields={"display_name": "2 to 60 characters"})
    email = (email or "").strip().lower() or None
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise AuthError("Enter a valid e-mail address.", 400, fields={"email": "invalid"})
    if role not in ROLES:
        raise AuthError("Choose a group: ADMIN or USER.", 400, fields={"role": "invalid"})
    temp = password or generate_temp_password()
    with db.write() as con:
        if con.execute("SELECT 1 FROM portal_user WHERE username = %s", (username,)).fetchone():
            raise AuthError("That user name already exists.", 409, fields={"username": "already exists"})
        con.execute("INSERT INTO portal_user (username, display_name, email, role, password_hash, must_change, created_by, engineer_key) VALUES (%s,%s,%s,%s,%s,TRUE,%s,%s)",
                    (username, display_name, email, role, hash_password(temp), editor, engineer_key))
    return {"username": username, "temporary_password": temp}


# ---------------------------------------------------------------- CIPL roster sync
NO_LOGIN_DESIGNATIONS = {"OFFICE BOY"}
ADMIN_DESIGNATION_RE = re.compile(r"\bSITE\s*IN-?\s*CHARGE\b|\bSI\b|\bSR\.?\s*SERVER\s+ENGINEER\b")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


LEAD_DESIGNATION_RE = re.compile(r"\bTEAM\s*LEAD(?:ER)?\b", re.I)


def is_lead_designation(designation):
    return bool(designation and LEAD_DESIGNATION_RE.search(designation))


def roster_role(designation):
    return "ADMIN" if designation and ADMIN_DESIGNATION_RE.search(designation) else "USER"


def _roster_email(raw):
    """-> (email or None, was_bad). A malformed address in the sheet never blocks the account - it is just left blank."""
    e = (raw or "").strip()
    if not e:
        return None, False
    return (e, False) if EMAIL_RE.fullmatch(e) else (None, True)


def sync_from_roster(editor, password_for_new=None):
    """Create (and refresh) portal logins from the CIPL roster (cipl_employee), one account per active employee.
    Username = ECODE. No login for a designation in NO_LOGIN_DESIGNATIONS. Group follows roster_role().
    New accounts get `password_for_new(ecode)` (default: the ECODE itself); existing accounts keep their password
    and only have display name / e-mail / group / engineer link refreshed to match the roster."""
    password_for_new = password_for_new or (lambda ecode: ecode)
    rows = db.query("SELECT ecode, employee_name, designation, company_email FROM cipl_employee WHERE employment_status = 'ACTIVE' AND is_on_roster = 1 ORDER BY ecode")
    created, updated, skipped, bad_email, errors = [], [], [], [], []
    for r in rows:
        desig = (r["designation"] or "").strip()
        if desig in NO_LOGIN_DESIGNATIONS:
            skipped.append(r["ecode"])
            continue
        role = roster_role(desig)
        email, was_bad = _roster_email(r["company_email"])
        if was_bad:
            bad_email.append(r["ecode"])
        eng = db.one("SELECT engineer_key FROM portal_engineer WHERE ecode = %s", [r["ecode"]])
        engineer_key = eng["engineer_key"] if eng else None
        try:
            existing = db.one("SELECT user_id, display_name, email, role, role_locked, engineer_key FROM portal_user WHERE username = %s", [r["ecode"]])
            if not existing:
                create_user(r["ecode"], r["employee_name"], email, role, editor, engineer_key, password=password_for_new(r["ecode"]))
                created.append(r["ecode"])
            else:
                changes = {}
                if existing["display_name"] != re.sub(r"\s+", " ", (r["employee_name"] or "").strip()).upper():
                    changes["display_name"] = r["employee_name"]
                # A malformed roster address never wipes a good one already on the account - it is only reported in bad_email.
                if not was_bad and (existing["email"] or None) != (email.lower() if email else None):
                    changes["email"] = email
                if existing["role"] != role and not existing["role_locked"]:     # a group set by hand in Manage user is kept
                    changes["role"] = role
                if existing["engineer_key"] != engineer_key:
                    changes["engineer_key"] = engineer_key
                if changes:
                    update_user(existing["user_id"], changes, editor)
                    updated.append(r["ecode"])
        except AuthError as e:
            errors.append({"ecode": r["ecode"], "error": str(e)})
    return {"created": created, "updated": updated, "skipped_no_login": skipped, "bad_email": bad_email, "errors": errors}


def _cols(cur):
    return [d.name for d in cur.description]


def _row(con, sql, params=()):
    with con.cursor() as cur:
        cur.execute(sql, params)
        r = cur.fetchone()
        return dict(zip(_cols(cur), r)) if r else None


def _rows(con, sql, params=()):
    with con.cursor() as cur:
        cur.execute(sql, params)
        c = _cols(cur)
        return [dict(zip(c, r)) for r in cur.fetchall()]


def list_users():
    return [public_user(u) for u in db.query("SELECT * FROM portal_user ORDER BY username")]


def update_user(user_id, changes, editor, lock_role=False):
    """lock_role=True (Manage user, not roster sync): a group actually changed here stays as set - later roster syncs leave it alone."""
    with db.write() as con:
        u = _row(con, "SELECT * FROM portal_user WHERE user_id = %s FOR UPDATE", (user_id,))
        if not u:
            raise AuthError("User not found.", 404)
        sets, vals = [], []
        if "display_name" in changes:
            n = re.sub(r"\s+", " ", str(changes["display_name"]).strip()).upper()
            if not 2 <= len(n) <= 60:
                raise AuthError("Enter the person's full name.", 400, fields={"display_name": "2 to 60 characters"})
            sets.append("display_name = %s"); vals.append(n)
        if "email" in changes:
            e = (changes["email"] or "").strip().lower() or None
            if e and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
                raise AuthError("Enter a valid e-mail address.", 400, fields={"email": "invalid"})
            sets.append("email = %s"); vals.append(e)
        if "role" in changes:
            if changes["role"] not in ROLES:
                raise AuthError("Choose a group: ADMIN or USER.", 400, fields={"role": "invalid"})
            if u["role"] == "ADMIN" and changes["role"] != "ADMIN" and _admin_count(con) <= 1:
                raise AuthError("Cannot remove the last administrator.", 409)
            sets.append("role = %s"); vals.append(changes["role"])
            if lock_role and changes["role"] != u["role"]:
                sets.append("role_locked = TRUE")
        if "extended_access" in changes:
            sets.append("extended_access = %s"); vals.append(bool(changes["extended_access"]))
        if "active" in changes:
            active = bool(changes["active"])
            if not active and u["role"] == "ADMIN" and _admin_count(con) <= 1:
                raise AuthError("Cannot deactivate the last administrator.", 409)
            sets.append("active = %s"); vals.append(active)
            if not active:
                con.execute("DELETE FROM portal_session WHERE user_id = %s", (user_id,))
        if "engineer_key" in changes:
            sets.append("engineer_key = %s"); vals.append((changes["engineer_key"] or None))
        if "call_parts_access" in changes:
            v = changes["call_parts_access"] or "NONE"
            if v not in ("NONE", "READ", "FULL"):
                raise AuthError("Choose none, read-only or full access.", 400, fields={"call_parts_access": "invalid"})
            sets.append("call_parts_access = %s"); vals.append(v)
        if "asset_access" in changes:
            v = changes["asset_access"] or "NONE"
            if v not in ("NONE", "READ", "FULL"):
                raise AuthError("Choose none, view-only or full access.", 400, fields={"asset_access": "invalid"})
            sets.append("asset_access = %s"); vals.append(v)
        if changes.get("unlock"):
            sets.append("failed_attempts = 0"); sets.append("locked_until = NULL")
        if changes.get("reset_2fa"):
            sets += ["totp_enabled = FALSE", "totp_secret = NULL", "recovery_hashes = NULL", "totp_last_step = NULL"]
            con.execute("DELETE FROM portal_session WHERE user_id = %s", (user_id,))
        if sets:
            con.execute(f"UPDATE portal_user SET {', '.join(sets)} WHERE user_id = %s", vals + [user_id])
        return public_user(_row(con, "SELECT * FROM portal_user WHERE user_id = %s", (user_id,)))


def _admin_count(con):
    """Real administrators only - a read-only account (e.g. the demo login) never counts as the one administrator that must remain."""
    return con.execute("SELECT count(*) FROM portal_user WHERE role = 'ADMIN' AND active AND NOT read_only").fetchone()[0]


def reset_password(user_id, editor, email=False):
    """New temporary password, must be changed at next sign-in, signed out everywhere. email=True sends it to the account's own address
    instead of returning it; the mail goes out before the change commits, so if it cannot be sent nothing changes (no one is locked out
    by a password that never arrived). -> {"username", "temporary_password"} or {"username", "emailed_to"}"""
    from . import mailer  # local: mailer pulls in pm/edit, which are not needed for a plain reset
    temp = generate_temp_password()
    with db.write() as con:
        u = _row(con, "SELECT * FROM portal_user WHERE user_id = %s FOR UPDATE", (user_id,))
        if not u:
            raise AuthError("User not found.", 404)
        con.execute("UPDATE portal_user SET password_hash = %s, must_change = TRUE, password_changed_at = now(), failed_attempts = 0, locked_until = NULL, reset_hash = NULL, reset_expires_at = NULL WHERE user_id = %s", (hash_password(temp), user_id))
        con.execute("DELETE FROM portal_session WHERE user_id = %s", (user_id,))
        _close_reset_requests(con, user_id, editor)
        if not email:
            return {"username": u["username"], "temporary_password": temp}
        if not u["email"]:
            raise AuthError(f"{u['username']} has no e-mail address. Add one first, or reset without e-mail.", 400)
        cfg = mailer.get_smtp()
        if not cfg["enabled"]:
            raise mailer.MailError("E-mail is switched off (Administration > E-mail). Nothing was changed.")
        url = cfg["portal_url"]
        text = (f"Hello {u['display_name']},\n\nYour ITAM Portal password has been reset by {editor}.\n\n"
                f"User name: {u['username']}\nTemporary password: {temp}\n\n"
                f"Sign in{(' at ' + url) if url else ''} with this password; you will be asked to choose a new one straight away.\n"
                "If you did not expect this, tell the portal administrator.\n")
        esc = mailer.html.escape
        body = (f"<p style='font-family:Arial;font-size:13px'>Hello {esc(u['display_name'])},</p>"
                f"<p style='font-family:Arial;font-size:13px'>Your ITAM Portal password has been reset by {esc(editor)}.</p>"
                f"<table style='font-family:Arial;font-size:13px'><tr><td style='padding:2px 12px 2px 0'>User name</td><td><b>{esc(u['username'])}</b></td></tr>"
                f"<tr><td style='padding:2px 12px 2px 0'>Temporary password</td><td style='font-family:Consolas,monospace;font-size:15px'><b>{esc(temp)}</b></td></tr></table>"
                f"<p style='font-family:Arial;font-size:13px'>Sign in{(' at <a href=\"' + esc(url) + '\">' + esc(url) + '</a>') if url else ''} with this password; "
                "you will be asked to choose a new one straight away.</p>"
                "<p style='font-family:Arial;font-size:11px;color:#777'>If you did not expect this, tell the portal administrator.</p>")
        try:
            mailer.send(u["email"], "ITAM Portal - your temporary password", text, body, cfg=cfg)
        except mailer.MailError as e:
            raise mailer.MailError(f"The e-mail could not be sent, so the password was not changed. {e}") from e
    return {"username": u["username"], "emailed_to": u["email"]}


RESET_VALID_MIN, RESET_COOLDOWN_MIN = 60, 5
FORGOT_MAX, FORGOT_WINDOW_S = 10, 600
_forgot, _forgot_lock = {}, threading.Lock()


def _close_reset_requests(con, user_id, by):
    con.execute("UPDATE portal_reset_request SET handled_at = now(), handled_by = %s WHERE user_id = %s AND handled_at IS NULL", (by, user_id))


def reset_requests():
    """Forgotten-password requests that could not be e-mailed (no address on the account, e-mail off, or the mail server refused), oldest first."""
    return db.query("""SELECT r.request_id, r.username, u.display_name, r.requested_at, r.reason, (u.email IS NOT NULL) AS has_email
                       FROM portal_reset_request r JOIN portal_user u USING (user_id) WHERE r.handled_at IS NULL AND u.active ORDER BY r.requested_at""")


def request_password_reset(username, ip):
    """Forgotten password, asked for from the sign-in page. -> "emailed" | "queued" | "ignored" - for the log and tests only: the caller shows
    the same words for all three, so the page never says whether a user name exists.
      emailed: a new temporary password (valid RESET_VALID_MIN minutes) went to the account's e-mail address. The current password is
               untouched until the temporary one is used at sign-in.
      queued:  no usable address - an administrator sees the request in Users and security and issues the password."""
    from . import mailer
    now = time.monotonic()
    with _forgot_lock:
        q = [t for t in _forgot.get(ip or "", []) if now - t < FORGOT_WINDOW_S]
        if len(q) >= FORGOT_MAX:
            _forgot[ip or ""] = q
            raise AuthError("Too many reset requests from this computer. Try again in a few minutes.", 429, code="throttled")
        _forgot[ip or ""] = [*q, now]
    uname = re.sub(r"\s+", "", (username or "")).upper()
    u = db.one("SELECT * FROM portal_user WHERE username = %s", [uname]) if uname else None
    if not u or not u["active"] or u["read_only"]:
        log("(UNKNOWN USER)" if not u else uname, ip, "PASSWORD_RESET_REQUESTED", detail={"result": "ignored"}, ok=False)
        return "ignored"
    if u["reset_requested_at"] and (_now() - u["reset_requested_at"]).total_seconds() < RESET_COOLDOWN_MIN * 60:
        return "ignored"                                  # asked again within minutes: the first answer (mail or queue entry) is still good
    with db.write() as con:
        con.execute("UPDATE portal_user SET reset_requested_at = now() WHERE user_id = %s", (u["user_id"],))
    reason = "no e-mail address on the account"
    if u["email"]:
        cfg = mailer.get_smtp()
        if not cfg["enabled"]:
            reason = "e-mail is switched off"
        else:
            temp = generate_temp_password()
            url = cfg["portal_url"]
            text = (f"Hello {u['display_name']},\n\nSomeone (hopefully you) asked to reset the ITAM Portal password for {u['username']}.\n\n"
                    f"Temporary password: {temp}\n\n"
                    f"Sign in{(' at ' + url) if url else ''} with it within {RESET_VALID_MIN} minutes; you will be asked to choose a new password straight away.\n"
                    "Your current password still works until then. If you did not ask for this, ignore this message - nothing has changed.\n")
            esc = mailer.html.escape
            link = (' at <a href="' + esc(url) + '">' + esc(url) + '</a>') if url else ''
            body = (f"<p style='font-family:Arial;font-size:13px'>Hello {esc(u['display_name'])},</p>"
                    f"<p style='font-family:Arial;font-size:13px'>Someone (hopefully you) asked to reset the ITAM Portal password for <b>{esc(u['username'])}</b>.</p>"
                    f"<p style='font-family:Arial;font-size:13px'>Temporary password: <span style='font-family:Consolas,monospace;font-size:15px'><b>{esc(temp)}</b></span></p>"
                    f"<p style='font-family:Arial;font-size:13px'>Sign in{link} with it within {RESET_VALID_MIN} minutes; "
                    "you will be asked to choose a new password straight away. Your current password still works until then.</p>"
                    "<p style='font-family:Arial;font-size:11px;color:#777'>If you did not ask for this, ignore this message - nothing has changed.</p>")
            try:
                mailer.send(u["email"], "ITAM Portal - your temporary password", text, body, cfg=cfg)
            except mailer.MailError as e:
                reason = f"the e-mail could not be sent ({str(e)[:120]})"
            else:
                with db.write() as con:
                    con.execute("UPDATE portal_user SET reset_hash = %s, reset_expires_at = now() + make_interval(mins => %s) WHERE user_id = %s",
                                (hash_password(temp), RESET_VALID_MIN, u["user_id"]))
                log(u["username"], ip, "PASSWORD_RESET_REQUESTED", detail={"result": "emailed"})
                return "emailed"
    with db.write() as con:
        if not con.execute("SELECT 1 FROM portal_reset_request WHERE user_id = %s AND handled_at IS NULL", (u["user_id"],)).fetchone():
            con.execute("INSERT INTO portal_reset_request (user_id, username, ip, reason) VALUES (%s,%s,%s,%s)", (u["user_id"], u["username"], ip, reason))
    log(u["username"], ip, "PASSWORD_RESET_REQUESTED", detail={"result": "queued", "reason": reason})
    return "queued"


def recover_account(username, ip="console", password=None):
    """Break-glass, for whoever has shell access to the server (see db/reset_admin.py): make the account usable again and give it a new
    temporary password - active, unlocked, failed-attempt counter cleared, signed out everywhere, must choose a new password at once.
    Deliberately does not touch the group (role) or two-factor settings. `password`: set this exact starting password instead of a random one
    (it skips the password policy on purpose - the account is still forced to choose a compliant one at its next sign-in).
    -> {"username", "temporary_password"}"""
    uname = re.sub(r"\s+", "", (username or "")).upper()
    with db.write() as con:
        u = _row(con, "SELECT user_id FROM portal_user WHERE username = %s FOR UPDATE", (uname,))
        if not u:
            raise AuthError(f"No account named {uname}.", 404)
        con.execute("UPDATE portal_user SET active = TRUE WHERE user_id = %s", (u["user_id"],))
    if password:
        with db.write() as con:
            con.execute("UPDATE portal_user SET password_hash = %s, must_change = TRUE, password_changed_at = now(), failed_attempts = 0, locked_until = NULL WHERE user_id = %s",
                        (hash_password(password), u["user_id"]))
            con.execute("DELETE FROM portal_session WHERE user_id = %s", (u["user_id"],))
        out = {"username": uname, "temporary_password": password}
    else:
        out = reset_password(u["user_id"], "console recovery")
    log("(CONSOLE)", ip, "ACCOUNT_RECOVERY", uname)
    return out


# ---------------------------------------------------------------- sign in / sessions
_DUMMY = hash_password("not-a-real-password")


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# Failed sign-ins per client address, newest last. The per-account lock-out below stops guessing one account; this stops one computer
# trying the default password against every user name (the roster accounts share a known starting password). In memory on purpose:
# one server process, and a restart only forgets a few minutes of history. A successful sign-in does NOT clear it, so an attacker
# holding one valid account cannot reset their own count.
IP_FAIL_MAX, IP_FAIL_WINDOW_S = 15, 600
_ip_fails, _ip_lock = {}, threading.Lock()


def reset_ip_throttle():
    with _ip_lock:
        _ip_fails.clear()


def _ip_throttle_check(ip):
    now = time.monotonic()
    with _ip_lock:
        q = [t for t in _ip_fails.get(ip, []) if now - t < IP_FAIL_WINDOW_S]
        _ip_fails[ip] = q
        if len(q) >= IP_FAIL_MAX:
            wait = int(IP_FAIL_WINDOW_S - (now - q[0])) // 60 + 1
            raise AuthError(f"Too many failed sign-ins from this computer. Try again in about {wait} minute(s).", 429, code="throttled")


def _ip_throttle_fail(ip):
    with _ip_lock:
        _ip_fails.setdefault(ip, []).append(time.monotonic())


def _temp_password_ok(u, password):
    return bool(u["reset_hash"] and u["reset_expires_at"] and u["reset_expires_at"] > _now() and verify_password(password or "", u["reset_hash"]))


def login(username, password, ip, user_agent):
    """-> (token, state). Raises AuthError. The failure message never says whether the user exists."""
    generic = AuthError("Sign-in failed. Check your user name and password.", 401, code="bad_credentials")
    _ip_throttle_check(ip or "")
    s = settings()
    uname = re.sub(r"\s+", "", (username or "")).upper()
    expired = None
    with db.write() as con:
        u = _row(con, "SELECT * FROM portal_user WHERE username = %s FOR UPDATE", (uname,))
        if not u or not u["active"]:
            verify_password(password or "", _DUMMY)
            failure = ("(UNKNOWN USER)" if not u else uname, 0, False)      # never write down what was typed for an unknown name: it may be a password
        elif _temp_password_ok(u, password):
            con.execute("INSERT INTO portal_password_history (user_id, password_hash) VALUES (%s,%s)", (u["user_id"], u["password_hash"]))
            con.execute("""UPDATE portal_user SET password_hash = reset_hash, must_change = TRUE, password_changed_at = now(), failed_attempts = 0, locked_until = NULL,
                           reset_hash = NULL, reset_expires_at = NULL WHERE user_id = %s""", (u["user_id"],))
            con.execute("DELETE FROM portal_session WHERE user_id = %s", (u["user_id"],))
            failure = None
        elif u["locked_until"] and u["locked_until"] > _now():
            mins = int((u["locked_until"] - _now()).total_seconds() // 60) + 1
            raise AuthError(f"This account is locked for about {mins} more minute(s) after repeated failed sign-ins. An administrator can unlock it.", 423, code="locked")
        elif not verify_password(password or "", u["password_hash"]):
            n = u["failed_attempts"] + 1
            lock = _now() + dt.timedelta(minutes=s["lockout_minutes"]) if n >= s["lockout_attempts"] else None
            con.execute("UPDATE portal_user SET failed_attempts = %s, locked_until = %s WHERE user_id = %s", (0 if lock else n, lock, u["user_id"]))
            failure = (uname, n, bool(lock))
        elif _starting_pw_expired(u, s):
            failure, expired = None, u["username"]
        else:
            failure = None
    if expired:                                        # the password was right, so saying why is not a leak; the person needs an administrator
        log(expired, ip, "LOGIN_REFUSED", detail={"reason": "starting password expired"}, ok=False)
        raise AuthError("Your starting password has expired because it was not used in time. Ask an administrator to reset it.", 403, code="starting_password_expired")
    if failure:
        _ip_throttle_fail(ip or "")
        log(failure[0], ip, "LOGIN_FAILED", detail={"attempt": failure[1], "locked": failure[2]}, ok=False)
        raise generic
    with db.write() as con:                            # the right password was typed: an unused e-mailed one is no longer needed, and a waiting request is moot
        con.execute("UPDATE portal_user SET reset_hash = NULL, reset_expires_at = NULL WHERE user_id = %s AND reset_hash IS NOT NULL AND NOT must_change", (u["user_id"],))
        _close_reset_requests(con, u["user_id"], "(signed in)")
    token = secrets.token_urlsafe(32)
    stage = "PENDING_2FA" if u["totp_enabled"] else "ACTIVE"
    life = dt.timedelta(minutes=5) if stage == "PENDING_2FA" else dt.timedelta(hours=s["session_max_hours"])
    with db.write() as con:
        con.execute("INSERT INTO portal_session (token_hash, user_id, stage, expires_at, ip, user_agent) VALUES (%s,%s,%s,%s,%s,%s)",
                    (_token_hash(token), u["user_id"], stage, _now() + life, ip, (user_agent or "")[:200]))
        if stage == "ACTIVE":
            con.execute("UPDATE portal_user SET failed_attempts = 0, locked_until = NULL, last_login_at = now() WHERE user_id = %s", (u["user_id"],))
        con.execute("DELETE FROM portal_session WHERE expires_at < now() OR last_active_at < now() - make_interval(mins => %s)", (s["idle_timeout_min"],))
    if stage == "ACTIVE":
        log(u["username"], ip, "LOGIN", detail={"role": u["role"]})
    return token


def verify_second_factor(token, code, ip):
    """Complete a pending sign-in with an authenticator code or a recovery code."""
    th = _token_hash(token or "")
    with db.write() as con:
        row = _row(con, "SELECT s.*, u.username, u.totp_secret, u.totp_last_step, u.recovery_hashes, u.user_id AS uid FROM portal_session s JOIN portal_user u USING (user_id) WHERE s.token_hash = %s AND s.stage = 'PENDING_2FA' AND s.expires_at > now() FOR UPDATE OF u, s", (th,))
        if not row:
            raise AuthError("Sign-in expired. Start again.", 401, code="expired")
        ok = False
        step = totp_match(row["totp_secret"], code, row["totp_last_step"])
        if step:
            con.execute("UPDATE portal_user SET totp_last_step = %s WHERE user_id = %s", (step, row["uid"]))
            ok = True
        else:
            h = hashlib.sha256(re.sub(r"\s+", "", code or "").upper().encode()).hexdigest()
            rec = list(row["recovery_hashes"] or [])
            if h in rec:
                rec.remove(h)
                con.execute("UPDATE portal_user SET recovery_hashes = %s::jsonb WHERE user_id = %s", (json.dumps(rec), row["uid"]))
                ok = True
        if ok:
            s = settings()
            con.execute("UPDATE portal_session SET stage = 'ACTIVE', last_active_at = now(), expires_at = now() + make_interval(hours => %s) WHERE token_hash = %s", (s["session_max_hours"], th))
            con.execute("UPDATE portal_user SET failed_attempts = 0, locked_until = NULL, last_login_at = now() WHERE user_id = %s", (row["uid"],))
    if not ok:
        log(row["username"], ip, "LOGIN_2FA_FAILED", ok=False)
        s = settings()
        with db.write() as con:                                    # a wrong code counts like a wrong password (stops guessing the 6 digits)
            con.execute("UPDATE portal_user SET failed_attempts = failed_attempts + 1 WHERE user_id = %s", (row["uid"],))
            con.execute("UPDATE portal_user SET locked_until = now() + make_interval(mins => %s), failed_attempts = 0 WHERE user_id = %s AND failed_attempts >= %s", (s["lockout_minutes"], row["uid"], s["lockout_attempts"]))
            con.execute("DELETE FROM portal_session WHERE token_hash = %s AND EXISTS (SELECT 1 FROM portal_user WHERE user_id = %s AND locked_until > now())", (th, row["uid"]))
        raise AuthError("That code is not valid. Enter the current 6-digit code or a recovery code.", 401, code="bad_code")
    log(row["username"], ip, "LOGIN", detail={"second_factor": True})


def session_user(token):
    """-> user dict with 'stage' and 'state', or None. Read-only; does not extend the session."""
    if not token:
        return None
    s = settings()
    r = db.one("""SELECT s.stage, s.last_active_at, s.expires_at, u.*,
                         (SELECT e.designation FROM cipl_employee e WHERE e.ecode = u.username AND e.employment_status = 'ACTIVE' AND e.is_on_roster = 1 LIMIT 1) AS designation
                  FROM portal_session s JOIN portal_user u USING (user_id)
                  WHERE s.token_hash = %s AND u.active""", [_token_hash(token)])
    if not r:
        return None
    now = _now()
    if r["expires_at"] < now or (r["stage"] == "ACTIVE" and (now - r["last_active_at"]).total_seconds() > s["idle_timeout_min"] * 60):
        return None
    r["idle_left"] = None if r["stage"] != "ACTIVE" else max(0, int(s["idle_timeout_min"] * 60 - (now - r["last_active_at"]).total_seconds()))
    if r["stage"] == "PENDING_2FA":
        r["state"] = "2fa"
    elif r["read_only"]:
        r["state"] = "ok"          # a read-only account (e.g. the demo login) never faces a forced password change or 2FA prompt
    elif r["must_change"] or _password_expired(r, s):
        r["state"] = "change_password"
    elif s["require_2fa_admin"] and r["role"] == "ADMIN" and not r["totp_enabled"]:
        r["state"] = "setup_2fa"
    else:
        r["state"] = "ok"
    # From here on "role" is the effective one, used by every permission check; "group" is the real one (shown to people, and what
    # require_admin(strict=True) looks at). An extended-access User therefore passes every ordinary administrator check.
    r["group"] = r["role"]
    # lead_tools: a plain User (no extended access) whose roster designation is Team Leader/SI gets PM cycles, Inventory match, Users and Activity log
    r["lead_tools"] = bool(r["role"] == "USER" and not r.get("extended_access") and not r["read_only"] and is_lead_designation(r.get("designation")))
    if r["role"] == "USER" and r.get("extended_access"):
        r["role"] = "ADMIN"
    return r


def _starting_pw_expired(u, s):
    """Still on the starting password (never changed, or reset by an administrator) for longer than the allowed days. Read-only demo accounts are exempt."""
    days = s.get("starting_pw_days") or 0
    return bool(days and u.get("must_change") and not u.get("read_only") and u.get("password_changed_at") and (_now() - u["password_changed_at"]).days >= days)


def _password_expired(u, s):
    return bool(s["pw_max_age_days"]) and (_now() - u["password_changed_at"]).days >= s["pw_max_age_days"]


def touch(token):
    """User activity: extends the idle timer (called by the browser's keep-alive and by every write)."""
    if not token:
        return
    with db.write() as con:
        con.execute("UPDATE portal_session SET last_active_at = now() WHERE token_hash = %s AND stage = 'ACTIVE'", (_token_hash(token),))


def logout(token, username, ip):
    if token:
        with db.write() as con:
            con.execute("DELETE FROM portal_session WHERE token_hash = %s", (_token_hash(token),))
    if username:
        log(username, ip, "LOGOUT")


def logout_others(user, token):
    """End every other session of this account (another PC, a forgotten browser), keeping the one making the request. -> how many were ended."""
    with db.write() as con:
        cur = con.execute("DELETE FROM portal_session WHERE user_id = %s AND token_hash <> %s", (user["user_id"], _token_hash(token or "")))
        return cur.rowcount


def change_password(user, current, new, token):
    with db.write() as con:
        u = _row(con, "SELECT * FROM portal_user WHERE user_id = %s FOR UPDATE", (user["user_id"],))
        if not verify_password(current or "", u["password_hash"]):
            raise AuthError("Your current password is not correct.", 400, fields={"current": "not correct"})
        s = settings()
        hist = [r["password_hash"] for r in _rows(con, "SELECT password_hash FROM portal_password_history WHERE user_id = %s ORDER BY at DESC LIMIT %s", (u["user_id"], s["pw_history"]))]
        problems = policy_problems(new or "", u["username"], [u["password_hash"], *hist])
        if problems:
            raise AuthError("The new password does not meet the policy: " + "; ".join(problems) + ".", 400, fields={"new": "; ".join(problems)})
        con.execute("INSERT INTO portal_password_history (user_id, password_hash) VALUES (%s,%s)", (u["user_id"], u["password_hash"]))
        con.execute("UPDATE portal_user SET password_hash = %s, must_change = FALSE, password_changed_at = now(), reset_hash = NULL, reset_expires_at = NULL WHERE user_id = %s", (hash_password(new), u["user_id"]))
        con.execute("DELETE FROM portal_session WHERE user_id = %s AND token_hash <> %s", (u["user_id"], _token_hash(token)))


# ---------------------------------------------------------------- two-factor enrolment
def totp_begin(user):
    secret = totp_new_secret()
    with db.write() as con:
        con.execute("UPDATE portal_user SET totp_secret = %s, totp_enabled = FALSE WHERE user_id = %s", (secret, user["user_id"]))
    uri = totp_uri(secret, user["username"])
    return {"secret": secret, "uri": uri, "qr_svg": qr_svg(uri)}


def totp_enable(user, code):
    with db.write() as con:
        u = _row(con, "SELECT * FROM portal_user WHERE user_id = %s FOR UPDATE", (user["user_id"],))
        if not u["totp_secret"]:
            raise AuthError("Start two-factor set-up first.", 400)
        step = totp_match(u["totp_secret"], code)
        if not step:
            raise AuthError("That code is not valid. Check the time on your phone and try the next code.", 400, fields={"code": "not valid"})
        codes, hashes = new_recovery_codes()
        con.execute("UPDATE portal_user SET totp_enabled = TRUE, totp_last_step = %s, recovery_hashes = %s::jsonb WHERE user_id = %s", (step, json.dumps(hashes), user["user_id"]))
    return {"recovery_codes": codes}


def totp_disable(user, password, code):
    with db.write() as con:
        u = _row(con, "SELECT * FROM portal_user WHERE user_id = %s FOR UPDATE", (user["user_id"],))
        if not verify_password(password or "", u["password_hash"]) or not totp_match(u["totp_secret"] or "", code, u["totp_last_step"]):
            raise AuthError("Password or code is not correct.", 400)
        if u["role"] == "ADMIN" and settings()["require_2fa_admin"]:
            raise AuthError("Two-factor sign-in is required for administrators.", 409)
        con.execute("UPDATE portal_user SET totp_enabled = FALSE, totp_secret = NULL, recovery_hashes = NULL, totp_last_step = NULL WHERE user_id = %s", (user["user_id"],))


# ---------------------------------------------------------------- permissions
# A User may change any asset field on an asset assigned to them (2026-10-02) except the Contract and Lifecycle
# groups (edit.py's SPEC field groups) - those stay administrator-only for every User, no per-person toggle. Kept
# as an explicit set here rather than imported from edit.py to avoid a circular import (edit.py already imports
# auth.py for its own field validation). edit.py's own _clean_all() already rejects any field name not in the
# dataset's real field list, and create_only fields (asset_key/class/type) on update regardless of caller, so this
# only needs to name what's locked, not enumerate everything that's allowed.
ASSET_LOCKED_FIELDS = {
    "cover_type", "cover_expiry_date", "rate_component", "rate_value",            # Contract
    "purchase_date", "purchase_cost", "vendor_name", "po_no", "refresh_due_date",  # Lifecycle
}


def is_admin(user):
    return user["role"] == "ADMIN"


def check_edit(user, dataset, fields, key=None):
    """`key` is the record being changed - the asset key or the engineer's register key (engineer_key), when known."""
    if is_admin(user):
        return
    if dataset in ADMIN_ONLY_DATASETS:
        if user.get("call_parts_access") == "FULL":
            return
        raise AuthError("This register is for administrators only.", 403, code="forbidden")
    if dataset == "assets":
        if user.get("asset_access") == "FULL":
            return
        bad = sorted(set(fields) & ASSET_LOCKED_FIELDS)
        if bad:
            raise AuthError("Only administrators can change: " + ", ".join(bad) + " (contract and lifecycle details).", 403, code="forbidden")
        if key is not None:
            row = db.one("SELECT engineer_name FROM asset WHERE asset_key = %s AND is_current = 1", [key])
            if not row or row["engineer_name"] != user.get("engineer_key"):
                raise AuthError("You can only change assets assigned to you.", 403, code="forbidden")
    elif dataset == "engineers":
        # a non-admin may change every field on their engineer record - but only their own; someone else's is always refused
        if key is not None and user.get("engineer_key") != key:
            raise AuthError("You can only change your own engineer record.", 403, code="forbidden")


def check_verify(user, key):
    """Physically checking an asset: an administrator any asset, an engineer only one assigned to them (or any asset, with asset_access=FULL)."""
    if is_admin(user) or user.get("asset_access") == "FULL":
        return
    row = db.one("SELECT engineer_name FROM asset WHERE asset_key = %s AND is_current = 1", [key])
    if not row or row["engineer_name"] != user.get("engineer_key"):
        raise AuthError("You can only verify assets assigned to you.", 403, code="forbidden")


def check_create(user, dataset):
    if dataset == "engineers":
        if not is_admin(user):
            raise AuthError("Only administrators can add a new engineer.", 403, code="forbidden")
        return
    if dataset in ADMIN_ONLY_DATASETS and not is_admin(user):
        if user.get("call_parts_access") == "FULL":
            return
        raise AuthError("This register is for administrators only.", 403, code="forbidden")
    if dataset == "assets" and not is_admin(user) and user.get("asset_access") != "FULL":
        raise AuthError("Only administrators can add a new asset.", 403, code="forbidden")


def check_archive(user, dataset):
    if dataset == "engineers":
        raise AuthError("Engineer records cannot be archived individually - manage roster membership through roster sync or a roster event.", 403, code="forbidden")
    if dataset in ADMIN_ONLY_DATASETS and not is_admin(user):
        if user.get("call_parts_access") == "FULL":
            return
        raise AuthError("This register is for administrators only.", 403, code="forbidden")
    if dataset == "assets" and not is_admin(user) and user.get("asset_access") != "FULL":
        raise AuthError("Only administrators can archive an asset.", 403, code="forbidden")


def is_full_admin(user):
    """A real administrator - not a User with extended access. Guards Control, Data tools (import, backup, software update) and Administration."""
    return user["role"] == "ADMIN" and user.get("group", user["role"]) == "ADMIN"


def is_lead(user):
    """A plain User (no extended access) whose designation is Team Leader/SI - see session_user(). Gets PM cycles, Inventory match, Users and Activity log."""
    return bool(user.get("lead_tools"))


def require_admin(user, strict=False, lead=False):
    """lead=True: a Team Leader/SI User passes too (admin="lead" / "lead_strict" in web.py)."""
    if not (is_full_admin(user) if strict else is_admin(user)) and not (lead and is_lead(user)):
        raise AuthError("This needs an administrator.", 403, code="forbidden")


# ---------------------------------------------------------------- first run
DEFAULT_ADMIN_PASSWORD = "*ongc123"    # fixed default so the first sign-in is known in advance; must_change forces a real password right after


def bootstrap_admin(con):
    """Create the first administrator when there are no users. Returns (username, default_password) or None."""
    row = con.execute("SELECT count(*) AS n FROM portal_user").fetchone()
    if (row["n"] if isinstance(row, dict) else row[0]):
        return None
    con.execute("INSERT INTO portal_user (username, display_name, role, password_hash, must_change, created_by) VALUES ('ADMIN','ADMINISTRATOR','ADMIN',%s,TRUE,'setup')", (hash_password(DEFAULT_ADMIN_PASSWORD),))
    return "ADMIN", DEFAULT_ADMIN_PASSWORD


DEMO_USERNAME = "DEMOUSER"
DEMO_PASSWORD = "demouser"    # fixed, never expires, never forced to change - a permanent read-only walkthrough account


def ensure_demo_user(con):
    """Create the standing read-only demo account if it does not already exist yet (every setup.py run, not just the first).
    Set PORTAL_DEMO_USER=off on a production server: the demo login has a public password and administrator-level read access, so it is
    then never created and, if it exists, is deactivated every time setup.py runs."""
    if os.environ.get("PORTAL_DEMO_USER", "").strip().lower() in ("0", "off", "no", "false"):
        con.execute("UPDATE portal_user SET active = FALSE WHERE username = %s AND read_only", (DEMO_USERNAME,))
        return
    con.execute("""INSERT INTO portal_user (username, display_name, role, password_hash, must_change, created_by, read_only)
                   VALUES (%s,'DEMO USER','ADMIN',%s,FALSE,'setup',TRUE) ON CONFLICT (username) DO NOTHING""",
                (DEMO_USERNAME, hash_password(DEMO_PASSWORD)))
