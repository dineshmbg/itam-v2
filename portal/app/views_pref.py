"""Per-user saved column layout (which columns are shown, and in what order) for a register. Independent of the register's own
dataset definition (app/datasets.py) - that still decides which columns *exist* and how each one is fetched/rendered; this only
decides which of them a given person currently wants to see, and in what order. Saved against the account, so it follows them
anywhere they sign in.
"""
import json

from . import db

DDL = ["""CREATE TABLE IF NOT EXISTS portal_view (
    user_id INTEGER NOT NULL, dataset TEXT NOT NULL, columns JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, dataset))"""]

MAX_COLUMNS = 60


def get(user_id, dataset):
    row = db.one("SELECT columns FROM portal_view WHERE user_id = %s AND dataset = %s", [user_id, dataset])
    return row["columns"] if row else None


def save(user_id, dataset, columns):
    if not isinstance(columns, list) or not columns:
        raise ValueError("choose at least one column")
    cleaned, seen = [], set()
    for c in columns[:MAX_COLUMNS]:
        key = str((c or {}).get("key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned.append({"key": key, "visible": bool(c.get("visible", True))})
    if not any(c["visible"] for c in cleaned):
        raise ValueError("at least one column must stay visible")
    with db.write() as con:
        con.execute("""INSERT INTO portal_view (user_id, dataset, columns) VALUES (%s,%s,%s::jsonb)
                       ON CONFLICT (user_id, dataset) DO UPDATE SET columns = EXCLUDED.columns, updated_at = now()""",
                    (user_id, dataset, json.dumps(cleaned)))
    return cleaned


def reset(user_id, dataset):
    with db.write() as con:
        con.execute("DELETE FROM portal_view WHERE user_id = %s AND dataset = %s", (user_id, dataset))
