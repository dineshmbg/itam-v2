"""New dashboard KPI additions: parts lead-time/frequency on the call tracker, and admin-only (or self) personal contact
details on the engineer detail drawer. Read-only checks against the live database inside one rolled-back transaction.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_dashboard_kpis.py
"""
import sys
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from portal.app import dashboards, engineers  # noqa: E402
from portal.app.main import app  # noqa: E402
from tests.test_edit import as_user  # noqa: E402

HDR = {"X-Requested-With": "itam-portal", "Content-Type": "application/json"}


def test_calls_dashboard_has_parts_lead_time_and_top_parts(box):
    d = dashboards.calls()
    assert "avg_transit_days" in d["spares"] and "top_parts" in d["spares"]
    assert isinstance(d["spares"]["top_parts"], list)
    if d["spares"]["top_parts"]:
        assert {"label", "n"} <= set(d["spares"]["top_parts"][0])


def test_engineer_detail_hides_personal_fields_by_default(box):
    key = box.execute("SELECT engineer_key FROM portal_engineer WHERE ecode IS NOT NULL LIMIT 1").fetchone()[0]
    e = engineers.detail(key)
    assert "mobile_no" not in e and "personal_email" not in e
    e_admin = engineers.detail(key, admin=True)
    assert "mobile_no" in e_admin and "personal_email" in e_admin


def test_http_engineer_detail_personal_fields_admin_vs_self_vs_other(box, monkeypatch):
    key = box.execute("SELECT engineer_key FROM portal_engineer WHERE ecode IS NOT NULL LIMIT 1").fetchone()[0]
    as_user(monkeypatch, "ADMIN")
    with TestClient(app) as c:
        r = c.get(f"/api/engineers/{key}")
        assert r.status_code == 200 and "mobile_no" in r.json()
    as_user(monkeypatch, "USER", username="SELFVIEW", engineer_key=key)
    with TestClient(app) as c:
        r2 = c.get(f"/api/engineers/{key}")
        assert "mobile_no" in r2.json()          # an engineer sees their own contact details
    as_user(monkeypatch, "USER", username="OTHERVIEW", engineer_key="SOMEONE ELSE ENTIRELY")
    with TestClient(app) as c:
        r3 = c.get(f"/api/engineers/{key}")
        assert "mobile_no" not in r3.json()
