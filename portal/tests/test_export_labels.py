"""Exports show the on-screen status wording; the stored codes are untouched."""
import csv
import io

from app import export


def test_status_columns_use_display_wording():
    table = {"title": "t", "columns": [("asset_key", "Asset"), ("asset_status", "Status"), ("pm_status", "PM"), ("call_status", "Call"), ("cover_status", "Cover")],
             "rows": [{"asset_key": "A1", "asset_status": "IN_USE", "pm_status": "DONE_OUTSIDE_QUARTER", "call_status": "CLOSED", "cover_status": "EXPIRING_90D"}]}
    rows = list(csv.reader(io.StringIO(export.csv_bytes(table).decode("utf-8-sig"))))
    assert rows[1] == ["A1", "Deployed", "Completed late", "Resolved", "Renewal due"]
    assert table["rows"][0]["asset_status"] == "IN_USE"


def test_other_columns_and_unknown_values_pass_through():
    assert export.shown("asset_class", "PENDING") == "PENDING"
    assert export.shown("asset_status", "SURPLUS") == "SURPLUS"
