"""Record identity for inward / outward lines (tools/call_tracking.py, 2026-10-02).

The tracker sheet's SERIAL column is a label for people. It used to decide a line's ID, so filling in blank serials made 30 outward
lines look deleted-and-re-added and tripped the 20% safety stop. A line is now matched by what it is (call + part [+ date]) and keeps
its ID for good. Every test runs inside the shared rolled-back transaction - the real data is never changed.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_call_ids.py
"""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
try:
    import call_tracking as ct
except ModuleNotFoundError:      # the loaders need pandas, which the portal's own venv does not ship: borrow the base interpreter's copy
    sys.path.append(str(Path(sys.base_prefix) / "Lib" / "site-packages"))
    import call_tracking as ct

AS_OF = dt.date(2026, 10, 1)


def put_out(con, oid, sr, part, **kw):
    cols = {"outward_id": oid, "sr_id": sr, "part_description": part, "is_current": 1, "source_row": kw.pop("source_row", None), **kw}
    con.execute(f"INSERT INTO spare_outward ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})", list(cols.values()))


def put_in(con, iid, sr, part, date, **kw):
    cols = {"inward_id": iid, "sr_id": sr, "part_description": part, "inward_date": date, "is_current": 1, **kw}
    con.execute(f"INSERT INTO spare_inward ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})", list(cols.values()))


def out(oid, sr, part, **kw):
    return {"OUTWARD_ID": oid, "SR_ID": sr, "PART_DESCRIPTION": part, **{k.upper(): v for k, v in kw.items()}}


def inn(iid, sr, part, date, **kw):
    return {"INWARD_ID": iid, "SR_ID": sr, "PART_DESCRIPTION": part, "INWARD_DATE": date, **{k.upper(): v for k, v in kw.items()}}


def row(con, table, key, val):
    return con.execute(f"SELECT * FROM {table} WHERE {key} = %s", (val,)).fetchone()


def test_a_filled_in_serial_does_not_change_the_record(box):
    """The 2026-10-01 incident: a line stored as OUT-R900 (blank serial) arrives with serial 900 filled in."""
    put_out(box, "OUT-R900", "SRTEST0001", "18.5 LCD DISPLAY", source_row=900)
    rec = out("OUT-0900", "SRTEST0001", "18.5 LCD DISPLAY", gatepass_no="1243")
    ct.load_table(box, "SPARE_OUTWARD", [rec], AS_OF, force=True)      # force: the table holds real rows this one-line file does not mention
    assert box.execute("SELECT count(*) FROM spare_outward WHERE sr_id = 'SRTEST0001'").fetchone()[0] == 1
    assert box.execute("SELECT count(*) FROM spare_outward WHERE outward_id = 'OUT-0900'").fetchone()[0] == 0
    assert box.execute("SELECT gatepass_no, is_current FROM spare_outward WHERE outward_id = 'OUT-R900'").fetchone() == ("1243", 1)
    assert box.execute("SELECT count(*) FROM cm_change_log WHERE record_key = 'OUT-R900' AND change_type = 'REMOVED'").fetchone()[0] == 0


def test_the_serial_cannot_point_a_line_at_someone_elses_record(box):
    """Serials swapped or retyped: lines still land on the records they actually are."""
    put_out(box, "OUT-R901", "SRTEST0002", "PSU")
    put_out(box, "OUT-R902", "SRTEST0003", "RAM")
    a, b = out("OUT-R901", "SRTEST0003", "RAM"), out("OUT-R902", "SRTEST0002", "PSU")        # the sheet's labels are crossed
    ct.reuse_ids(box, "SPARE_OUTWARD", [a, b])
    assert (a["OUTWARD_ID"], b["OUTWARD_ID"]) == ("OUT-R902", "OUT-R901")


def test_a_genuinely_new_line_gets_the_next_free_number(box):
    top = box.execute("SELECT max(substring(outward_id from 5)::int) FROM spare_outward WHERE outward_id ~ '^OUT-[0-9]+$'").fetchone()[0] or 0
    new1, new2 = out("OUT-0001", "SRTEST0004", "FAN"), out("OUT-0002", "SRTEST0005", "FAN")
    ct.reuse_ids(box, "SPARE_OUTWARD", [new1, new2])
    assert (new1["OUTWARD_ID"], new2["OUTWARD_ID"]) == (f"OUT-{top + 1:04d}", f"OUT-{top + 2:04d}")


def test_whitespace_and_case_in_the_part_text_do_not_matter(box):
    put_out(box, "OUT-R903", "SRTEST0006", "HP 8300  POWER SUPPLY")
    rec = out("OUT-0903", "srtest0006", " hp 8300 power supply ")
    ct.reuse_ids(box, "SPARE_OUTWARD", [rec])
    assert rec["OUTWARD_ID"] == "OUT-R903"


def test_a_corrected_part_text_is_an_edit_not_a_delete_and_add(box):
    """One line left on each side for the same call: the same record, changed - and it is reported, not silent."""
    put_out(box, "OUT-R904", "SRTEST0007", "HP 8300 MOTHERBORAD")
    rec, said = out("OUT-0904", "SRTEST0007", "HP 8300 MOTHERBOARD"), []
    res = ct.reuse_ids(box, "SPARE_OUTWARD", [rec], say=said.append)
    assert rec["OUTWARD_ID"] == "OUT-R904" and res["edited"] and res["new"] == 0
    assert any("OUT-R904" in s and "MOTHERBOARD" in s for s in said)


def test_two_different_new_parts_for_a_call_are_not_confused_with_an_edit(box):
    """When the call already has one old line and two new ones arrive, nothing is guessed: no one-for-one pairing."""
    put_out(box, "OUT-R905", "SRTEST0008", "PSU")
    a, b = out("OUT-1", "SRTEST0008", "RAM"), out("OUT-2", "SRTEST0008", "FAN")
    res = ct.reuse_ids(box, "SPARE_OUTWARD", [a, b])
    assert res["edited"] == [] and res["new"] == 2 and "OUT-R905" not in (a["OUTWARD_ID"], b["OUTWARD_ID"])


def test_repeated_identical_lines_pair_up_in_order_and_extras_are_new(box):
    put_out(box, "OUT-R906", "SRTEST0009", "CABLE")
    a, b = out("OUT-1", "SRTEST0009", "CABLE"), out("OUT-2", "SRTEST0009", "CABLE")
    ct.reuse_ids(box, "SPARE_OUTWARD", [a, b])
    assert a["OUTWARD_ID"] == "OUT-R906" and b["OUTWARD_ID"] != "OUT-R906" and a["OUTWARD_ID"] != b["OUTWARD_ID"]


def test_the_same_part_received_twice_for_one_call_stays_two_records(box):
    d1, d2 = dt.date(2026, 9, 1), dt.date(2026, 9, 20)
    put_in(box, "IN-R950", "SRTEST0010", "HDD", d1)
    put_in(box, "IN-R951", "SRTEST0010", "HDD", d2)
    a, b = inn("IN-0001", "SRTEST0010", "HDD", d2), inn("IN-0002", "SRTEST0010", "HDD", d1)    # file order reversed
    ct.reuse_ids(box, "SPARE_INWARD", [a, b])
    assert (a["INWARD_ID"], b["INWARD_ID"]) == ("IN-R951", "IN-R950")


def test_a_corrected_inward_date_keeps_its_record(box):
    put_in(box, "IN-R952", "SRTEST0011", "SSD", dt.date(2026, 9, 3))
    rec = inn("IN-0001", "SRTEST0011", "SSD", dt.date(2026, 9, 5), received_date=dt.date(2026, 9, 9))
    res = ct.reuse_ids(box, "SPARE_INWARD", [rec])
    assert rec["INWARD_ID"] == "IN-R952" and len(res["edited"]) == 1


def test_a_line_that_really_disappears_is_still_removed(box):
    """Real removal is unchanged: with the safety stop lifted, a record missing from the file is made not-current."""
    put_out(box, "OUT-R907", "SRTEST0012", "TONER")
    keep = out("OUT-1", "SRTEST0013", "TONER")
    ct.load_table(box, "SPARE_OUTWARD", [keep], AS_OF, force=True)
    assert box.execute("SELECT is_current FROM spare_outward WHERE outward_id = 'OUT-R907'").fetchone()[0] == 0
    assert box.execute("SELECT count(*) FROM cm_change_log WHERE record_key = 'OUT-R907' AND change_type = 'REMOVED'").fetchone()[0] == 1


def test_the_mass_removal_safety_stop_still_works(box):
    """The 20% guard stays: a file with almost nothing in it is refused rather than wiping the table."""
    with pytest.raises(SystemExit) as e:
        ct.load_table(box, "SPARE_OUTWARD", [out("OUT-1", "SRTEST0014", "ONE LONELY LINE")], AS_OF, force=False)
    assert "Load blocked" in str(e.value)


def test_matching_is_repeatable(box):
    put_out(box, "OUT-R908", "SRTEST0015", "PSU")
    recs = [out("OUT-1", "SRTEST0015", "PSU"), out("OUT-2", "SRTEST0016", "NEW THING")]
    ct.reuse_ids(box, "SPARE_OUTWARD", recs)
    first = [r["OUTWARD_ID"] for r in recs]
    ct.reuse_ids(box, "SPARE_OUTWARD", recs)
    assert [r["OUTWARD_ID"] for r in recs] == first


def test_a_line_removed_and_later_back_revives_the_same_record(box):
    put_out(box, "OUT-R909", "SRTEST0017", "TONER", is_current=0)
    rec = out("OUT-1", "SRTEST0017", "TONER")
    ct.reuse_ids(box, "SPARE_OUTWARD", [rec])
    assert rec["OUTWARD_ID"] == "OUT-R909"


def test_duplicate_lines_in_the_sheet_are_flagged_by_the_builder():
    """build_outward flags the second identical call+part line (the old serial-based flag is gone)."""
    cm = {"sr": 0, "desc": 1, "serial": 2}
    rows = [(10, ["SRX1", "PSU", "5"]), (11, ["SRX1", "PSU", "6"]), (12, ["SRX2", "PSU", "7"])]
    recs = ct.build_outward(rows, cm, {"SRX1": {}, "SRX2": {}}, None, AS_OF)
    flags = [r["_flags"] for r in recs]
    assert "DUPLICATE_LINE" not in flags[0] and "DUPLICATE_LINE" in flags[1] and "DUPLICATE_LINE" not in flags[2]
    assert all("DUPLICATE_SOURCE_SERIAL" not in f for f in flags)


# ---------------------------------------------------------------- OEM RMA lines (2026-10-03): same engine, key was the sheet's "Sr. no"
def put_rma(con, rid, faulty, call_date, rma_no="R-1", **kw):
    cols = {"rma_line_id": rid, "faulty_part_serial": faulty, "call_log_date": call_date, "rma_no": rma_no, "is_current": 1, **kw}
    con.execute(f"INSERT INTO oem_rma ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))})", list(cols.values()))


def rma(rid, faulty, call_date, rma_no="R-1", **kw):
    return {"RMA_LINE_ID": rid, "FAULTY_PART_SERIAL": faulty, "CALL_LOG_DATE": call_date, "RMA_NO": rma_no, **{k.upper(): v for k, v in kw.items()}}


def test_rma_renumbered_sr_no_keeps_the_record(box):
    d = dt.date(2026, 8, 1)
    put_rma(box, "RMA-R990", "SNTEST-A1", d)
    rec = rma("RMA-0990", "snTEST-a1", d, return_status="RETURNED")
    ct.reuse_ids(box, "OEM_RMA", [rec])
    assert rec["RMA_LINE_ID"] == "RMA-R990"


def test_rma_corrected_call_date_is_an_edit_of_the_same_record(box):
    put_rma(box, "RMA-R991", "SNTEST-B2", dt.date(2026, 8, 1))
    rec = rma("RMA-0001", "SNTEST-B2", dt.date(2026, 8, 3))
    res = ct.reuse_ids(box, "OEM_RMA", [rec])
    assert rec["RMA_LINE_ID"] == "RMA-R991" and res["edited"] == ["RMA-R991"] and res["new"] == 0


def test_rma_corrected_faulty_serial_is_an_edit_only_when_the_rma_number_pairs_one_for_one(box):
    put_rma(box, "RMA-R992", "SNTEST-C3", dt.date(2026, 8, 1), rma_no="RTEST-77")
    rec = rma("RMA-0001", "SNTEST-C3X", dt.date(2026, 8, 1), rma_no="RTEST-77")
    assert ct.reuse_ids(box, "OEM_RMA", [rec])["edited"] == ["RMA-R992"] and rec["RMA_LINE_ID"] == "RMA-R992"


def test_rma_blank_rma_number_never_pairs_unrelated_lines(box):
    put_rma(box, "RMA-R993", "SNTEST-D4", dt.date(2026, 8, 1), rma_no=None)
    rec = rma("RMA-0001", "SNTEST-OTHER", dt.date(2026, 8, 1), rma_no=None)
    res = ct.reuse_ids(box, "OEM_RMA", [rec])
    assert res["edited"] == [] and res["new"] == 1 and rec["RMA_LINE_ID"] != "RMA-R993"


def test_rma_same_serial_repaired_again_later_is_a_new_record(box):
    put_rma(box, "RMA-R994", "SNTEST-E5", dt.date(2026, 3, 1))
    again = rma("RMA-0001", "SNTEST-E5", dt.date(2026, 9, 1), rma_no="R-2")
    first = rma("RMA-0002", "SNTEST-E5", dt.date(2026, 3, 1))
    ct.reuse_ids(box, "OEM_RMA", [first, again])
    assert first["RMA_LINE_ID"] == "RMA-R994" and again["RMA_LINE_ID"] not in ("RMA-R994", first["RMA_LINE_ID"])
