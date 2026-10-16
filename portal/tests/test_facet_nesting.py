"""Asset filter bar: a Type is nested under its Class. A class with types ticked keeps only those types, a class with none ticked stays whole.
Synthetic classes (ZZN-*) inside one rolled-back transaction, so the result never depends on the real data and nothing is written.

  cd portal && .venv\\Scripts\\python -m pytest -q tests/test_facet_nesting.py
"""
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from portal.app import queries  # noqa: E402
from portal.app.datasets import DATASETS  # noqa: E402

DS = DATASETS["assets"]
TODAY = dt.date(2026, 10, 1)
# ZZN-A has two types (3 + 2 rows), ZZN-B has two (4 + 1), ZZN-C has one (6), ZZN-N has a type but no class
ROWS = [("ZZN-A", "ZZN-A1", 3), ("ZZN-A", "ZZN-A2", 2), ("ZZN-B", "ZZN-B1", 4), ("ZZN-B", "ZZN-B2", 1), ("ZZN-C", "ZZN-C1", 6), (None, "ZZN-N1", 2)]


def seed(con):
    i = 0
    for cls, typ, n in ROWS:
        for _ in range(n):
            i += 1
            con.execute("INSERT INTO asset (snapshot_date, asset_key, ci_no, asset_class, asset_type, record_level, is_current, first_seen_date, last_seen_date) VALUES (%s,%s,%s,%s,%s,'ASSET',1,%s,%s)",
                        (TODAY, f"ZZN-{i:03}", f"ZZN-{i:03}", cls, typ, TODAY, TODAY))


def total(**filters):
    f = {"record_level": ["ASSET"], **filters}
    w, p = queries.where(DS, "", f)
    from portal.app import db
    return db.one(f"SELECT count(*) AS n FROM asset WHERE {w} AND asset_key LIKE 'ZZN-%%'", p)["n"]


def test_a_type_narrows_only_its_own_class(box):
    seed(box)
    assert total(asset_class=["ZZN-A"]) == 5                                          # nothing ticked under the class: the whole class
    assert total(asset_class=["ZZN-A"], asset_type=["ZZN-A1"]) == 3                   # one type ticked: just that type
    assert total(asset_class=["ZZN-A", "ZZN-B"], asset_type=["ZZN-A1"]) == 3 + 5      # B has no type ticked, so all of B stays in
    assert total(asset_class=["ZZN-A", "ZZN-B"], asset_type=["ZZN-A1", "ZZN-B2"]) == 3 + 1
    assert total(asset_class=["ZZN-A", "ZZN-C"], asset_type=["ZZN-A2"]) == 2 + 6
    assert total(asset_class=["ZZN-A"], asset_type=["ZZN-A1", "ZZN-A2"]) == 5


def test_a_type_on_its_own_still_filters(box):
    seed(box)
    assert total(asset_type=["ZZN-B1"]) == 4                                          # old links / reports that send only a type
    assert total(asset_type=["ZZN-B1", "ZZN-C1"]) == 4 + 6
    assert total(asset_class=["~blank"], asset_type=["ZZN-N1"]) == 2                  # a blank class takes part like any other


def test_class_counts_ignore_the_type_filter_and_carry_each_classes_types(box):
    seed(box)
    f = {"record_level": ["ASSET"], "asset_class": ["ZZN-A"], "asset_type": ["ZZN-A1"]}
    res = queries.facet_counts(DS, "", f)
    classes = {i["v"]: i for i in res["asset_class"] if i["v"].startswith("ZZN-")}
    assert {k: v["n"] for k, v in classes.items()} == {"ZZN-A": 5, "ZZN-B": 5, "ZZN-C": 6}           # ticking a type must not shrink the other classes
    assert {k["v"]: k["n"] for k in classes["ZZN-A"]["kids"]} == {"ZZN-A1": 3, "ZZN-A2": 2}
    assert [k["v"] for k in classes["ZZN-C"]["kids"]] == ["ZZN-C1"]                                   # a single-type class still lists its one type
    assert "asset_type" in res                                                                       # the flat Type counts are still answered (API unchanged)


def test_meta_tells_the_client_which_facet_is_nested():
    facets = {f["key"]: f for f in queries.meta()["assets"]["facets"]}
    assert facets["asset_type"]["under"] == "asset_class" and facets["asset_class"]["under"] is None
    assert all(f["under"] is None for name, ds in queries.meta().items() if name != "assets" for f in ds["facets"])
