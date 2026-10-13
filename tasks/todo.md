# Task list - PM work-order module

Plan: `tasks/plan.md`. Test command: `cd portal; .\.venv\Scripts\python -m pytest -q` (tests run in rolled-back transactions). Front-end build: `cd portal\frontend; node build.mjs`. Never commit another session's hunks; push only when the user says so.

## Phase 0 - Gate and baseline

- [ ] **T1: Reinstate the verified baseline** (M)
  - Ask the user first (plan Open question 1 and 2). Then read-only inventory of the live PM tables; fresh backup.
  - `git revert 8639f07 3b770c5` (or cherry-pick `c3da739` `56cd6c6`), fix moved imports (`when`, `toast`, `NOISE`), rebuild front end.
  - Accept: [ ] `pm_*` work-order code, routes, screens and nav are back; [ ] kick-off is day 40; [ ] the 2,411 live work orders and 954 answers are intact and visible; [ ] `db/setup.py` runs clean against the live DB.
  - Verify: full pytest green except tests already failing on real data (list them); sandbox portal walk-through (list, open, complete, owner accept, batch) with a never-committed transaction; stop it afterwards.
  - Deps: user go-ahead. Files: `app/pmwo.py`, `routes_tools.py`, `scheduler.py`, `db/setup.py`, `pm.py`, 5 views, `main.js`, `shell.js`, `views.css`.

- [ ] **T2: Harden set-up and tests** (S)
  - Add a test that executes the PM part of `db/setup.py` with the admin dict-row connection; make PM tests independent of live data (they delete/seed inside their transaction); fix or mark the real-data tests (`test_closing_a_quarter...`).
  - Accept: [ ] setup crash reproducible in a test before the fix path is trusted; [ ] suite green with 178+ real PM records present.
  - Verify: pytest; `python db/setup.py` twice (idempotent).
  - Deps: T1. Files: `tests/test_pmwo.py`, `tests/test_tools.py`.

### Checkpoint A
- [ ] Suite green, live portal serving, work-order data unchanged (row counts match the inventory), user has seen the pages.

## Phase 1 - Complete the loop without domain login

- [ ] **T3: Checklist editor** (M)
  - Admin can add/edit/reorder tasks and publish a new **version**; old versions retire; work orders already started keep their frozen copy. Existing starter lists are never overwritten.
  - Accept: [ ] draft -> active creates v+1; [ ] a started work order is unchanged; [ ] mandatory/critical/reading limits/every-N editable; [ ] validation (limits, every-N in 1/2/4).
  - Verify: tests for versioning and freeze; browser: edit a checklist, start a work order, see the new task.
  - Deps: T1. Files: `pmwo.py`, `routes_tools.py`, `pm-checklists.js`, `test_pmwo.py`.

- [ ] **T4: E-mail rules** (M)
  - New `mailer` rules: work orders assigned/kick-off per engineer (existing `PM_KICKOFF` text updated), awaiting verification (admins), owner reminder digest, overdue digest. De-duplicated via `notify_log`; each switchable under E-mail and alerts; "Preview" works.
  - Accept: [ ] nothing sent unless e-mail and the rule are on; [ ] same event never sent twice; [ ] silent while a quarter is unclosed (as the existing PM rules).
  - Verify: tests on `mailer.detect` with fixed dates; Preview in the browser.
  - Deps: T1. Files: `mailer.py`, `pmwo.py`, `admin-email.js` (rule list), `test_pmwo.py`.

- [ ] **T5: Owner confirmation lists** (M)
  - Per owner/section: a printable list of the assets serviced this quarter (PDF/Excel via `export`), with a tick/sign column, so a section head can confirm many at once; administrator records the answers in bulk (existing *Owner accepts* with an owner filter).
  - Accept: [ ] filter by owner or section; [ ] export carries work-order numbers and dates; [ ] bulk record logged per work order.
  - Verify: export opens; test for the owner grouping; browser walk-through.
  - Deps: T1. Files: `pmwo.py`, `routes_tools.py`, `pm-work.js`, `test_pmwo.py`.

- [ ] **T6: PM history on the asset page** (S)
  - Asset drawer/page lists its work orders (quarter, stage, who, findings) with links.
  - Accept: [ ] visible to those who can see the asset; [ ] User sees only own assets.
  - Verify: test for scoping; browser check on an asset with a completed work order.
  - Deps: T1. Files: `detail.js` (or asset drawer), `queries.py`/`pmwo.py`, test.

- [ ] **T7: Findings -> call follow-up** (S)
  - Findings page shows the linked call's status from the tracker; a finding whose call is closed prompts closing; repeated findings on one asset flagged.
  - Accept: [ ] call status shown; [ ] only existing SR IDs linkable; [ ] repeat flag after 2 findings in 90 days.
  - Verify: tests; browser.
  - Deps: T1. Files: `pmwo.py`, `pm-findings.js`, test.

### Checkpoint B
- [ ] Full suite green; an engineer can do a quarter end to end (open, batch/individual, findings); an administrator can verify, record owners, defer, cancel; e-mails previewed; user review before Phase 2.

## Phase 2 - Insight

- [ ] **T8: Dashboard KPIs by stage** (M)
  - PM dashboard gains work-order figures: compliance, overdue, awaiting verification/owner, deemed-accepted rate, batch share, finding rate; scoped for Users.
  - Accept: [ ] numbers equal the work-order list counts; [ ] KPI definitions documented on the page (hint text).
  - Verify: test comparing dashboard and list counts; browser.
  - Deps: T1. Files: `pmwo.py`, `dash-pm.js`, `test_pmwo.py`.

- [ ] **T9: Compliance report, job card, auditor export** (M)
  - Excel/CSV/PDF: PM compliance by engineer/class/location; job card PDF per work order (tasks, readings, findings, signatures); auditor pack for a quarter.
  - Accept: [ ] files download with the screen's filters; [ ] job card shows frozen checklist version; [ ] logged `EXPORT_PM_*`.
  - Verify: tests on content; open the PDF.
  - Deps: T1. Files: `pmwo.py`, `routes_tools.py`, `export.py`, `pm-wo.js`, test.

- [ ] **T10: Work orders in the report builder** (S)
  - Add a `pm_work_orders` dataset to `datasets.py`/`reports.SETS` (columns, filters, scoping).
  - Accept: [ ] appears in the builder; [ ] a User sees own assets only.
  - Verify: `tests/test_reports*`-style test; browser.
  - Deps: T1. Files: `datasets.py`, `reports.py`, test.

### Checkpoint C
- [ ] Reports match the screens; user review.

## Phase 3 - Domain login (blocked)

- [ ] **T11: Get the facts** (XS, user)
  - AD login name format (CPF?), domain controller host/port, LDAPS read-only account or bind-as-user allowed, certificate chain for LDAPS, ONGC IT-security approval.
  - Accept: [ ] answers recorded in the plan; [ ] decision on vendoring `ldap3` (pure Python) into `vendor/wheels`.

- [ ] **T12: LDAPS sign-in as a second identity source** (L -> split if needed)
  - Local accounts and administrator 2FA stay (break-glass). Domain sign-in checks the typed password against the DC over LDAPS; never stored or logged; rate-limited; disabled AD account cannot sign in.
  - Just-in-time account for a person found in the HR master (CPF) and not retired; restricted role limited to *their own assets' PM* (no other module).
  - Accept: [ ] works against a test directory (or a mock LDAP in tests); [ ] unknown/retired person refused; [ ] works offline from the container.
  - Verify: tests with a fake LDAP; manual test on the VM with a real account.
  - Deps: T11. Files: new `app/domain_auth.py`, `auth.py`, `routes_auth.py`, `login.js`, `vendor/wheels`, tests (split into 2 tasks if > 5 files).

- [ ] **T13: Owner self-service page** (M)
  - Owner signs in, sees "My assets' PM" this quarter, **Confirm all** or **Dispute** per asset; recorded as the owner themselves (replaces "on behalf").
  - Accept: [ ] owner sees only assets whose owner CPF matches; [ ] section heads see their section; [ ] a self-answer cannot be overwritten by an administrator without a logged reason.
  - Verify: tests for scoping and answers; browser as an owner.
  - Deps: T12. Files: `pmwo.py`, routes, new view `pm-mine.js`, `shell.js`, tests.

- [ ] **T14: Security review** (S)
  - Run the security-review on T12/T13: injection in LDAP filters, brute force, session fixation, privilege of the JIT role, log hygiene.
  - Accept: [ ] findings fixed or accepted in writing; [ ] no credential in logs or DB.

### Checkpoint D
- [ ] Owners confirm for themselves on a test account; ONGC IT-security satisfied.

## Phase 4 - Ship

- [ ] **T15: Docs, memory and a signed release** (S)
  - README section updated; memory entry written; `check.ps1`; `deploy/build-release.ps1`; hand over with **both** update paths and the sha256 (Software update page and `update-portal.sh`), a fresh-backup reminder, and note that the first daily run creates the quarter's work orders.
  - Accept: [ ] signature verified; [ ] release notes mention the new tables and the day-40 kick-off.
  - Verify: full log read; `git status` clean of own work.
  - Deps: all of the above that the user wants in this release.
