# Implementation Plan: Remove assets from AMC (running quarter)

Status: **DRAFT for review (rev 3: removal = archive)** - nothing below has been built. Separate from `tasks/plan.md` (PM module, unfinished). Task list: `tasks/todo-amc-removal.md`.

## Overview

Today "remove from AMC" is a single field edit (`asset_status = REMOVED_FROM_AMC`, `portal/app/edit.py`) with no reason, no effective date, no approval record, no multi-asset path, and PM scope (`pm.IN_SCOPE`) is not touched. This plan turns it into a controlled, audited **removal request** covering one or many assets of any class/type, aligned with ITIL 4 (change enablement, service configuration management, service financial management) and ISO/IEC 20000-1 / 19770-1 / 27001 (asset inventory, change control; the asset record is never deleted).

## Decisions agreed with the user (do not reopen)

- Approval in real life is **verbal** -> the portal records *who* approved verbally and *when*; no document or vendor reference is stored.
- **Effective date: any date, default = first day of the running quarter** (Q3 FY = 2026-10-01). Backdating is therefore normal.
- Billing is pro-rata. Because removal defaults to quarter start, the portal **tells the user the billing impact** (days, amount) on every request.
- **Same person may request and approve** (no segregation of duties). An Administrator can create-and-approve in one step.
- Users (engineers) may **request** for their own assets; Administrators approve.
- Components (AMC billing lines under a parent) are removed **with** the parent.
- PM history is kept; the asset is simply excluded from PM scope from the effective date.
- Removal is **reversible** through an approved reinstatement request (never a delete).
- **Laptops are outside AMC scope**; warranty (`WRTY`) assets are not eligible.

- Verbal approver must be a **portal user** (picked from the user list). Rate is fixed for the whole contract (3+ years), so the rate on the asset is the rate to credit.
- **Team leader / SI is the deciding authority**: a portal User given an "AMC removal" grant who selects assets and approves; Administrators can do the same. Other engineers can only request.
- **Rate is per quarter**: credit = quarterly rate x (days from effective date to quarter end) / days in that quarter. Effective on quarter start (1 Oct 2026, 92 days) = the full quarter rate.

## Where a removed asset goes (DECIDED by the user)

**Removal = archive.** The repo already has an archive mechanism (`edit.py`: `is_current = 0` plus an `__archived__` lock, undoable, nothing deleted; `queries.py` has an "archived" view). Every register, dashboard, PM and cover figure reads `is_current = 1`, so an archived asset automatically:
- disappears from the Assets register and is **not counted** in the asset total, status chart, PM or cover figures;
- disappears from the assigned engineer's scoped account and counts.

It is **not deleted**: the row, its history and a snapshot of rate/cover/PM state are kept, and it is listed in a **"Removed from AMC" (archived) list**. Engineers can *see* their own removed assets there (view only), outside every count. Reinstatement = un-archive via an approved request.
Calls already raised on the asset keep their history; new calls on it are flagged "not under AMC".
To verify during T1: the archive lock really stops an inventory re-import from bringing the row back, and the mailer rule at `mailer.py:256` (reads `is_current = 0`) does not treat these as "retired" mistakenly.

## Architecture decisions

- New module `portal/app/amcremoval.py` + two tables: `amc_removal` (request header: id like `AMCR-2026Q3-001`, quarter, effective date, requested_by, verbally approved by/date, status REQUESTED/APPROVED/APPLIED/REJECTED/REVERSED) and `amc_removal_line` (asset_key, reason, snapshot of rate/cover/pm_status/asset_status taken at apply time, billing days + amount).
- **Snapshot before clearing**: the integrity check requires a removed asset to carry no rate, so the rate is copied onto the line first. Reinstatement restores from the snapshot.
- Reuse the audited-edit path in `edit.py` so every field change still lands in the change log.
- Reasons are a fixed list: Decommissioned/retired, Surplus, Transferred, Replaced (link new asset), Moved to warranty, Lost/stolen/written off, End of life/support, Contract scope change, Wrongly included.
- Eligibility (per asset): current, record level ASSET, `cover_type='AMC'`, not LAPTOP, not already removed. Warning with override reason: open call or open RMA.
- Effective date >= start of the running quarter (cannot reach into a closed quarter). Future dates are applied by the existing scheduled task.
- Billing impact = rate x (days from effective date to quarter end) / days in quarter. Needs the rate period (see Open Questions).

## Dependency graph

```
T1 tables+single removal -> T2 apply effects (+snapshot) -> T3 effective date + billing impact
T2 -> T4 PM exclusion        T2 -> T5 components
T3 -> T6 basket picker (multi class/type) -> T7 paste/upload list
T2 -> T8 user request / admin approve -> T9 reinstatement -> T10 reports + docs
```

## Task List

See `tasks/todo-amc-removal.md` (T1-T10, three checkpoints).

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Backdated removal changes an invoice already paid | High | Show billing impact before approval; list shows "credit due" per request |
| Rate period (yearly vs quarterly) unknown -> wrong credit figure | High | Open question 1; until answered, show days only, no amount |
| Removing a parent leaves component lines billing | Med | T5 removes them together; integrity check stays |
| Wrong asset in bulk list | Med | Paste/upload shows unmatched + ineligible rows, nothing applied until confirmed |
| Parallel sessions edit `edit.py` / `pm.py` | Med | New module; touch shared files minimally; commit only own hunks |

## Open Questions

1. Assumed: an engineer sees their removed assets read-only; only a team leader/SI or Administrator can reinstate. Correct?
2. Assumed: one "AMC removal" grant (name it) is enough for team leaders/SI; no separate role. Correct?
