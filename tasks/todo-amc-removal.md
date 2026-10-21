# Task list - Remove assets from AMC

Plan: `tasks/plan-amc-removal.md`. Test: `cd portal; .\.venv\Scripts\python -m pytest -q`. Front end: `cd portal\frontend; node build.mjs`. Commit only your own hunks; push only when told.

## Phase 1 - One asset, end to end

- [x] **T1: Removal request tables + single-asset removal by an administrator** (M)
  - Accept: admin picks one asset, reason, effective date, verbal approver -> request `AMCR-...` created and applied; asset is archived (is_current=0 + archive lock, undoable) and gone from the register and every count; change-log row written; asset listed in the "Removed from AMC" list.
  - Done 2026-10-21: 14 tests pass (`tests/test_amcremoval.py`). Not yet exercised in a browser (needs a portal restart and a signed-in session). Team Leader/SI already exists in the portal (`auth.is_lead`, route option `admin="lead"`), so T8 needs no new grant.
  - Verify: new test file in `portal/tests/`; manual in browser.
  - Files: `portal/app/amcremoval.py`, `portal/app/main.py` (routes), `portal/app/edit.py` (reuse only), `portal/tests/test_amcremoval.py`, a view in `portal/frontend/src/js/views/`.
- [x] **T2: Apply effects with snapshot** (S)
  - Accept: rate/cover/pm_status copied to the line, then rate cleared; `amc_removed_rate` integrity check stays green.
  - Dep: T1. Done together with T1 (snapshot + rate cleared in `amcremoval.remove`).
- [x] **T3: Future-dated removal + billing impact** (M) - date >= quarter start and default already done in T1; done 2026-10-21: later day in the running quarter waits as PENDING and is applied by the daily scheduler job; credit = quarterly rate x days left / days in quarter, stored on the line; pending requests can be cancelled; API `/api/amc-removal/impact|requests|cancel` (no requests list screen yet - T10). Mailer 'asset removed' rule now also covers AMC removals
  - Accept: date < running-quarter start rejected; days and credit amount shown per line and total; future date applied by the scheduled task.
  - Dep: T2. Needs open question 1.

### Checkpoint 1
- [ ] All tests pass, front end builds, single removal and its billing figure shown to the user.

## Phase 2 - Many assets, any class/type

- [x] **T4: Archive side-effects check** (S) - confirm an archived asset drops out of PM scope, cover/expiry mails and dashboards; inventory re-import does not resurrect it; PM history untouched. Dep: T2. Done: tests show PM scope, cover mails and dashboard counts drop it; the archive lock the loader reads (`master_db.py:376`) is set; loader run itself not executed.
- [x] **T5: Components removed with the parent** (S) - Dep: T2. Done 2026-10-21: parts leave with the parent (their own rates are credited), a part can also be removed alone, a part added after a pending request still leaves with it, a parent that gets skipped keeps its parts.
- [x] **T6: Basket picker across classes and types with eligibility rules** (M) - page `Data tools > Remove from AMC` (`admin/amc`, Administrators and Team Leader/SI). Checked in the browser pane with a stubbed API fed from real read-only data (1,084 eligible assets); not against the live server.
  - Accept: filters (class, type, make, model, location, engineer, status, cover expiry); 3+ assets of 2+ classes in one request; LAPTOP, WRTY, already-removed, non-current are blocked with the reason shown; open call/RMA = warning needing an override note.
  - Dep: T3.
- [x] **T7: Paste/upload list of CI names / serials / keys** (M) - unmatched and ineligible rows listed, nothing applied until confirmed. Dep: T6. Done 2026-10-21: matches asset key, CI, hostname, serial or ONGC ID; paste, .txt/.csv or .xlsx (first column); explains not found / ambiguous / already removed / same asset twice; adds the good ones to the basket.

### Checkpoint 2
- [ ] Bulk removal of mixed classes verified in browser; PM counts for the quarter drop correctly.

## Phase 3 - Roles, reversal, reporting

- [x] **T8: Team leader/SI grant decides; engineers request; engineers view their removed assets read-only** (M) - new "AMC removal" grant, permission-matrix test updated; removed assets listed in the engineer account outside all counts; same person may approve. Dep: T6. Done 2026-10-21: no new grant (Team Leader/SI = existing `is_lead`). Engineer page `Reports > AMC removals` (own assets only, request -> REQUESTED, withdraw, see decisions, read-only list of removed assets); deciders see 'Waiting for a decision' on the picker and approve (verbal approver, date, override note) or turn down with a reason. Approval re-checks the assets.
- [ ] **T9: Reinstatement request** (M) - un-archives and restores snapshot values, audited, original request marked REVERSED. Dep: T8.
- [ ] **T10: Reports, dashboard tile, README section** (M) - removals per quarter with credit due; `portal/README.md` updated. Dep: T9.

### Checkpoint 3
- [ ] `..\check.ps1` clean; every acceptance item exercised and reported.
