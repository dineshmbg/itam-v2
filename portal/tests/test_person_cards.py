"""Hover cards for a person: the short user card (CPF - name, designation, department, mobile) and the short engineer card (ECODE - name,
designation, mobile, gate pass). The same for every signed-in user; nothing else about the person may appear. Synthetic rows (ZZ*) inside
one rolled-back transaction, so real data is never touched.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_person_cards.py
"""
import datetime as dt
import json

import pytest
from starlette.testclient import TestClient

from conftest import fake_user
from portal.app.main import app

TODAY = dt.date(2026, 10, 1)
SECRETS = ["1980-01-01", "zz.personal@example.com", "ZZ-BANK-99", "zz.company@example.com", "ZZ-EPFO-7"]   # must never reach a card


def seed(con):
    con.execute("INSERT INTO employee (cpf_no, employee_name, designation, mobile_no, date_of_birth) VALUES (990001, 'ZZ SAMPLE PERSON', 'ZZ CHIEF MANAGER', '9000000001', DATE '1980-01-01')")
    con.execute("INSERT INTO employee (cpf_no, employee_name, designation) VALUES (990002, 'ZZ NO ASSET PERSON', 'ZZ CLERK')")
    for i, dept in enumerate(["ZZ DEPT A", "ZZ DEPT A", "ZZ DEPT B", None], 1):
        con.execute("INSERT INTO asset (snapshot_date, asset_key, ci_no, asset_class, asset_type, record_level, is_current, first_seen_date, last_seen_date, cpf_no, user_department) "
                    "VALUES (%s,%s,%s,'DESKTOP','DESKTOP','ASSET',1,%s,%s,990001,%s)", (TODAY, f"ZZC-{i}", f"ZZC-{i}", TODAY, TODAY, dept))
    con.execute("INSERT INTO cipl_employee (snapshot_date, ecode, employee_name, designation, mobile_no, company_email, personal_email, bank_account_no, epfo_no, ongc_gatepass_no) "
                "VALUES (%s,'ZZE001','ZZ SAMPLE ENGINEER','ZZ NETWORK ENGINEER','9000000002','zz.company@example.com','zz.personal@example.com','ZZ-BANK-99','ZZ-EPFO-7','ZZ-GP-0001')", (TODAY,))
    con.execute("INSERT INTO portal_engineer (engineer_key, display_name, ecode, source) VALUES ('ZZ SAMPLE ENGINEER', 'ZZ SAMPLE ENGINEER', 'ZZE001', 'TEST')")
    con.execute("INSERT INTO portal_engineer (engineer_key, display_name, ecode, source) VALUES ('ZZ ROSTERLESS', 'ZZ ROSTERLESS', NULL, 'TEST')")


@pytest.mark.parametrize("role", ["ADMIN", "USER"])
def test_user_card_is_short_and_the_same_for_every_role(box, monkeypatch, role):
    seed(box)
    fake_user(monkeypatch, role, engineer_key="SOMEONE ELSE ENTIRELY" if role == "USER" else None)
    with TestClient(app) as c:
        r = c.get("/api/card/cpf/990001")
        assert r.status_code == 200
        card = r.json()["small"]
        assert card == {"id": "990001", "name": "ZZ SAMPLE PERSON", "designation": "ZZ CHIEF MANAGER", "department": "ZZ DEPT A", "mobile": "9000000001"}   # the most common department
        assert not any(x in json.dumps(r.json()) for x in SECRETS)                                                                                      # no date of birth, no e-mail
        none = c.get("/api/card/cpf/990002").json()["small"]
        assert none["department"] is None and none["mobile"] is None                                                                                      # nothing recorded: nothing shown
        assert c.get("/api/card/cpf/123456789").status_code == 404


@pytest.mark.parametrize("role", ["ADMIN", "USER"])
def test_anyone_can_open_any_engineer_card_and_only_the_short_fields_appear(box, monkeypatch, role):
    seed(box)
    fake_user(monkeypatch, role, engineer_key="SOMEONE ELSE ENTIRELY" if role == "USER" else None)       # an ordinary user opening ANOTHER engineer's card
    with TestClient(app) as c:
        for ident in ("ZZ SAMPLE ENGINEER", "ZZE001"):                                                       # by name or by ECODE
            r = c.get(f"/api/card/engineer/{ident}") if ident.startswith("ZZ S") else c.get(f"/api/card/ecode/{ident}")
            assert r.status_code == 200, (role, ident)
            assert r.json()["small"] == {"id": "ZZE001", "name": "ZZ SAMPLE ENGINEER", "designation": "ZZ NETWORK ENGINEER", "mobile": "9000000002", "gate_pass": "ZZ-GP-0001"}
            assert not any(x in json.dumps(r.json()) for x in SECRETS)                                       # no e-mail of any kind, no bank or statutory numbers
        rl = c.get("/api/card/engineer/ZZ ROSTERLESS").json()["small"]
        assert rl["id"] is None and rl["mobile"] is None and rl["gate_pass"] is None and "roster" in rl["note"]
        assert c.get("/api/card/engineer/ZZ NOBODY").status_code == 404
