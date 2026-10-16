"""Asset hover card: the short card (ID, type - make - model, hostname / serial / OS, processor / RAM / storage). A hostname equal to the asset ID is left
out, empty fields are None, and the full record is still in the payload because the new-call form reads it to auto-fill the user and engineer.
Synthetic assets (ZZA-*) inside one rolled-back transaction, so real data is never touched.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_asset_card.py
"""
import datetime as dt

import pytest
from starlette.testclient import TestClient

from conftest import fake_user
from portal.app.main import app

TODAY = dt.date(2026, 10, 1)


def seed(con):
    cols = "snapshot_date, asset_key, ci_no, asset_class, asset_type, record_level, is_current, first_seen_date, last_seen_date, hostname, serial_no, make, model, os, processor, ram, storage, cpf_no, engineer_name"
    ph = ", ".join(["%s"] * 19)
    rows = [
        ("ZZA-AIO", "DESKTOP", "ALL IN ONE", "ZZ-HOST-1", "ZZSN1", "HP", "PRO ONE 440", "WIN 11 PRO", "I7-12700", "16GB DDR5", "512GB SSD", 990001, "ZZ ENGINEER"),
        ("ZZA-SAME", "DESKTOP", "DESKTOP", "ZZA-SAME", "ZZSN2", "HP", "ELITE", "WIN 10 PRO", "I7-3770", "12GB DDR3", "256GB SSD", None, None),     # hostname == asset ID
        ("ZZA-PRN", "PRINTER", "PRINTER", None, "ZZSN3", "HP", "LJ PRO", None, None, None, None, None, None),                                        # no hostname, no hardware
    ]
    for key, cls, typ, host, serial, make, model, os_, cpu, ram, st, cpf, eng in rows:
        con.execute(f"INSERT INTO asset ({cols}) VALUES ({ph})", (TODAY, key, key, cls, typ, "ASSET", 1, TODAY, TODAY, host, serial, make, model, os_, cpu, ram, st, cpf, eng))


@pytest.mark.parametrize("role", ["ADMIN", "USER"])
def test_asset_card_is_short_and_leaves_out_what_is_missing_or_repeated(box, monkeypatch, role):
    seed(box)
    fake_user(monkeypatch, role)
    with TestClient(app) as c:
        full = c.get("/api/card/asset/ZZA-AIO").json()
        assert full["small"] == {"key": "ZZA-AIO", "cls": "DESKTOP", "what": "ALL IN ONE - HP - PRO ONE 440", "hostname": "ZZ-HOST-1", "serial": "ZZSN1",
                                 "os": "WIN 11 PRO", "processor": "I7-12700", "ram": "16GB DDR5", "storage": "512GB SSD"}
        assert c.get("/api/card/asset/ZZA-SAME").json()["small"]["hostname"] is None                       # same as the asset ID: it would only repeat the heading
        prn = c.get("/api/card/asset/ZZA-PRN").json()["small"]
        assert prn["hostname"] is None and prn["os"] is None and prn["processor"] is None and prn["ram"] is None and prn["storage"] is None
        assert prn["what"] == "PRINTER - HP - LJ PRO"
        assert "barcode_svg" not in full                                                                    # the barcode stays on the printed labels
        # the new-call form still auto-fills the user (CPF) and the engineer from this record
        assert full["row"]["cpf_no"] == 990001 and full["row"]["engineer_name"] == "ZZ ENGINEER"
        assert c.get("/api/card/asset/ZZA-NOPE").status_code == 404
