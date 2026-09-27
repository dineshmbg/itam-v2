"""Report builder: blank, custom-heading columns appended after the real data columns (no SQL, no data - every export writer already
treats a row missing a column's key as blank). Read-only: no sandbox transaction needed, just runs SELECTs.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_reports_blank_columns.py
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from portal.app import export, reports  # noqa: E402


def test_blank_column_appended_with_no_sql_column(box):
    defn = {"dataset": "assets", "columns": ["asset_key", "asset_class"], "blank_columns": [{"label": "signature"}, {"label": "  remarks  "}]}
    sql, params, cols = reports.build(defn, admin=True)
    assert '"_blank_0"' not in sql and '"_blank_1"' not in sql          # never touches the SQL
    assert cols[-2:] == [("_blank_0", "SIGNATURE"), ("_blank_1", "REMARKS")]


def test_blank_column_survives_grouped_reports_too(box):
    defn = {"dataset": "assets", "group_by": ["engineer_name"], "blank_columns": [{"label": "notes"}]}
    sql, params, cols = reports.build(defn, admin=True)
    assert cols[-1] == ("_blank_0", "NOTES")


def test_blank_column_requires_a_heading(box):
    with pytest.raises(reports.ReportError):
        reports.build({"dataset": "assets", "blank_columns": [{"label": "   "}]}, admin=True)


def test_at_most_ten_blank_columns(box):
    defn = {"dataset": "assets", "blank_columns": [{"label": f"col{i}"} for i in range(15)]}
    _, _, cols = reports.build(defn, admin=True)
    assert sum(1 for k, _ in cols if k.startswith("_blank_")) == 10


def test_run_and_every_export_format_render_blank_columns_without_crashing(box):
    defn = {"dataset": "assets", "columns": ["asset_key"], "blank_columns": [{"label": "sign-off"}]}
    r = reports.run(defn, admin=True, limit=5)
    assert r["columns"][-1] == {"key": "_blank_0", "label": "SIGN-OFF"}
    assert all("_blank_0" not in row for row in r["rows"])           # the column simply is not in the row data
    table, total = reports.table_for_export(defn, admin=True, title="Assets")
    assert export.csv_bytes(table)
    assert export.xlsx_bytes([table], title="Assets")
