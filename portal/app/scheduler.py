"""Background jobs, run inside the portal process: automatic backups, notification rules, PM cycle upkeep and housekeeping.

Set PORTAL_SCHEDULER=0 to disable (tests do). State (what ran when) is kept in portal_setting so a restart never repeats a job.
"""
import datetime as dt
import json
import logging
import os
import threading
import time

from . import auth, backup, db, mailer, pm

log = logging.getLogger("itam.scheduler")
DEFAULT_BACKUP = {"enabled": True, "time": "02:00", "keep": 14}


def _get(key, default):
    r = db.one("SELECT value FROM portal_setting WHERE key = %s", [key])
    return {**default, **r["value"]} if r else dict(default)


def _put(key, value, by="scheduler"):
    with db.write() as con:
        con.execute("INSERT INTO portal_setting (key, value, updated_by) VALUES (%s,%s::jsonb,%s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by",
                    (key, json.dumps(value), by))


def backup_settings():
    return _get("backup_auto", DEFAULT_BACKUP)


def save_backup_settings(v, editor):
    t = str(v.get("time", "02:00"))
    try:
        dt.datetime.strptime(t, "%H:%M")
        keep = int(v.get("keep", 14))
    except (ValueError, TypeError):
        raise backup.BackupError("Enter the time as HH:MM and the number of copies to keep as a whole number.") from None
    if not 1 <= keep <= 365:
        raise backup.BackupError("Keep between 1 and 365 automatic backups.")
    _put("backup_auto", {"enabled": bool(v.get("enabled")), "time": t, "keep": keep}, editor)
    return backup_settings()


def tick(now=None):
    """One pass over all jobs. Each job is isolated: a failure is logged and never stops the others."""
    now = now or dt.datetime.now()
    today = now.date().isoformat()
    st = _get("sched_state", {})
    changed = False

    def job(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            log.exception("scheduled job %s failed", name)
            auth.log("SCHEDULER", None, "JOB_FAILED", name, {"error": str(e)[:300]}, ok=False)

    b = backup_settings()
    if b["enabled"] and now.strftime("%H:%M") >= b["time"] and st.get("last_backup") != today:
        def do_backup():
            r = backup.run_backup("AUTO", "scheduler")
            removed = backup.prune(int(b["keep"]))
            auth.log("SCHEDULER", None, "BACKUP_AUTO", r["file"], {"size": r["size_bytes"], "pruned": removed})
        job("backup", do_backup)
        st["last_backup"] = today; changed = True

    if st.get("last_pm") != today:
        def do_pm():
            with db.write() as con:
                pm.ensure_cycle(con)
                if now.weekday() == 0:
                    n = pm.capture_snapshot(con, pm.quarter(now.date())["label"], now.date())
                    auth.log("SCHEDULER", None, "PM_SNAPSHOT", pm.quarter(now.date())["label"], {"assets": n})
        job("pm", do_pm)
        st["last_pm"] = today; changed = True

    last_rules = st.get("last_rules_ts", 0)
    if time.time() - last_rules >= mailer.RULE_INTERVAL_S:
        def do_rules():
            if not mailer.get_smtp()["enabled"]:
                return
            sent = mailer.run_rules()
            if sent:
                auth.log("SCHEDULER", None, "NOTIFY_RUN", None, {"sent": sum(1 for s in sent if s["status"] == "SENT"), "failed": sum(1 for s in sent if s["status"] == "FAILED")})
        job("notifications", do_rules)
        st["last_rules_ts"] = time.time(); changed = True

    if st.get("last_cleanup") != now.strftime("%Y-%m-%d %H"):
        def do_clean():
            with db.write() as con:
                con.execute("DELETE FROM portal_session WHERE expires_at < now() OR last_active_at < now() - interval '2 days'")
        job("cleanup", do_clean)
        st["last_cleanup"] = now.strftime("%Y-%m-%d %H"); changed = True
    if changed:
        _put("sched_state", st)


def start():
    if os.environ.get("PORTAL_SCHEDULER") == "0":
        return None
    stop = threading.Event()

    def loop():
        stop.wait(20)                    # let the server finish starting
        while not stop.is_set():
            try:
                tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler pass failed")
            stop.wait(30)
    threading.Thread(target=loop, daemon=True, name="itam-scheduler").start()
    return stop.set
