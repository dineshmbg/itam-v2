# Implementation Plan: Preventive Maintenance (PM) work-order module

Status: **DRAFT for review** - nothing below has been built. No `tasks/plan.md` or `tasks/todo.md` existed before this one.

## Overview

A full PM module for the ITAM portal on top of the existing quarterly PM (`portal/app/pm.py`): one work order per in-scope asset per financial quarter, with a checklist, findings, a two-step sign-off (technical verification + owner acknowledgement), e-mail alerts, reports and, last, domain login so owners can answer for themselves.

**It was built once already and then removed at the user's request (2026-10-04).** The design, decisions and a verified implementation exist in git: commit `c3da739` (module) and `56cd6c6` (kick-off 40 days), reverted by `8639f07` / `3b770c5`. This plan reinstates that baseline and builds the rest. Source of truth for the design: `samples/PM_Module_Design.md`, `samples/PM_Module_Preview.html`.

## Decisions already agreed with the user (do not reopen)

- Scope: desktops, workstations, printers, scanners, servers, network (switch, router, media converter), UPS. **No laptops.** Quarterly for all, fixed financial-quarter calendar.
- Kick-off e-mail to the assigned engineer **40 days after the quarter starts** (was 60), then the weekly reminder.
- Owner verifies (service acceptance), technical verification by a *different* administrator; owner answer recorded by an administrator until domain login exists; **deemed accepted after 7 days**.
- Photos optional, only on findings. Failed PM -> finding; Major/Critical flagged "call needed"; the SR ID comes from the CIPL tracker, so it is **linked**, not auto-raised.
- Standards: EN 13306 terms, ISO 55001 loop, ISO 27001 A.7.13 (authorised maintenance, records, segregation of duties), ITIL 4 for findings.

## Facts that shape the plan

| Fact | Consequence |
|---|---|
| The six PM work-order tables **still exist in the live DB with real data**: 2,411 work orders, 106 completed by engineer A003550 via the batch sheet, 954 answers, 2,517 events (backup `backups/ongc_ank_20261004_203233_manual.dump`) | Reinstating must keep that data and its DDL must be idempotent. Nothing may be dropped or rewritten without a `CONFIRM DELETE <ID>` phrase. |
| The old code crashed `db/setup.py` (admin connection returns dict rows) and took the live portal down ~13 min | `setup.py`'s PM path needs its own regression test; run `db/setup.py` for real before every handover. |
| Since 10-04 shared helpers moved: `when()` -> `core/format.js`, `toast()` -> `ui/toast.js`, `NOISE` -> `views/common.js` | The reverted views import the old locations; fix imports on reinstatement. |
| Other Claude sessions edit the same folder and dev DB | Commit only your own hunks; push only when the user says so; tests must not assume dev data (179 real PM-done assets exist). |
| Releases are signed `.itamrel` packages built by `deploy/build-release.ps1`; the dev PC cannot reach production | Final phase builds a release and hands over both update paths with the sha256. |
| No domain/LDAP code or library exists; portal runs in a container on an Ubuntu VM | Domain login is a separate, blocked phase needing facts from the user. |

## Architecture decisions

- **Layer, don't replace.** Completing a work order writes the same `pm_date`/`pm_done_by` and `pm_record` row as "Record PM" (`pm.record_in`), so dashboards, snapshots, Past quarters and e-mails keep working.
- **Reuse** the portal's own pieces: `edit.update` (audited, locked asset writes), `mailer` rules (`PM_*` pattern, `notify_log` de-duplication), `export` (xlsx/csv/pdf), `reports.SETS`, `auth.log`, row scoping by `engineer_key`.
- **Frozen checklists:** a work order copies its task list when work starts; later checklist versions never alter past records.
- **Vertical slices:** each task below ends with something a person can use and a test.

## Dependency graph

```
T1 reinstate baseline ──┬─ T2 setup/test hardening
                        ├─ T3 checklist editor ───────────────┐
                        ├─ T4 e-mail rules ─ T5 owner lists    │
                        ├─ T6 asset-page PM history            │
                        └─ T7 findings -> calls follow-up      │
T1 ── T8 dashboard KPIs ── T9 reports + job card ── T10 report-builder dataset
T11 (user inputs) ── T12 LDAPS login ── T13 owner self-service ── T14 security review
all ── T15 docs + release
```

## Task list

### Phase 0 - Gate and baseline
- [ ] T1: Reinstate the verified baseline (needs user go-ahead)
- [ ] T2: Harden set-up and tests

### Checkpoint A - baseline is back, nothing lost
### Phase 1 - Complete the engineer/admin loop
- [ ] T3: Checklist editor (versioned)
- [ ] T4: E-mail rules (assigned, verify queue, owner reminder, overdue)
- [ ] T5: Owner confirmation lists (section head / owner, printable)
- [ ] T6: PM history on the asset page
- [ ] T7: Findings -> call follow-up

### Checkpoint B - the module is usable day to day without domain login
### Phase 2 - Insight
- [ ] T8: Dashboard KPIs by work-order stage
- [ ] T9: PM compliance report, job card and auditor export
- [ ] T10: Work orders in the report builder

### Checkpoint C - reporting complete
### Phase 3 - Domain login (blocked on user inputs)
- [ ] T11: Get the facts from the user
- [ ] T12: LDAPS sign-in as a second identity source
- [ ] T13: Owner self-service page
- [ ] T14: Security review

### Checkpoint D - owners answer for themselves
### Phase 4 - Ship
- [ ] T15: Docs, memory and a signed release

(Full per-task acceptance criteria and verification are in `tasks/todo.md`.)

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| A crash in `db/setup.py` takes the live portal down | High | T2 test + run it for real; keep PM DDL `IF NOT EXISTS` and seed idempotent |
| Reinstating collides with 2,411 existing live work orders | High | T1 starts with a read-only inventory of live data and a backup; no destructive SQL |
| Batch "all Pass" becomes tick-box compliance | Med | Verification sampling (A 100 / B 25 / C 10 %), batch events logged and reported in T8/T9 |
| 2,400 owners will not all answer | Med | Deemed-accepted after 7 days, shown separately; one list per owner/section (T5) |
| Another session edits the same files | Med | Commit own hunks only; re-read files before editing |
| Domain password handled by the portal | High | LDAPS only, never logged or stored, rate-limited; ONGC IT-security sign-off first (T11/T14) |
| Data-dependent tests fail on real data | Med | Tests create and roll back their own rows |

## Open questions (need the user)

1. **Why was the module removed?** If something was wrong (behaviour, naming, scope, speed) change it in T1 rather than reinstating blindly.
2. What happens to the **106 completed and 2,233 open live work orders**: keep as they are (default), or reset before go-live?
3. T11 inputs: is the AD login name the CPF number? Domain controller name/port, LDAPS read-only account (or may the portal bind as the user)? Does ONGC IT-security allow the portal to check domain passwords?
4. Who may verify besides administrators? (Default: administrators only.)
5. Is the production server to receive this in the same release as other pending work?
