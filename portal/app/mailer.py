"""E-mail: server settings, sending, and the automatic notification rules.

Nothing is sent until an administrator switches e-mail on and enables individual rules. Every message sent (or failed) is recorded in notify_log,
which is also what stops the same event being mailed twice. The SMTP password is kept in a file outside the project and outside the database
(~/.itam_smtp_secret); it is never returned to the browser.
"""
import datetime as dt
import html
import json
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from . import db, pm

SECRET_FILE = Path(os.environ.get("ITAM_SMTP_SECRET") or Path.home() / ".itam_smtp_secret")
DEFAULT_SMTP = {"enabled": False, "host": "", "port": 25, "security": "none", "user": "", "from_addr": "", "from_name": "ITAM PORTAL", "reply_to": "", "portal_url": ""}
RULE_INTERVAL_S = 900

DDL = [
    "CREATE TABLE IF NOT EXISTS notify_rule (rule_key TEXT PRIMARY KEY, enabled BOOLEAN NOT NULL DEFAULT FALSE, extra_to TEXT, params JSONB NOT NULL DEFAULT '{}'::jsonb, updated_at TIMESTAMPTZ DEFAULT now(), updated_by TEXT)",
    """CREATE TABLE IF NOT EXISTS notify_log (
         log_id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now(), rule_key TEXT NOT NULL, event_key TEXT NOT NULL, recipient TEXT, subject TEXT,
         status TEXT NOT NULL CHECK (status IN ('SENT','FAILED','SKIPPED')), error TEXT, attempts INT NOT NULL DEFAULT 1, UNIQUE (rule_key, event_key, recipient))""",
]

RULES = {
    "PM_KICKOFF": ("PM kick-off", f"{pm.KICKOFF_DAYS} days after a quarter starts, every engineer gets the list of assets still needing preventive maintenance.", {}),
    "PM_REMINDER": ("PM weekly reminder", "Every Monday after the kick-off until the quarter ends: engineers with pending PM get the list.", {}),
    "PM_CLOSE_QUARTER": ("PM quarter needs closing", "Once a quarter has ended and nobody has rolled it over yet, every administrator (Team Leader / Site In-charge) is "
                         "reminded daily until it is closed from PM cycles. Never closes it automatically - this is a reminder only.", {}),
    "ASSET_ADDED": ("New asset assigned", "An asset is added (import or portal) and assigned to an engineer.", {"lookback_days": 7}),
    "ASSET_REMOVED": ("Asset removed", "An asset assigned to an engineer is removed from the register.", {"lookback_days": 7}),
    "AMC_EXPIRED": ("Cover expired", "The AMC / warranty of an assigned asset has expired.", {"lookback_days": 7}),
    "AMC_EXPIRING": ("Cover expiring soon", "Weekly digest of assigned assets whose cover ends within the next days.", {"within_days": 60}),
    "CALL_ASSIGNED": ("Call assigned", "A new service call is logged against an engineer.", {"lookback_days": 3}),
    "CALL_OVERDUE": ("Overdue open calls", "Weekly digest of an engineer's open calls older than the limit.", {"older_than_days": 7}),
    "PART_RECEIVED": ("Spare part received", "A part arrived for an engineer's call.", {"lookback_days": 3}),
}


class MailError(Exception):
    http_error = True


# ---------------------------------------------------------------- settings
def get_smtp():
    row = db.one("SELECT value FROM portal_setting WHERE key = 'smtp'")
    cfg = {**DEFAULT_SMTP, **(row["value"] if row else {})}
    cfg["has_password"] = bool(_read_secret())
    return cfg


def _read_secret():
    try:
        return SECRET_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def save_smtp(cfg, password, editor):
    out = {}
    for k, default in DEFAULT_SMTP.items():
        v = cfg.get(k, default)
        if k == "enabled":
            out[k] = bool(v)
        elif k == "port":
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                raise MailError("Port must be a number.") from None
            if not 1 <= out[k] <= 65535:
                raise MailError("Port must be between 1 and 65535.")
        else:
            out[k] = str(v or "").strip()
    if out["security"] not in ("none", "starttls", "ssl"):
        raise MailError("Security must be none, STARTTLS or SSL.")
    if out["from_addr"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", out["from_addr"]):
        raise MailError("The sender address is not a valid e-mail address.")
    if out["enabled"] and not (out["host"] and out["from_addr"]):
        raise MailError("Enter the mail server and the sender address before turning e-mail on.")
    with db.write() as con:
        con.execute("INSERT INTO portal_setting (key, value, updated_by) VALUES ('smtp', %s::jsonb, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by",
                    (json.dumps(out), editor))
    if password:
        SECRET_FILE.write_text(password, encoding="utf-8")
    return get_smtp()


# ---------------------------------------------------------------- sending
def send(to, subject, text, html_body=None, attachments=(), cfg=None):
    cfg = cfg or get_smtp()
    to = [a.strip() for a in (to if isinstance(to, (list, tuple)) else [to]) if a and a.strip()]
    if not to:
        raise MailError("There is no recipient e-mail address.")
    if not cfg["host"] or not cfg["from_addr"]:
        raise MailError("E-mail is not set up yet (Administration > E-mail).")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{cfg['from_name']} <{cfg['from_addr']}>" if cfg["from_name"] else cfg["from_addr"]
    msg["To"] = ", ".join(to)
    if cfg["reply_to"]:
        msg["Reply-To"] = cfg["reply_to"]
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    for name, data, mime in attachments:
        main, _, sub = mime.partition("/")
        msg.add_attachment(data, maintype=main, subtype=sub.split(";")[0], filename=name)
    try:
        if cfg["security"] == "ssl":
            srv = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=20, context=ssl.create_default_context())
        else:
            srv = smtplib.SMTP(cfg["host"], cfg["port"], timeout=20)
        with srv:
            if cfg["security"] == "starttls":
                srv.starttls(context=ssl.create_default_context())
            if cfg["user"]:
                srv.login(cfg["user"], _read_secret() or "")
            srv.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"The mail server refused or could not be reached: {e}") from e


def page(title, intro, rows=None, columns=None, footer=""):
    """Simple, client-safe HTML e-mail body + plain-text twin."""
    esc = html.escape
    body = f"<h2 style='font-family:Arial;color:#1F3864'>{esc(title)}</h2><p style='font-family:Arial;font-size:13px'>{esc(intro)}</p>"
    text = f"{title}\n\n{intro}\n"
    if rows:
        head = "".join(f"<th style='text-align:left;padding:4px 8px;background:#1F3864;color:#fff;font-size:12px'>{esc(l)}</th>" for _, l in columns)
        trs = "".join("<tr>" + "".join(f"<td style='padding:4px 8px;border-bottom:1px solid #ddd;font-size:12px'>{esc(str(r.get(k) if r.get(k) is not None else ''))}</td>" for k, _ in columns) + "</tr>" for r in rows[:200])
        body += f"<table style='border-collapse:collapse;font-family:Arial'><tr>{head}</tr>{trs}</table>"
        text += "\n" + "\n".join(" | ".join(str(r.get(k) if r.get(k) is not None else "") for k, _ in columns) for r in rows[:200])
        if len(rows) > 200:
            body += f"<p style='font-size:12px'>... and {len(rows) - 200} more.</p>"
            text += f"\n... and {len(rows) - 200} more."
    body += f"<p style='font-family:Arial;font-size:11px;color:#777'>{esc(footer)}</p>"
    return body, text + f"\n\n{footer}"


# ---------------------------------------------------------------- recipients
def engineer_email(key):
    r = db.one("""SELECT coalesce(nullif(c.company_email, ''), (SELECT email FROM portal_user u WHERE u.engineer_key = e.engineer_key AND u.active AND u.email IS NOT NULL LIMIT 1)) AS email
                  FROM portal_engineer e LEFT JOIN cipl_employee c ON c.ecode = e.ecode WHERE e.engineer_key = %s AND coalesce(c.employment_status, 'ACTIVE') = 'ACTIVE'""", [key])
    return r["email"] if r else None


def rules_state():
    saved = {r["rule_key"]: r for r in db.query("SELECT * FROM notify_rule")}
    out = []
    for key, (label, desc, params) in RULES.items():
        s = saved.get(key)
        out.append({"key": key, "label": label, "description": desc, "enabled": bool(s and s["enabled"]), "extra_to": s["extra_to"] if s else "", "params": {**params, **((s or {}).get("params") or {})},
                    "default_params": params})
    return out


def save_rule(key, enabled, extra_to, params, editor):
    if key not in RULES:
        raise MailError("Unknown rule.")
    extra = ", ".join(a.strip() for a in re.split(r"[;,\s]+", extra_to or "") if a.strip())
    for a in filter(None, extra.split(", ")):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", a):
            raise MailError(f"'{a}' is not a valid e-mail address.")
    clean = {}
    for k, default in RULES[key][2].items():
        try:
            clean[k] = max(1, min(365, int((params or {}).get(k, default))))
        except (TypeError, ValueError):
            raise MailError(f"{k}: enter a whole number of days.") from None
    with db.write() as con:
        con.execute("""INSERT INTO notify_rule (rule_key, enabled, extra_to, params, updated_by) VALUES (%s,%s,%s,%s::jsonb,%s)
                       ON CONFLICT (rule_key) DO UPDATE SET enabled = EXCLUDED.enabled, extra_to = EXCLUDED.extra_to, params = EXCLUDED.params, updated_at = now(), updated_by = EXCLUDED.updated_by""",
                    (key, bool(enabled), extra, json.dumps(clean), editor))


# ---------------------------------------------------------------- event detection: each returns {engineer_key: {"event_key", "subject", "intro", "rows", "columns"}}
def _lookback(p):
    return dt.date.today() - dt.timedelta(days=int(p.get("lookback_days", 7)))


def _iso_week(d=None):
    d = d or dt.date.today()
    return f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"


def _group(rows, key_field):
    out = {}
    for r in rows:
        if r.get(key_field):
            out.setdefault(r[key_field], []).append(r)
    return out


def detect(rule_key, params, today=None):
    today = today or dt.date.today()
    q = pm.quarter(today)
    D = {}
    if rule_key == "PM_KICKOFF":
        if today < q["kickoff"]:
            return D
        rows = db.query(f"SELECT asset_key, asset_class, trim(coalesce(make,'') || ' ' || coalesce(model,'')) AS model, location_code, engineer_name FROM asset WHERE {pm.IN_SCOPE} AND pm_status = 'PENDING' ORDER BY asset_key")
        for eng, items in _group(rows, "engineer_name").items():
            D[eng] = dict(event_key=f"{q['label']}", subject=f"PM kick-off {q['label']}: {len(items)} assets pending", intro=f"Preventive maintenance for {q['label']} is now due. You have {len(items)} asset(s) still pending; the quarter ends on {q['end']:%d %b %Y}.",
                          rows=items, columns=[("asset_key", "Asset"), ("asset_class", "Class"), ("model", "Model"), ("location_code", "Location")])
    elif rule_key == "PM_REMINDER":
        if today <= q["kickoff"] or today.weekday() != 0:
            return D
        rows = db.query(f"SELECT asset_key, asset_class, trim(coalesce(make,'') || ' ' || coalesce(model,'')) AS model, location_code, engineer_name FROM asset WHERE {pm.IN_SCOPE} AND pm_status = 'PENDING' ORDER BY asset_key")
        for eng, items in _group(rows, "engineer_name").items():
            D[eng] = dict(event_key=f"{q['label']}:{_iso_week(today)}", subject=f"PM reminder: {len(items)} assets pending, {(q['end'] - today).days} days left",
                          intro=f"{len(items)} asset(s) still need preventive maintenance before {q['end']:%d %b %Y}.", rows=items,
                          columns=[("asset_key", "Asset"), ("asset_class", "Class"), ("model", "Model"), ("location_code", "Location")])
    elif rule_key == "PM_CLOSE_QUARTER":
        # Finds the cycle itself, not pm.quarter(today): once today has rolled into the next quarter, pm.quarter(today) already
        # points at the NEW one, so "the quarter containing today" is the wrong question - the right one is "is there a cycle
        # whose end_date has passed that is still OPEN". Recurs daily (event_key includes today) until someone rolls it over,
        # at which point pm_cycle.status flips to CLOSED and this stops finding anything - never rolls the quarter over itself.
        overdue = db.one("SELECT quarter_label, end_date FROM pm_cycle WHERE status = 'OPEN' AND end_date < %s ORDER BY end_date DESC LIMIT 1", [today])
        if not overdue:
            return D
        k = db.one(f"SELECT count(*) FILTER (WHERE pm_status IN ('DONE','DONE_OUTSIDE_QUARTER')) AS done, count(*) FILTER (WHERE pm_status = 'PENDING') AS pending, count(*) AS scope FROM asset WHERE {pm.IN_SCOPE}")
        overdue_days = (today - overdue["end_date"]).days
        admins = db.query("SELECT DISTINCT engineer_key FROM portal_user WHERE role = 'ADMIN' AND active AND engineer_key IS NOT NULL")
        for row in admins:
            D[row["engineer_key"]] = dict(
                event_key=f"{overdue['quarter_label']}:{today.isoformat()}",
                subject=f"{overdue['quarter_label']} needs closing - ended {overdue_days} day{'s' if overdue_days != 1 else ''} ago",
                intro=f"{overdue['quarter_label']} ended on {overdue['end_date']:%d %b %Y} and has not been rolled over to the next quarter yet. "
                      f"{k['pending']} of {k['scope']} in-scope assets still show pending PM for it. Close the quarter from PM cycles once you have reviewed this "
                      "- nothing closes it automatically.",
                rows=[], columns=[])
    elif rule_key == "ASSET_ADDED":
        rows = db.query("""SELECT a.asset_key, a.asset_class, trim(coalesce(a.make,'') || ' ' || coalesce(a.model,'')) AS model, a.location_code, a.engineer_name, a.first_seen_date
                           FROM asset a WHERE a.is_current = 1 AND a.record_level = 'ASSET' AND a.engineer_name IS NOT NULL AND a.first_seen_date >= %s
                             AND EXISTS (SELECT 1 FROM asset_change_log l WHERE l.asset_key = a.asset_key AND l.change_type = 'ADDED' AND l.snapshot_date >= %s
                                         UNION ALL SELECT 1 FROM portal_lock p WHERE p.dataset = 'assets' AND p.record_key = a.asset_key AND p.field = '__created__')""", [_lookback(params), _lookback(params)])
        for eng, items in _group(rows, "engineer_name").items():
            D[eng] = dict(event_key="|".join(sorted(i["asset_key"] for i in items))[:300], subject=f"{len(items)} new asset(s) assigned to you", intro="These assets were added to the register and assigned to you.",
                          rows=items, columns=[("asset_key", "Asset"), ("asset_class", "Class"), ("model", "Model"), ("location_code", "Location")])
    elif rule_key == "ASSET_REMOVED":
        rows = db.query("""SELECT a.asset_key, a.asset_class, trim(coalesce(a.make,'') || ' ' || coalesce(a.model,'')) AS model, a.location_code, a.engineer_name
                           FROM asset a WHERE a.is_current = 0 AND a.record_level = 'ASSET' AND a.engineer_name IS NOT NULL
                             AND (EXISTS (SELECT 1 FROM asset_change_log l WHERE l.asset_key = a.asset_key AND l.change_type = 'REMOVED' AND l.snapshot_date >= %s)
                                  OR EXISTS (SELECT 1 FROM portal_audit p WHERE p.dataset = 'assets' AND p.record_key = a.asset_key AND p.action = 'ARCHIVE' AND p.at >= %s))""", [_lookback(params), _lookback(params)])
        for eng, items in _group(rows, "engineer_name").items():
            D[eng] = dict(event_key="|".join(sorted(i["asset_key"] for i in items))[:300], subject=f"{len(items)} asset(s) removed from your list", intro="These assets are no longer in the register.",
                          rows=items, columns=[("asset_key", "Asset"), ("asset_class", "Class"), ("model", "Model"), ("location_code", "Location")])
    elif rule_key == "AMC_EXPIRED":
        rows = db.query("""SELECT asset_key, asset_class, trim(coalesce(make,'') || ' ' || coalesce(model,'')) AS model, cover_type, cover_expiry_date, engineer_name FROM asset
                           WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name IS NOT NULL AND cover_status = 'EXPIRED' AND cover_expiry_date >= %s ORDER BY cover_expiry_date""", [_lookback(params)])
        for eng, items in _group(rows, "engineer_name").items():
            D[eng] = dict(event_key="|".join(f"{i['asset_key']}@{i['cover_expiry_date']}" for i in items)[:300], subject=f"Cover expired on {len(items)} asset(s)", intro="The AMC / warranty on these assets has expired.",
                          rows=items, columns=[("asset_key", "Asset"), ("asset_class", "Class"), ("model", "Model"), ("cover_type", "Cover"), ("cover_expiry_date", "Expired on")])
    elif rule_key == "AMC_EXPIRING":
        if today.weekday() != 0:
            return D
        rows = db.query("""SELECT asset_key, asset_class, trim(coalesce(make,'') || ' ' || coalesce(model,'')) AS model, cover_type, cover_expiry_date, engineer_name FROM asset
                           WHERE is_current = 1 AND record_level = 'ASSET' AND engineer_name IS NOT NULL AND cover_expiry_date BETWEEN current_date AND current_date + %s ORDER BY cover_expiry_date""", [int(params.get("within_days", 60))])
        for eng, items in _group(rows, "engineer_name").items():
            D[eng] = dict(event_key=_iso_week(today), subject=f"Cover ending soon on {len(items)} asset(s)", intro=f"Cover on these assets ends within {params.get('within_days', 60)} days.",
                          rows=items, columns=[("asset_key", "Asset"), ("asset_class", "Class"), ("model", "Model"), ("cover_type", "Cover"), ("cover_expiry_date", "Ends on")])
    elif rule_key == "CALL_ASSIGNED":
        rows = db.query("""SELECT sr_id, cipl_call_date, asset_key, priority, problem_description, engineer FROM svc_call WHERE is_current = 1 AND call_status = 'OPEN' AND engineer IS NOT NULL
                           AND cipl_call_date >= %s ORDER BY cipl_call_date""", [_lookback(params)])
        for eng, items in _group(rows, "engineer").items():
            D[eng] = dict(event_key="|".join(i["sr_id"] for i in items)[:300], subject=f"{len(items)} new call(s) assigned to you", intro="These service calls were logged against you.",
                          rows=items, columns=[("sr_id", "SR ID"), ("cipl_call_date", "Logged"), ("asset_key", "Asset"), ("priority", "Priority"), ("problem_description", "Problem")])
    elif rule_key == "CALL_OVERDUE":
        if today.weekday() != 0:
            return D
        rows = db.query("""SELECT sr_id, cipl_call_date, asset_key, priority, problem_description, ageing_days, engineer FROM svc_call WHERE is_current = 1 AND call_status = 'OPEN'
                           AND engineer IS NOT NULL AND ageing_days >= %s ORDER BY ageing_days DESC""", [int(params.get("older_than_days", 7))])
        for eng, items in _group(rows, "engineer").items():
            D[eng] = dict(event_key=_iso_week(today), subject=f"{len(items)} open call(s) older than {params.get('older_than_days', 7)} days", intro="Please update or close these calls.",
                          rows=items, columns=[("sr_id", "SR ID"), ("cipl_call_date", "Logged"), ("ageing_days", "Age (d)"), ("asset_key", "Asset"), ("problem_description", "Problem")])
    elif rule_key == "PART_RECEIVED":
        rows = db.query("""SELECT i.inward_id, i.received_date, i.sr_id, i.part_description, c.engineer FROM spare_inward i JOIN svc_call c ON c.sr_id = i.sr_id AND c.is_current = 1
                           WHERE i.is_current = 1 AND i.received_date >= %s AND c.engineer IS NOT NULL AND c.call_status = 'OPEN' ORDER BY i.received_date""", [_lookback(params)])
        for eng, items in _group(rows, "engineer").items():
            D[eng] = dict(event_key="|".join(i["inward_id"] for i in items)[:300], subject=f"Spare part received for {len(items)} of your call(s)", intro="The part is here - the call can proceed.",
                          rows=items, columns=[("sr_id", "SR ID"), ("part_description", "Part"), ("received_date", "Received"), ("inward_id", "Inward ID")])
    return D


def run_rules(dry_run=False, only=None):
    """Evaluate enabled rules. Returns a list of what was (or, in a dry run, would be) sent. Never raises for one bad recipient."""
    cfg = get_smtp()
    if not cfg["enabled"] and not dry_run:
        return []
    sent = []
    for r in rules_state():
        if not r["enabled"] or (only and r["key"] != only):
            continue
        for eng, ev in detect(r["key"], r["params"]).items():
            to = engineer_email(eng)
            extra = [a for a in (r["extra_to"] or "").split(", ") if a]
            recipients = [a for a in [to, *extra] if a]
            item = {"rule": r["key"], "engineer": eng, "recipients": recipients, "subject": ev["subject"], "count": len(ev["rows"]), "status": "DRY_RUN"}
            if not to:
                item["status"] = "NO_ADDRESS"
            if dry_run:
                sent.append(item)
                continue
            for addr in recipients:
                done = db.one("SELECT status FROM notify_log WHERE rule_key = %s AND event_key = %s AND recipient = %s", [r["key"], f"{eng}:{ev['event_key']}", addr])
                if done and (done["status"] == "SENT" or done["status"] == "SKIPPED"):
                    continue
                body, text = page(ev["subject"], ev["intro"], ev["rows"], ev["columns"], footer=f"Automatic message from the ITAM Portal ({r['label']}).{(' ' + cfg['portal_url']) if cfg['portal_url'] else ''}")
                status, err = "SENT", None
                try:
                    send(addr, ev["subject"], text, body, cfg=cfg)
                except MailError as e:
                    status, err = "FAILED", str(e)[:300]
                with db.write() as con:
                    con.execute("""INSERT INTO notify_log (rule_key, event_key, recipient, subject, status, error) VALUES (%s,%s,%s,%s,%s,%s)
                                   ON CONFLICT (rule_key, event_key, recipient) DO UPDATE SET status = EXCLUDED.status, error = EXCLUDED.error, at = now(), attempts = notify_log.attempts + 1""",
                                (r["key"], f"{eng}:{ev['event_key']}", addr, ev["subject"], status, err))
                sent.append({**item, "recipients": [addr], "status": status, "error": err})
    if not dry_run and any(x["rule"] == "PM_KICKOFF" and x["status"] == "SENT" for x in sent):
        with db.write() as con:
            con.execute("UPDATE pm_cycle SET kickoff_notified_at = now() WHERE quarter_label = %s AND kickoff_notified_at IS NULL", (pm.quarter(dt.date.today())["label"],))
    return sent


def log_list(limit=100, offset=0):
    return {"rows": db.query("SELECT log_id AS id, at, rule_key, recipient, subject, status, error, attempts FROM notify_log ORDER BY log_id DESC LIMIT %s OFFSET %s", [limit, offset]),
            "total": db.one("SELECT count(*) AS n FROM notify_log")["n"]}
