"""PM work orders: generation, checklist execution, findings, two-step sign-off. Transaction-isolated (see conftest.box)."""
import datetime as dt

import pytest

from conftest import HDR, fake_user
from portal.app import pm, pmwo
from portal.app.main import app
from starlette.testclient import TestClient

ENG = {"username": "ENG1", "display_name": "Eng One", "role": "USER"}
ADM1 = {"username": "ADM1", "display_name": "Admin One", "role": "ADMIN"}
ADM2 = {"username": "ADM2", "display_name": "Admin Two", "role": "ADMIN"}


def one(con, sql, params=()):
    return con.execute(sql, params).fetchone()


@pytest.fixture()
def fresh(box):
    """Generated work orders for this quarter on a clean slate (the delete is rolled back with the rest)."""
    box.execute("DELETE FROM pm_work_order")
    box.execute("UPDATE asset SET pm_date = NULL WHERE " + pm.IN_SCOPE)
    out = pmwo.generate("test")
    return box, out


def _wo(box, cls="DESKTOP", verify=None):
    sql = "SELECT wo_id FROM pm_work_order WHERE asset_class = %s AND state = 'OPEN'" + ("" if verify is None else f" AND verify_required = {verify}") + " ORDER BY wo_id LIMIT 1"
    return one(box, sql, (cls,))[0]


def _answer_all(wo_id, result="PASS", value=None):
    d = pmwo.detail(wo_id)
    return [{"seq": r["seq"], "result": result if r["kind"] == "PASSFAIL" else None, "value": value if r["kind"] == "VALUE" else None} for r in d["results"]]


def test_generation_covers_scope_once_and_skips_laptops(fresh):
    box, out = fresh
    scope = one(box, f"SELECT count(*) FROM asset WHERE {pm.IN_SCOPE}")[0]
    assert out["created"] == scope and out["legacy"] == 0
    assert one(box, "SELECT count(*) FROM pm_work_order WHERE asset_class = 'LAPTOP'")[0] == 0
    assert pmwo.generate("test")["created"] == 0                       # idempotent
    assert one(box, "SELECT count(*) FROM pm_checklist WHERE status = 'ACTIVE'")[0] >= 9
    kinds = {r[0]: r[1] for r in box.execute("SELECT asset_class, owner_kind FROM pm_work_order GROUP BY 1, 2").fetchall()}
    assert kinds["DESKTOP"] == "PERSON" and kinds["PRINTER"] == "SECTION"
    assert one(box, "SELECT bool_and(verify_required) FROM pm_work_order WHERE criticality = 'A'")[0] is True      # criticality A is always verified
    c = one(box, "SELECT count(*) FILTER (WHERE verify_required), count(*) FROM pm_work_order WHERE criticality = 'C'")
    assert 0.04 < c[0] / c[1] < 0.2                                                                                  # roughly the 10% sample


def test_assets_already_done_this_quarter_get_a_closed_legacy_work_order(box):
    box.execute("DELETE FROM pm_work_order")
    key = one(box, f"SELECT asset_key FROM asset WHERE {pm.IN_SCOPE} ORDER BY asset_key LIMIT 1")[0]
    box.execute("UPDATE asset SET pm_date = %s, pm_done_by = 'SOMEONE' WHERE asset_key = %s", (pm.quarter(dt.date.today())["start"], key))
    out = pmwo.generate("test")
    assert out["legacy"] >= 1
    assert one(box, "SELECT state, legacy, ack_state FROM pm_work_order WHERE asset_key = %s", (key,)) == ("CLOSED", True, "NA")


def test_cannot_complete_with_unanswered_mandatory_lines_or_failure_without_a_note(fresh):
    box, _ = fresh
    wid = _wo(box)
    pmwo.save(wid, [], 10, None, ENG)
    with pytest.raises(pm.PmError, match="not been answered"):
        pmwo.complete(wid, None, ENG, "x")
    res = _answer_all(wid)
    res[0]["result"] = "FAIL"
    pmwo.save(wid, res, 10, None, ENG)
    with pytest.raises(pm.PmError, match="say what you found"):
        pmwo.complete(wid, None, ENG, "x")
    assert one(box, "SELECT state FROM pm_work_order WHERE wo_id = %s", (wid,))[0] == "IN_PROGRESS"


def test_completing_writes_the_asset_pm_and_raises_findings(fresh):
    box, _ = fresh
    wid = _wo(box)
    res = _answer_all(wid)
    res[0].update(result="FAIL", note="cracked casing")           # a mandatory line: MAJOR
    pmwo.save(wid, res, 25, "all done", ENG)
    d = pmwo.complete(wid, dt.date.today().isoformat(), ENG, "127.0.0.1")
    assert d["state"] == "COMPLETED" and d["ack_state"] == "PENDING" and d["ack_due"] == dt.date.today() + dt.timedelta(days=pmwo.ACK_DAYS)
    assert one(box, "SELECT pm_date, pm_status FROM asset WHERE asset_key = %s AND is_current = 1", (d["asset_key"],))[0] == dt.date.today()
    assert one(box, "SELECT count(*) FROM pm_record WHERE asset_key = %s AND pm_date = %s", (d["asset_key"], dt.date.today()))[0] == 1
    f = d["findings"]
    assert len(f) == 1 and f[0]["severity"] == "MAJOR" and f[0]["call_required"] and f[0]["sr_id"] is None
    with pytest.raises(pm.PmError):
        pmwo.save(wid, [], None, None, ENG)                       # frozen once completed


def test_value_outside_limits_is_a_failure_and_critical_lines_are_critical(fresh):
    box, _ = fresh
    wid = _wo(box, "SERVER")
    res = _answer_all(wid, value=48)                                  # inlet temperature 48 C, limit 35
    crit = next(r for r in pmwo.detail(wid)["results"] if r["critical"])
    next(r for r in res if r["seq"] == crit["seq"]).update(result="FAIL", note="RAID degraded")
    for r in res:
        if r["result"] == "FAIL":
            r["note"] = "RAID degraded"
    d = pmwo.save(wid, res, 60, None, ENG)
    temp = next(r for r in d["results"] if r["kind"] == "VALUE")
    assert temp["result"] == "FAIL"
    temp_note = [dict(r, note="too hot") if r["seq"] == temp["seq"] else r for r in res]
    pmwo.save(wid, temp_note, 60, None, ENG)
    out = pmwo.complete(wid, None, ENG, "x")
    sev = {x["seq"]: x["severity"] for x in out["findings"]}
    assert sev[crit["seq"]] == "CRITICAL" and sev[temp["seq"]] == "MAJOR"


def test_verification_cannot_be_done_by_the_person_who_completed_the_work(fresh):
    box, _ = fresh
    wid = _wo(box, "SERVER", verify=True)
    pmwo.save(wid, _answer_all(wid, value=24), 30, None, ADM1)
    pmwo.complete(wid, None, ADM1, "x")
    out = pmwo.verify([wid], True, "", ADM1)
    assert out["done"] == 0 and "yourself" in out["skipped"][0]["reason"]
    out = pmwo.verify([wid], True, "checked on site", ADM2)
    assert out["done"] == 1
    assert one(box, "SELECT state, verified_by FROM pm_work_order WHERE wo_id = %s", (wid,)) == ("COMPLETED", "ADM2")          # still waiting for the owner
    pmwo.acknowledge([wid], "ACK", "Section Head", "by phone", ADM2, "x")
    assert one(box, "SELECT state, ack_state FROM pm_work_order WHERE wo_id = %s", (wid,)) == ("CLOSED", "ACKNOWLEDGED")


def test_rejected_verification_goes_back_to_the_engineer(fresh):
    box, _ = fresh
    wid = _wo(box, "SERVER", verify=True)
    pmwo.save(wid, _answer_all(wid, value=24), 30, None, ENG)
    pmwo.complete(wid, None, ENG, "x")
    with pytest.raises(pm.PmError):
        pmwo.verify([wid], False, "", ADM1)                                                       # a reason is required
    pmwo.verify([wid], False, "photos missing, redo the cleaning", ADM1)
    assert one(box, "SELECT state FROM pm_work_order WHERE wo_id = %s", (wid,))[0] == "IN_PROGRESS"


def test_owner_acknowledgement_dispute_and_deemed_acceptance(fresh):
    box, _ = fresh
    a, b = [r[0] for r in box.execute("SELECT wo_id FROM pm_work_order WHERE asset_class = 'DESKTOP' AND NOT verify_required AND state = 'OPEN' ORDER BY wo_id LIMIT 2").fetchall()]
    for w in (a, b):
        pmwo.save(w, _answer_all(w), 10, None, ENG)
        pmwo.complete(w, None, ENG, "x")
    # a: owner acknowledges -> closed, and the signer lands on the asset
    pmwo.acknowledge([a], "ACK", "Mr Owner", None, ADM1, "x")
    assert one(box, "SELECT state FROM pm_work_order WHERE wo_id = %s", (a,))[0] == "CLOSED"
    key = one(box, "SELECT asset_key FROM pm_work_order WHERE wo_id = %s", (a,))[0]
    assert one(box, "SELECT pm_signed_by FROM asset WHERE asset_key = %s AND is_current = 1", (key,))[0] == "MR OWNER"
    # b: owner disputes -> back in progress with a finding; needs a reason
    with pytest.raises(pm.PmError):
        pmwo.acknowledge([b], "DISPUTE", "Mr Owner", "", ADM1, "x")
    pmwo.acknowledge([b], "DISPUTE", "Mr Owner", "keyboard still sticky", ADM1, "x")
    d = pmwo.detail(b)
    assert d["state"] == "IN_PROGRESS" and d["ack_state"] == "DISPUTED" and any("disputes" in f["description"] for f in d["findings"])
    pmwo.complete(b, None, ENG, "x")                                                                # redone
    assert one(box, "SELECT ack_state, state FROM pm_work_order WHERE wo_id = %s", (b,)) == ("PENDING", "COMPLETED")
    # silence for ACK_DAYS -> deemed accepted and closed
    box.execute("UPDATE pm_work_order SET ack_due = CURRENT_DATE - 1 WHERE wo_id = %s", (b,))
    assert pmwo.deem_overdue_acks() >= 1
    assert one(box, "SELECT ack_state, state FROM pm_work_order WHERE wo_id = %s", (b,)) == ("DEEMED", "CLOSED")


def test_batch_sheet_passes_the_rest_but_not_work_that_needs_a_reading(fresh):
    box, _ = fresh
    desk = [r[0] for r in box.execute("SELECT wo_id FROM pm_work_order WHERE asset_class = 'DESKTOP' AND state = 'OPEN' ORDER BY wo_id LIMIT 3").fetchall()]
    srv = _wo(box, "SERVER")
    pmwo.save(desk[0], [{"seq": 1, "result": "FAIL", "note": "dented"}], None, None, ENG)          # an answer already given is kept
    out = pmwo.batch_complete(desk + [srv], None, 5, ENG, "x")
    assert out["completed"] == 3 and len(out["skipped"]) == 1 and "reading" in out["skipped"][0]["reason"]
    assert one(box, "SELECT result FROM pm_wo_result WHERE wo_id = %s AND seq = 1", (desk[0],))[0] == "FAIL"
    assert one(box, "SELECT count(*) FROM pm_wo_event WHERE action = 'COMPLETED_BATCH' AND wo_id = ANY(%s)", (desk,))[0] == 3
    assert one(box, "SELECT count(*) FROM pm_finding WHERE wo_id = %s", (desk[0],))[0] == 1


def test_deferral_needs_a_reason_a_sensible_date_and_an_administrator(fresh):
    box, _ = fresh
    wid = _wo(box)
    due = one(box, "SELECT due_date FROM pm_work_order WHERE wo_id = %s", (wid,))[0]
    with pytest.raises(pm.PmError):
        pmwo.defer_request(wid, "", (due + dt.timedelta(days=10)).isoformat(), ENG)
    with pytest.raises(pm.PmError):
        pmwo.defer_request(wid, "user on leave", (due - dt.timedelta(days=1)).isoformat(), ENG)         # must be later
    pmwo.defer_request(wid, "user on leave", (due + dt.timedelta(days=10)).isoformat(), ENG)
    assert pmwo.list_orders(None, bucket="deferral")["total"] >= 1
    pmwo.defer_decide(wid, True, "ok", ADM1)
    assert one(box, "SELECT defer_status FROM pm_work_order WHERE wo_id = %s", (wid,))[0] == "APPROVED"
    with pytest.raises(pm.PmError):
        pmwo.defer_decide(wid, True, "", ADM1)                                                          # already decided


def test_finding_links_only_a_call_that_exists_and_closing_needs_a_note(fresh):
    box, _ = fresh
    wid = _wo(box)
    res = _answer_all(wid)
    res[0].update(result="FAIL", note="bad")
    pmwo.save(wid, res, 5, None, ENG)
    fid = pmwo.complete(wid, None, ENG, "x")["findings"][0]["finding_id"]
    with pytest.raises(pm.PmError, match="No call"):
        pmwo.finding_update(fid, {"sr_id": "NO-SUCH-SR"}, ADM1)
    sr = one(box, "SELECT sr_id FROM svc_call WHERE is_current = 1 LIMIT 1")[0]
    pmwo.finding_update(fid, {"sr_id": sr}, ADM1)
    with pytest.raises(pm.PmError):
        pmwo.finding_update(fid, {"close": True}, ADM1)
    pmwo.finding_update(fid, {"close": True, "note": "replaced the casing"}, ADM1)
    assert pmwo.findings(None, "OPEN")["rows"] == [] or all(r["finding_id"] != fid for r in pmwo.findings(None, "OPEN")["rows"])
    with pytest.raises(pm.PmError):
        pmwo.finding_update(fid, {"severity": "MINOR"}, ADM1)                                           # closed findings are final


def test_an_engineer_sees_and_changes_only_their_own_work(fresh):
    box, _ = fresh
    mine = one(box, "SELECT w.wo_id, a.engineer_name FROM pm_work_order w JOIN asset a ON a.asset_key = w.asset_key AND a.is_current = 1 WHERE a.engineer_name IS NOT NULL ORDER BY 1 LIMIT 1")
    other = one(box, "SELECT w.wo_id FROM pm_work_order w JOIN asset a ON a.asset_key = w.asset_key AND a.is_current = 1 WHERE a.engineer_name IS DISTINCT FROM %s ORDER BY 1 LIMIT 1", (mine[1],))[0]
    assert pmwo.detail(mine[0], mine[1])["wo_id"] == mine[0]
    with pytest.raises(pm.PmError):
        pmwo.detail(other, mine[1])
    with pytest.raises(pm.PmError):
        pmwo.save(other, [], None, None, ENG, mine[1])
    assert all(r["engineer_name"] == mine[1] for r in pmwo.list_orders(mine[1], limit=50)["rows"])
    assert pmwo.list_orders("~no-engineer~")["total"] == 0                                              # a user with no linked engineer sees nothing
    s = pmwo.summary(mine[1])
    assert s["kpi"]["total"] == pmwo.list_orders(mine[1])["total"]


def test_summary_counts_and_pct(fresh):
    box, out = fresh
    wid = _wo(box)
    pmwo.save(wid, _answer_all(wid), 5, None, ENG)
    pmwo.complete(wid, None, ENG, "x")
    k = pmwo.summary()["kpi"]
    assert k["total"] == out["created"] and k["done"] == 1 and k["owner"] == 1 and k["todo"] == out["created"] - 1 and k["pct_done"] == round(100 / out["created"], 1)


def test_cancel_needs_a_reason_and_open_work(fresh):
    box, _ = fresh
    wid = _wo(box)
    with pytest.raises(pm.PmError):
        pmwo.cancel(wid, "", ADM1)
    pmwo.cancel(wid, "asset retired", ADM1)
    with pytest.raises(pm.PmError):
        pmwo.cancel(wid, "asset retired", ADM1)


def test_routes_are_protected_and_admin_only_actions_refuse_a_plain_user(fresh, monkeypatch):
    box, _ = fresh
    c = TestClient(app)
    assert c.get("/api/pmwo/summary").status_code == 401
    fake_user(monkeypatch, role="USER", username="U1", engineer_key="SOMEONE")
    assert c.get("/api/pmwo/summary").status_code == 200
    assert c.post("/api/pmwo/verify", json={"ids": [1]}, headers=HDR).status_code == 403
    assert c.post("/api/pmwo/generate", json={}, headers=HDR).status_code == 403
    assert c.get("/api/pmwo/list").json()["total"] == 0                                             # no work for an engineer who owns no assets


def test_seeding_completes_a_checklist_that_has_no_tasks_and_never_duplicates(box):
    """A set-up run that stopped half-way leaves a checklist header without tasks; the next run must finish it, not skip it."""
    box.execute("DELETE FROM pm_work_order")
    box.execute("DELETE FROM pm_checklist")
    box.execute("INSERT INTO pm_checklist (asset_class, name, version, created_by) VALUES ('DESKTOP', 'Desktop PM', 1, 'half-done')")
    pmwo.seed_checklists(box)
    pmwo.seed_checklists(box)
    assert one(box, "SELECT count(*) FROM pm_checklist WHERE asset_class = 'DESKTOP'")[0] == 1
    assert one(box, "SELECT count(*) FROM pm_checklist_task t JOIN pm_checklist c USING (checklist_id) WHERE c.asset_class = 'DESKTOP'")[0] == len(pmwo.STARTER["DESKTOP"][1])
    assert one(box, "SELECT count(*) FROM pm_checklist")[0] == len(pmwo.STARTER)
    assert pmwo._first({"checklist_id": 7}) == 7 and pmwo._first((8,)) == 8
