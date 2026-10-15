"""Software/OS observations: inspection date is server-stamped and unforgeable, rows are append-only, latest per source, stale flags. Rolled back (conftest.box)."""
import datetime as dt

import psycopg
import pytest

from portal.app import swobs


def test_inspected_at_is_stamped_by_the_server(box):
    before = dt.datetime.now(dt.timezone.utc)
    oid = swobs.record("A1", "BIGFIX_REPORT", {"os_build": "10.0.26200.9445"}, by="TEST")
    row = box.execute("SELECT inspected_at FROM portal_sw_obs WHERE obs_id = %s", (oid,)).fetchone()
    assert row[0] >= before - dt.timedelta(seconds=5)
    with pytest.raises(TypeError):
        swobs.record("A1", "MANUAL", {}, by="TEST", inspected_at=dt.datetime(2020, 1, 1))
    with pytest.raises(swobs.ObsError):
        swobs.record("A1", "MANUAL", {"inspected_at": "2020-01-01"}, by="TEST")


def test_rejects_unknown_source_and_field(box):
    with pytest.raises(swobs.ObsError):
        swobs.record("A1", "GUESS", {}, by="TEST")
    with pytest.raises(swobs.ObsError):
        swobs.record("A1", "MANUAL", {"colour": "red"}, by="TEST")


def test_rows_cannot_be_updated_or_deleted(box):
    oid = swobs.record("A1", "MANUAL", {"vpn_name": "Zscaler"}, by="TEST")
    for sql in ("UPDATE portal_sw_obs SET vpn_name = 'x' WHERE obs_id = %s", "DELETE FROM portal_sw_obs WHERE obs_id = %s"):
        with pytest.raises(psycopg.errors.RaiseException):
            with box.transaction():
                box.execute(sql, (oid,))


def test_latest_per_source_side_by_side_with_stale_flags(box):
    swobs.record("A2", "BIGFIX_REPORT", {"os_build": "10.0.19045.1"}, by="T")
    new = swobs.record("A2", "BIGFIX_REPORT", {"os_build": "10.0.19045.2"}, by="T", data_as_of=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45))
    swobs.record("A2", "TREND_REPORT", {"os_build": "19045"}, by="T", data_as_of=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=100))
    got = {r["source"]: r for r in swobs.latest_by_source("A2")}
    assert set(got) == {"BIGFIX_REPORT", "TREND_REPORT"}
    assert got["BIGFIX_REPORT"]["obs_id"] == new and got["BIGFIX_REPORT"]["os_build"] == "10.0.19045.2"
    assert got["BIGFIX_REPORT"]["stale"] == "STALE" and got["TREND_REPORT"]["stale"] == "VERY_STALE"
    swobs.record("A2", "MANUAL", {}, by="T")
    assert {r["source"]: r for r in swobs.latest_by_source("A2")}["MANUAL"]["stale"] is None
