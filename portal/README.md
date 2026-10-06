# ITAM Portal

Locally hosted, offline web portal over the PostgreSQL inventory (`ongc_ank`): live dashboards, editable registers, preventive maintenance, reports, data import, backup and restore, e-mail alerts and user management. Everything is served from this folder - no CDN, no internet at run time.

## At a glance (updated 2026-10-05)

**What it is.** ONGC Ankleshwar's IT asset management system: a login-protected, fully offline web app - Starlette API, vanilla-JavaScript front end bundled by esbuild (IBM Plex type, Carbon icons, light/dark/automatic themes) - over one PostgreSQL 18 database, `ongc_ank`. It holds the asset register (whole assets and component lines), service calls, spare-part Inward/Outward lines, OEM RMA cases, the CIPL engineers/roster, preventive maintenance (PM) and the audit trail. Python 3.14; packages are vendored in `vendor/wheels`, so nothing is ever downloaded at run time.

**Where it runs**

| Environment | What it is |
|---|---|
| Development PC (this repo) | Windows. The portal runs as the Scheduled Task **ITAM Portal** on port 8420 against a local dev database. All work, tests and release builds happen here. It cannot reach production. |
| Production server, `10.205.64.47` | Windows Server 2019. PostgreSQL 18 runs natively here and holds the real data; scheduled backups. |
| Production VM, `10.205.64.25` | Hyper-V Ubuntu VM on that server. The portal runs here as **one Podman container** (HTTPS), talking to the database on the server. Updated with signed release packages. |

**Menu** (what each page is for): *Dashboards* - Assets, Call tracker, Engineers. *Registers* - Assets, Calls, Inward, Outward, OEM RMA. *People* - Engineers. *Preventive maintenance* - PM dashboard, PM worklist, Past quarters, Cycles and snapshots. *Reports* - Report builder (Excel/CSV/PDF). *Control* - Data integrity, Change log. *Data tools* - Data import, Backup and restore, Software update. *Administration* - Users and security, E-mail and alerts, Activity log. Every register can be searched, filtered, sorted, exported to CSV and laid out per person; a record opens in a floating detail window.

**Who can do what** - two groups, **Administrator** and **User**, plus a read-only demo login and optional per-person grants for a User (Calls/Inward/Outward/RMA access, Asset access, Extended access). The full table is in "Who can do what" below and is executable (`tests/test_permission_matrix.py`). In short: a User sees and edits only the assets assigned to them (everything except the Contract and Lifecycle fields); Control, Data tools and Administration are for real administrators only.

**Safety nets** (each is described in its own section below)
- Every change is validated, written to the change log and audit trail, and a manual edit is remembered so a later import cannot silently overwrite it.
- Nothing is deleted from the portal: Archive hides a record; Replace/Redeploy keep a retired machine's history.
- Every import is **rehearsed first** (the check runs the real load and rolls it back), has safety stops against mass removals, and takes an automatic backup before it loads.
- Inward/Outward/RMA lines are matched by what they are, not by the spreadsheet's serial column.
- Daily automatic backup (second copy on another disk when one exists), a **restore drill** that proves a backup really restores, and a signed one-click software update with automatic rollback.
- Sign-in: scrypt hashes, lock-out, per-address throttle, optional two-factor, idle time-out, optional expiry of unused starting passwords.

**Everyday commands**
```powershell
cd portal; .\run_portal.ps1                       # run in a terminal (the Scheduled Task does this permanently)
.\check.ps1 [-Drill]                              # from the project root: is it safe to release? uncommitted work, front-end build, every test, optional restore drill
cd portal; .\.venv\Scripts\python -m pytest -q    # the tests (about 540); they never change real data
cd portal\frontend; node build.mjs                # rebuild the front end after any change under frontend\src
python portal\db\restore_drill.py                 # restore a fresh dump into a scratch database and compare it with the live data
.\deploy\build-release.ps1 -Notes "..."           # signed release package; needs a clean (committed) tree and Docker Desktop running
```

**Releasing.** Commit everything, run `.\check.ps1`, then `.\deploy\build-release.ps1`; it refuses to build from a dirty folder, runs the tests, builds the container image, signs the package and records the commit id in the signed notes. Carry `deploy\releases\itam-release-<version>.itamrel` to the VM and install it from *Data tools > Software update*, or run `sudo ./update-portal.sh releases/itam-release-<version>.itamrel` there. Take a fresh backup first; if the new version fails its health check the updater rolls back by itself. At the time of writing the newest package is `itam-release-20261004-1520.itamrel`, built from commit `879a3e0` (see `deploy/README.md`, "Updating").

**Scheduled work inside the portal.** Daily automatic backup (default 02:00, keeps the newest 14); PM cycle bookkeeping daily and a PM snapshot every Monday; the e-mail rules (off until a mail server is configured and a rule is switched on).

**Not built / known gaps** (so nobody assumes otherwise): ownership/custody history, document attachments (invoices, contracts), software and licence tracking, bulk edit, per-call SLA alerts, a scan-to-verify mobile flow, an "undo this load" button, automatic disabling of leavers' accounts, hosted CI and browser tests (the tests read the dev database's real data, so a hosted runner has nothing to test against), a measured accessibility audit (WCAG 2.2 AA is the design target, not a measured result), and an alert if the production portal goes down.

**How to read the rest of this file.** The sections from "Who can do what" to "Rules built in" describe the current behaviour. The dated sections after that are the change history in the order things happened; where a later section changes an earlier one, **the later one wins** (for example, everyone now lands on the Call tracker after sign-in, and the OS family list is the closed list from 2026-09-23).

## Run

```powershell
cd portal
.\run_portal.ps1            # first run builds .venv from vendor\wheels (offline), prepares the DB, opens http://127.0.0.1:8420
```
Options: `-Port 9000`, `-NoBrowser`, `-SkipSetup`. The database password is read from `~/.ongc_pgpass` (or `PGPASSWORD` / pgpass.conf); it is never stored in the project.

## Run automatically (recommended)

The commands above run the portal only for as long as that terminal (or session) is open. For a permanent setup that survives reboots and doesn't depend on any terminal:

```powershell
cd portal
.\service\install.ps1
```

This registers a Windows Scheduled Task named **ITAM Portal** that:
- **Starts the portal when you log in to Windows** (no admin rights needed - it runs as your own account, so it reads `~/.ongc_pgpass` and `~/.itam_smtp_secret` exactly as before).
- **Self-heals within about a minute if the portal ever stops** (crash, `Stop-Process`, PostgreSQL restart, etc.) - a repeating trigger checks every minute and starts a fresh instance if none is running. Task Scheduler's own "restart on failure" setting was tested and found unreliable on this machine (it did not fire after a forced kill), so the task instead uses this 1-minute watchdog trigger, which was verified end-to-end to self-heal correctly. `run_hidden.ps1` also frees port 8420 itself before starting, in case a previous instance was not cleanly reaped by Task Scheduler.
- Has no execution time limit (a normal task would be killed after 3 days; this one runs indefinitely).

PostgreSQL already runs as its own Windows service (`postgresql-x64-18`) and starts at boot on its own - only the portal process itself needed this.

Useful commands:
```powershell
Get-ScheduledTask -TaskName 'ITAM Portal' | Get-ScheduledTaskInfo   # status, last run time/result
Start-ScheduledTask -TaskName 'ITAM Portal'                          # start it right now
Stop-ScheduledTask  -TaskName 'ITAM Portal'                          # stop it (the watchdog will restart it within ~1 min unless also disabled)
Disable-ScheduledTask -TaskName 'ITAM Portal'                        # stop it and keep it stopped
.\service\uninstall.ps1                                              # remove the task entirely (portal files/data untouched)
```
Logs: `service\logs\portal.log` (rotated to `portal.log.old` once it passes 10 MB).

**First sign-in.** `db/setup.py` creates one administrator (`ADMIN`) with the fixed default password `*ongc123` (`auth.DEFAULT_ADMIN_PASSWORD`), prints it once and saves it to `%USERPROFILE%\.itam_first_admin.txt`. You must change it at first sign-in; delete the file afterwards.

## Who can do what

| | Administrator | User | DEMOUSER |
|---|---|---|---|
| Assets dashboard | yes, whole fleet | yes, own assets only - unless individually granted `asset_access` READ/FULL, see below (2026-09-30) | yes, whole fleet (read-only) |
| Call tracker dashboard | yes, whole fleet | yes, own calls only | yes, whole fleet (read-only) |
| PM dashboard | yes, whole fleet | yes, own assets only | yes, whole fleet (read-only) |
| Report builder | yes, every dataset, unscoped | yes, but scoped the same as the matching register/dashboard | yes, everything (read-only) |
| Data integrity | yes | **no access at all** (2026-09-23) | yes, everything (read-only) |
| Assets register, hover cards | yes | yes, own scope only - unless individually granted `asset_access` READ/FULL, see below (2026-09-30) | yes, everything (read-only) |
| Engineers register, hover cards | yes, any engineer | yes, list of everyone, but a hover card/floating window only for themselves | yes, everything (read-only) |
| Employees register | yes | yes, own scope only | yes, everything (read-only) |
| Calls, Inward, Outward, OEM RMA registers | yes | **no access by default** - unless individually granted read-only or full access, see below (2026-09-22, extended 2026-09-23) | yes, everything (read-only) |
| Engineers dashboard, Change log, Cycles and snapshots | yes | **no access at all** (2026-09-22) | yes, everything (read-only) |
| Assets: change any field except Contract/Lifecycle | yes | yes, only on assets assigned to them (2026-10-02 - was status/cover/PM only) | no |
| Assets: change Contract or Lifecycle fields (cover, rate, purchase, vendor, PO, refresh-due) | yes | no, unless individually granted `asset_access=FULL`, see below | no |
| Assets: add, archive | yes | no, unless individually granted `asset_access=FULL`, see below (2026-09-30) | no |
| Engineer record: any field | yes, any engineer | yes, but **only their own record** (2026-09-23 - was contact fields only) | no |
| Engineer record: create | yes (2026-09-23 - was roster sync only) | no | no |
| Engineer record: archive | no one - roster sync/events only | no | no |
| Record PM, PM worklist | yes | yes, own assets only | no |
| Past quarters (closed PM quarters) | yes, whole fleet | yes, only assets that were on their own engineer key when the snapshot was taken | yes, everything (read-only) |
| Replace asset / Redeploy asset | yes (real administrators only - not extended access) | no | no |
| Personal data on hover cards (mobile, personal e-mail, date of birth) | yes | only their own | no |
| Data import, backup and restore, PM roll-over, roster events | yes | no | no |
| Users, security policy, e-mail set-up, activity log | yes | no, except a **Team Leader/SI** User: Users and security (other plain Users only, no security policy / roster sync) and Activity log - see "Team Leader/SI tools" | no |
| Create a portal account | yes, manually (Add user) or via roster sync (2026-09-30 - Add user restored) | never | never |

**The table above is also executable** (2026-10-03): `tests/test_permission_matrix.py` runs 9 kinds of account (administrator, plain User, +calls/parts read or full, +asset read or full, extended access, User with no engineer, read-only demo) against ~20 sensitive actions through the real HTTP routes, plus what each account sees in the Assets register and which datasets the report builder offers. Change a rule by changing that table; add a grant by adding an account row. It fails if any route disagrees (checked by deliberately loosening a rule: it caught it).

**Calls/Inward/Outward/OEM RMA access for a User (`portal_user.call_parts_access`, 2026-09-23):** set from *Manage user* to `NONE`
(default), `READ` or `FULL` - one setting covers all four registers together, not per-module. `READ` can view and search those
registers but not add/edit/archive; `FULL` can also add and edit (never archive - that stays administrator-only, same as assets).
An administrator's access to these is never affected by this setting.

**Extended access for a User (`portal_user.extended_access`, 2026-09-25/26):** a tick box in *Manage user*, User group only. The account
still shows as a User, but works like an administrator in Dashboards, Registers (all of them, unscoped, including Calls/Inward/Outward/OEM RMA),
People, Preventive maintenance (including Cycles and snapshots) and Reports. It never gets Control (Data integrity, Change log), Data tools
(Data import, Backup and restore, Software update) or Administration: those routes use `admin="strict"` (`auth.is_full_admin`), and the menu
hides them. `auth.session_user()` sets the effective `role` (ADMIN for such an account) and keeps the real one in `group`. It does not count as an
administrator for the last-administrator check or for required two-factor sign-in.

**Asset access for a User (`portal_user.asset_access`, 2026-09-30):** set from *Manage user* to `NONE` (default), `READ` or `FULL` -
deliberately narrower than `extended_access`, which conflates "sees everything" with "can edit everything" across every register, not
just Assets. `READ` unscopes the Assets dashboard, register and report builder (whole fleet, not just assets assigned to them) but grants
no extra editing beyond the blanket User rule below. `FULL` also bypasses the ownership check entirely in `check_edit`/`check_create`/
`check_archive`/`check_verify`, so the account can add, edit (including Contract/Lifecycle fields), archive and verify any asset, as if
every asset were assigned to them. Neither value touches Engineers, PM, or anything outside Assets - unlike `extended_access`, which does.

**What a plain User can edit on an asset (`auth.ASSET_LOCKED_FIELDS`, 2026-10-02 - supersedes the narrower status/cover/PM-only rule from
2026-09-22):** any field on an asset already assigned to them, **except** the Contract group (cover type, cover ends, rate component, rate
value) and the Lifecycle group (purchased on, purchase cost, vendor, PO no., refresh due) - those stay administrator-only for every User,
with no per-person toggle (an earlier, since-reverted attempt built this as a per-engineer grant; the user asked for it blanket instead).
This includes CPF No. and Engineer, so an engineer who already holds an asset can hand it to a new owner (and a new engineer, if that
person is in someone else's territory) without an administrator - the asset must still be assigned to them already; this only widens
**which fields** are editable, never **which assets**. `routes_edit.py`'s `schema()` route computes the same field set for the edit form's
`readonly` flags as `auth.check_edit` enforces server-side - both have to agree, or a field can be technically permitted but never
actually offered in the UI (or vice versa); see the schema-level tests in `tests/test_edit.py` for the regression coverage this needs.

**A group set by hand survives roster sync (`portal_user.role_locked`):** changing a group in *Manage user* locks it, so *Sync from roster*
no longer derives it from the designation (e.g. a TEAM LEADER/SI moved to User stays a User).

**Row-level scoping for a User account (`app/queries.py`'s `scope_for`):** a signed-in User only sees Assets, PM worklist and Employees rows
connected to them - Assets/PM: `engineer_name` matches their linked `engineer_key`; Employees: the employee is the HR owner (`cpf_no`) of one of
those assets. An Administrator (and the read-only demo account, which carries `role = ADMIN`) sees everything, unscoped. Filter/facet option
counts on those three registers are computed against the same scoped subset, so a User only ever sees options and counts that exist within their
own data - never the whole table's. A User whose account has no `engineer_key` linked sees an empty list, not an error.

**The DEMOUSER account** (`auth.DEMO_USERNAME` / `auth.DEMO_PASSWORD`, both fixed as `DEMOUSER` / `demouser`) is a standing walkthrough login:
`role = ADMIN` so it sees the full, unscoped admin view of every register and dashboard, but `read_only = TRUE` makes every write route
(`app/web.py`'s `write()`) refuse it outright with a 403 before the request reaches any handler - nothing it does can ever change data. It never
forces a password change, never expires, and is excluded from the "last active administrator" safety count. Created idempotently by
`auth.ensure_demo_user()`, called from `db/setup.py` on every run.

**Account creation: roster sync, or manual (`Add user`, restored 2026-09-30).** `POST /api/admin/users/sync` (*Sync from roster*) remains the way
to create a login for everyone on the active CIPL roster, one per row, username = ECODE. For anyone not on that roster - a general ONGC
employee, say - *Add user* (`POST /api/admin/users/create`, `admin="strict"`) creates a single account by hand: username, full name, e-mail and
group are typed in, an optional engineer link can be set the same as in *Manage user*, and a one-time temporary password is shown (the account
must change it at first sign-in, same as any other new login). This route previously existed, was removed entirely (no HTTP path, `auth.create_user()`
only reachable from roster sync), and was restored at the user's request - a manually created account with no `engineer_key` linked gets the same
"sees nothing" row-scoping as any other engineer-less User account (`scope_for`'s `("false", [])` fallback), so it never has broader default access
than a roster account would.

**Read-only accounts still browse normally, extended 2026-09-22.** `app/web.py`'s `write()` takes a `mutates=False` flag for POST routes that
carry a body but change no portal data: session keep-alive, page-view activity logging, running/exporting a report, and downloading a
dashboard. Only those are exempt from the read-only block - saving a report, sharing one by e-mail, and every data-editing route still refuse
a read-only account. Get this wrong in either direction and either DEMOUSER silently times out mid-walkthrough (keep-alive blocked) or a demo
account can persist real changes - both were caught and fixed by test, see `tests/test_scoping.py`.

## After sign-in, cascading forms, and per-person layout (2026-09-22)

- **Default dashboard**: originally admin -> Call tracker, everyone else -> Assets; **now everyone lands on the Call tracker on every sign-in** (see "Landing page and clean sign-out" below). `router.js`'s `defaultPath()` is used only when no page is requested.
- **New asset form cascades**: Class limits Type's suggestions to types already used for that class; Make suggests every distinct make already
  in the data; Model narrows to that make's models; OS family is a fixed list (Windows 11/10, Windows Server, macOS, Linux, Other) and
  Operating System suggests values already recorded for that family. Hostname defaults to the asset key until the person types their own.
  The user and engineer fields are type-ahead by name or ID. All suggestions are freely overridable text, not closed lists - a genuinely new
  make/model/type is not blocked. Backed by one whitelisted endpoint, `GET /api/edit/assets/lookup?field=<name>&filter=<value>`
  (`app/routes_edit.py`'s `LOOKUP_FIELDS`), and a new `edit.py` field kind `"suggest"` (`frontend/.../ui/form.js`'s `suggest()` control).
  On save, if the new asset has an engineer assigned and SMTP is configured, that engineer gets a notification e-mail
  (`routes_edit.py`'s `_notify_engineer_of_new_asset` - best-effort, a mail failure never fails the create).
- **Per-person column layout**: every register's "Columns" toolbar button opens a dialog to show/hide columns and reorder them with arrows.
  Saved against the signed-in account (new table `portal_view`, `app/views_pref.py`, routes `GET/POST /api/views/{name}[/save|/reset]`), so it
  follows a person to any machine - not a browser-local setting. "Reset to default" clears the saved row and falls back to the register's own
  column order.
- **Report builder blank columns**: "Add a blank column" appends a custom-heading column with no data of its own, for the reader to fill in by
  hand (a sign-off, a remark). Pure a `(key, label)` pair appended after the real columns in `reports.py`'s `build()` - every export writer
  already treats a row missing a column's key as blank, so no SQL or row data is involved.
- **Personal data (mobile, personal e-mail) is now consistently admin-only across the whole app**, not just on hover cards: the engineer
  register's edit form, the engineers register's read view, and the engineer dashboard's detail drawer all now show it to an administrator (or
  the engineer looking at their own record) and hide it from everyone else, via `queries.py`'s `_clean(row, expose_private)`. This also fixed
  a real bug: editing your own engineer record used to show mobile number and personal e-mail as blank even though a value existed, because
  the field was stripped from the server response before the edit form ever saw it.
- **Engineers register**: hovering the Name column (not just ECODE) now shows the hover card (`datasets.py`'s `display_name` column gained
  `ref="engineer"`). Archive/Restore never showed for engineers to begin with (roster-sync only) and still don't.
- **Filter panel**: heading renamed from the terse "Filters · N" to "Narrow down results" with an "N filters applied" line, and all its text
  is 2px smaller throughout, per request.
- **Fixed a real, reproducible bug**: checking/unchecking a facet (reported against Make) could blank the whole page, needing a refresh to
  recover. Two independent causes, both in `frontend/src/css` and `ui/facets.js`: (1) after a facet redraw, restoring keyboard focus to the
  still-checked checkbox used a plain `.focus()`, which some browsers use to auto-scroll the *whole document* rather than just the internal
  panel - fixed with `.focus({ preventScroll: true })`; (2) the facets panel (and other internal scroll panels - `.main`, `.gt-scroll`, `.nav`,
  drawers, modals) let mouse-wheel scroll chain into the outer document once their own content was exhausted, pushing the entire app off
  screen - fixed with `overscroll-behavior: contain` on each of them.

## Naming, new-call defaults, Inward/Outward, and the User-group lockdown (2026-09-22, later the same day)

- **"Parts received" is now "Inward"; "Faulty parts sent" is now "Outward"** everywhere - nav, page headers, report builder, data-quality/import
  labels, error messages (e.g. "cannot archive: still has 2 inward lines"). The underlying dataset keys (`inward`/`outward`) and tables
  (`spare_inward`/`spare_outward`) were already named that; only display text changed.
- **Register subtitles dropped "from the live PostgreSQL register"** (an implementation detail, not something a reader needs) in favour of
  "— updates live as records change"; the nav footer similarly reads "Live view of the IT asset inventory."
- **New call**: "Logged by CIPL" defaults to today (still editable); Zone/Site/Site in-charge default to WEST / ONGC ANKLESHWAR / TEAM
  LEADER/SI. Typing or picking an Asset (CI) auto-fills User CPF no. and Engineer from that asset's current record (only fields still empty
  are touched, and only on create) - reuses `GET /api/card/asset/{key}` (the same data the hover card already shows), no new endpoint.
  New Inward/Outward records default their own date field to today too.
- **Call detail page**: an "Inward" button appears top-right while the call is OPEN and it has no inward line yet; "Outward" appears while
  CLOSED and it has no outward line yet. Either opens the normal New Inward/Outward window pre-filled with this call's SR ID and asset, and
  disappears the moment a line exists for it (`p.related.inward`/`outward`, already returned by the call's own detail payload) - after that,
  the line is only editable from the Inward/Outward register itself, same as anything else.
- **Engineers can no longer see each other's floating-window (hover card) details** - only their own; an admin still sees anyone's. Fixed in
  two places: `cards.py`'s `card()` route 404s a non-admin's request for another engineer's card, and `_engineer()` itself now decides
  personal-field exposure (mobile, personal e-mail) as `admin OR viewing their own card`, not `admin` alone - the first pass only added the
  outer 404 and missed that the inner personal-data check still needed the same exception, which a test caught. The Engineers *register*
  itself (the list) is unaffected - still browsable, with the signed-in engineer's own row highlighted (`row-self` CSS class, `ui/table.js`'s
  new `rowClass` option).
- **A User account's Assets and Call tracker dashboards are now scoped to themselves** - "my assets" / "my calls", not the whole fleet.
  `dashboards.py`'s `assets(eng=None)`/`calls(eng=None)` take the viewer's own `engineer_key` (`None` for an admin = unscoped) and thread it
  through every aggregate query via a named `%(eng)s` parameter appended to each query's own base filter - every breakdown (by class, by
  status, ageing, SLA, parts flow, RMA, the 30-day daily chart, etc.) is scoped, not just the headline KPI numbers.
- **A User account now has no access at all** (not just row-scoped - the register/dashboard is unreachable) **to: Calls, Inward, Outward and
  OEM RMA registers, the Engineers dashboard, the Change log, and Cycles and snapshots.** This is a deliberate narrowing from the original
  design (where Calls/Inward/Outward/RMA were open to everyone). Enforced at three points so hiding the nav link is not the only thing
  stopping access: `queries.check_access()` (register list/detail, called from `main.py`), `auth.check_edit/check_create/check_archive`
  (the edit routes), and `admin=True` on the `dash/engineers`, `audit` and `pm/cycles` route registrations - plus `queries.search_all()`
  leaves those datasets out of a non-admin's search results, so a lucky search hit never dead-ends into a 403. The shared list, deliberately
  kept in `datasets.py` (not `auth.py` or `queries.py`, to dodge a circular import), is `ADMIN_ONLY_DATASETS = {"calls","inward","outward","rma"}`.

## Freshness, form auto-fill, and full engineer self-edit (2026-09-23)

- **A tab left open across a deploy now recovers itself.** A hash-only route change (`#/registers/assets` -> `#/registers/calls`) never
  re-fetches anything in a browser - only a real navigation does - so a portal tab that was open when a new version was deployed would
  otherwise go on running the old JS indefinitely. Fixed at the one moment a full reload is actually safe and expected: `main.js`'s `gate()`
  now sets a `freshLogin` flag the instant any part of the sign-in flow shows (the login form, 2FA, a forced password change, forced 2FA
  setup) and, when state next reaches `'ok'`, calls `location.reload()` instead of mounting the app in place - so a real login always lands
  on whatever is currently deployed. A session that was already valid when the tab opened never sets the flag, so it is not reloaded in a
  loop. `index.html` already had `Cache-Control: no-cache` (verified correct - Starlette's `FileResponse` adds `Last-Modified`/`ETag` so a
  reload properly revalidates and picks up new hashed bundle names) - the missing piece was that nothing was ever triggering a reload at all.
- **A "today" default must never be read from the cached schema.** `getSchema()` fetches once and keeps that response for the rest of the
  browser session, so a date baked into the schema response would go stale the moment midnight passed while the tab stayed open - caught by
  the user, not found in testing. Every date field that defaults to "today" (`cipl_call_date`, `inward_date`, `received_date`,
  `outward_date`, `sent_date`) is therefore recomputed in the *browser's own* local time, fresh, at the exact moment the New-record window
  opens (`record.js`'s `TODAY_DEFAULT_FIELDS` + `todayISO()`), never taken from `spec.defaults`.
- **New call**: Site in-charge now defaults to the actual *name* of whoever currently holds the Team Leader/SI designation (previously the
  literal text "TEAM LEADER/SI") - `edit.py`'s `_team_lead_si_name()` looks up `cipl_employee` by designation (`ILIKE '%team lead%' AND
  ILIKE '%si%'`, tolerant of "Leader" vs "Lead" spelling) each time `schema()` is built. Same lookup fills Inward's "Received by".
- **Part no. is now a suggestion dropdown, not free text** - on the call's "Part no. required" and Inward's "Part no.", scoped to that
  specific asset's own part history (`spare_inward.part_no` where `asset_key` matches), via the same generalised `/api/edit/assets/lookup`
  endpoint used for the new-asset form's Make/Model/Type suggestions - `routes_edit.py`'s `LOOKUP_FIELDS` now carries a table name per
  field, not just column/filter-column, so it can query `spare_inward` as well as `asset`.
- **New Inward**: Received on defaults to today. **New Outward**: Sent on defaults to today; Sent to defaults to "CIPL WARE HOUSE";
  Location defaults to "NOIDA".
- **A non-admin engineer can now change every field on their own engineer record**, not just mobile/company e-mail/personal e-mail as
  before - `auth.check_edit`'s field-level restriction for the `engineers` dataset was removed entirely (the now-unused
  `ENGINEER_SELF_FIELDS` constant went with it); only the ownership check remains (`user.engineer_key == key`), so someone else's record is
  still always refused. `routes_edit.py`'s `schema()` no longer marks any engineer field `readonly` for a non-admin, for the same reason.
- **PM dashboard is now scoped to a User's own assets**, the same pattern as the Assets/Call tracker dashboards: `pm.dashboard(eng=None)`
  takes the viewer's own `engineer_key` and threads it through every asset-level query via a named `%(eng)s` parameter - the cycle/quarter
  metadata (dates, kickoff, snapshot history) stays unscoped, since it is the same for everyone regardless of who is looking.
- **Data integrity is now administrator-only**, same enforcement pattern as the other admin-only pages (`admin=True` on the route, `true` on
  the nav entry).
- **Side panel signature removed; the designer's name now sits under the "ITAM Portal" heading itself**, small and in a rainbow
  `linear-gradient` text-clip, left-aligned with the wordmark - `ui/shell.js`'s `.brand` gained a `.brand-text`/`.brand-line`/`.brand-sig`
  structure, and `--hdr-h` grew from 48px to 56px to fit the second line (every layout calc that depends on it already reads the CSS
  variable, so nothing else needed manual adjustment).

## Engineer records, module access grants, and new-asset defaults (2026-09-23, later still)

- **Administrators can now add an engineer directly**, not only via CIPL roster sync - a "New engineer" button appears on the
  Engineers register for admins (`schema().can_create.engineers`). `edit.create_engineer()` gives it a synthetic ECODE
  (`PORTAL-0001`, `PORTAL-0002`, ...) so it joins `cipl_employee`/`portal_engineer` exactly like a roster-sourced one, tagged
  `source = 'PORTAL'` so a later real roster sync of the same person is never auto-merged into it. New fields: bank name/account
  number/IFSC, EPFO/UAN number, ESIC number, and shirt/trouser/jacket size (free-typed, inch/cm or S-XXXL, whichever the person
  writes down - `uniform_shirt_size` etc. on `cipl_employee`, never touched by the roster loader). All of these plus mobile/personal
  e-mail/date of birth are admin-or-self only (`datasets.NEVER_EXPOSE`).
- **A User can now be individually granted Calls/Inward/Outward/OEM RMA access**, read-only or full, from *Manage user* -
  `portal_user.call_parts_access` (`NONE`/`READ`/`FULL`, one setting covers all four registers, not per-module). Wired through
  every layer that previously hard-blocked these for non-admins: `queries.check_access`/`search_all` (view + search), `auth.check_edit`/
  `check_create`/`check_archive` (`FULL` only), `schema().can_create`, and the nav (`session.hasCallPartsAccess()` in `shell.js`).
  An admin still sees and edits everything regardless of this setting.
- **New-asset form defaults**: OS family defaults to WINDOWS, Operating system to WINDOWS 11 PRO, Installed on to today. Asset
  (CI) now fills automatically **from** Hostname as you type it (reversed from the previous hostname-follows-CI direction - typing
  a CI number directly still overrides it, same soft-default pattern as before). Typing a Type now suggests a Class, when every
  existing asset of that Type happens to share exactly one (ambiguous or unknown Types leave Class alone).
- **OS family is now a closed list**: WINDOWS, WINDOWS SERVER, LINUX, MACOS, OTHER (previously separate "WINDOWS 11"/"WINDOWS 10"
  entries - existing rows migrated to WINDOWS by `db/setup.py`). **Operating system now suggests editions per family** (e.g.
  WINDOWS -> Windows 11/10 Home/Pro/Enterprise/Education, WINDOWS SERVER -> 2025/2022/2019/2016/2012 R2 Standard/Datacenter, LINUX
  -> Ubuntu/RHEL/Rocky/CentOS/Debian/SUSE/Fedora, MACOS -> Sequoia/Sonoma/Ventura/Monterey/Big Sur/Catalina) - a fixed offline list
  (`edit.OS_EDITIONS`), not a server lookup, since the production server has no internet; the field stays freely typable for
  anything not on the list. `form.js`'s `suggest()` control gained a third suggestion source (`f.values_by`, a static list keyed by
  another field's current value) alongside the existing flat static list and server-lookup modes.

## Landing page and clean sign-out (2026-09-23, evening)

- **Everyone lands on the Call tracker dashboard after signing in** - administrators and Users alike (a User's is scoped to their own
  calls). Before this, Users landed on Assets and the page returned to whatever route the tab had last shown. `main.js`'s `gate()` now
  rewrites the URL to `#/dashboard/calls` just before the post-login reload; `router.js`'s default and the logo link agree.
- **Signing out, an idle time-out, or an expired session reloads the whole page to a clean login** (`wasSignedIn` in `main.js`): the URL
  is cleared and nothing from the previous session - route, open drawer, cached schema, script state - survives. The reason ("signed
  out because of inactivity" / "session ended") is carried across the reload in `sessionStorage` only to show one message.
- **Suggestion boxes (Type, Model, Operating system, ...)** offer their whole list again when you click into a field that already
  holds a value (the old value shows as a placeholder and returns if you leave without choosing) - a browser datalist otherwise only
  shows options matching what is typed. **Changing the field a suggestion depends on clears it** (Class -> Type, Make -> Model, OS
  family -> Operating system) and loads a fresh list for the new value (`form.js`).
- **Choosing a Class fills Type** when there is no real choice: exactly one Type is already used for that Class (ROUTER -> ROUTER,
  PRINTER -> PRINTER, SCANNER, LAPTOP, MEDIA_CONVERTER -> MEDIA CONVERTOR), or none yet (then the Class name itself). Classes with several
  Types (DESKTOP, SERVER, SWITCH, UPS, WORKSTATION) leave Type blank with the fresh list. The Type fill then drives the rate fill; a
  rate filled automatically is dropped again if the Type is cleared, but a rate typed by hand is kept.
- **The activity log records the real address of the PC that connected.** The Scheduled Task used to start the portal bound to
  `127.0.0.1` only, so every entry showed the server's own address. `service/run_hidden.ps1` now binds `0.0.0.0` (every network
  address; set the environment variable `PORTAL_HOST=127.0.0.1` to keep it local-only), so each connection is logged with the
  address it actually came from (`web.client_ip()`; `::ffff:` prefixes stripped). Behind a local reverse proxy such as nginx, uvicorn
  already substitutes the forwarded address - the proxy must send `X-Forwarded-For`. Other PCs need Windows Firewall to allow inbound
  TCP 8420 on this machine (needs an administrator: `New-NetFirewallRule -DisplayName "ITAM Portal" -Direction Inbound -Protocol TCP -LocalPort 8420 -Action Allow`).
  Entries written before this change keep 127.0.0.1.
- **Rate component / rate value** are on the Assets register, the New/Edit asset form, and follow the chosen Type on a new asset
  (`GET /api/edit/assets/rates`).

## Review batch: security hardening, data-quality checks, stocktake, labels, export (2026-09-23, night)

Result of a tester / IT-inventory review of the whole portal (see the review notes in the conversation). Everything below is additive:
no existing row was changed and no importer behaviour changed.

- **Sign-in throttling per address** (`auth._ip_throttle_*`): 15 failed sign-ins from one address in 10 minutes -> HTTP 429 for that address,
  whatever user names were tried (the per-account lock-out only stopped guessing one account; the roster accounts share a known starting
  password, so one PC could try them all). A successful sign-in does not reset the count. In memory: a server restart forgets it.
- **Users and security** lists accounts that have never signed in (still on the starting password) in a warning above the table.
- **Optional second backup location**: set the environment variable `PORTAL_BACKUP_COPY_DIR` (another disk or a network share) and every
  successful backup is also copied there; the result is written in the backup's note. Off by default; a failed copy never fails the backup.
- **Sign out other sessions** in *My account* ends the account's sessions everywhere else (`POST /api/auth/logout-others`).
- **Data integrity has 11 new checks**, each with example assets you can click: IP field not a single address, duplicate IPs, duplicate
  hostnames, no installation date, in-use assets with expired cover, removed-from-AMC assets still carrying a rate, AMC without a rate,
  assets held by retired users, users retiring within a year, components with no parent device, assets not physically checked in a year.
  All are "needs review" (never "fails") and change nothing.
- **Physical check (stocktake)**: *Verify* on an asset records FOUND / NOT FOUND with an optional note, who and when - one row each time in
  `asset_verification`, so the history is kept. An administrator may verify any asset, an engineer only their own. The register has a
  *Last verified* column and a *Physical check* filter (never / over a year ago / within a year).
- **QR label**: *Label* on an asset shows a QR code that opens the asset's record on the address the request came in on, with its key, class,
  type, make/model and serial, and prints only the label (`/api/assets/{key}/qr`). Sign-in is still required to open the record.
- **Export CSV** on every register: everything matching the current search and filters (up to 50,000 rows), same columns, same access and
  per-user scoping as the screen; logged in the activity log as EXPORT (`GET /api/registers/{name}/export`).
- **Asset lifecycle fields**: purchased on, purchase cost, vendor, purchase order no., refresh due on (new `asset` columns, portal-only: the
  importer writes only its own template columns, so a re-import never touches them and they need no manual-override lock -
  `edit.PORTAL_ONLY`). **Hardware details** (sub type, processor, RAM, storage, attached monitor, ports, capacity, battery) are now editable too.
- **Second backup copy is now on**: `service/run_hidden.ps1` sets `PORTAL_BACKUP_COPY_DIR` to `D:\itam-v2-backup` (a different physical disk from C:)
  whenever a D: drive exists and the variable is not already set; set it to an empty value to switch the copy off. Verified with a real backup.
- **SD-WAN network sheet reconciliation** (`tools/sdwan_live_preview.py`, read-only): compares the "Network Inventry" sheet of
  `S D Wan Cisco Detail.xlsx` with the live register and writes a preview workbook to `samples/`. It never reads the sheet's Login/Password columns.
  Findings on 2026-09-23: all 241 sheet rows match a register asset; 0 blank fields to fill; 28 rows where the CI number belongs to one asset but the serial/IP/hostname
  belong to another (`CI_MISMATCH`); 47 real value differences; 152 real SFP serials in the sheet, none in the register. The register's 814 "unlinked
  components" are AMC billing lines with no serial/location/parent - they cannot be tied to a switch individually. Nothing has been applied.
- Not done, on purpose: forcing password resets / disabling accounts, enforcing two-factor (needs the ADMIN password settled first), TLS and
  the nginx fix (server setup), filling install dates or linking the 814 components (needs your data), bulk edit, attachments, licence
  tracking, automatic e-mail alerts for retirements. The importer already keeps file-to-file history (`asset_snapshot`, `asset_change_log`);
  the asset drawer shows it as "Change history".

## Rules built in

- **Text is upper case.** Everything on screen is upper case; everything saved (portal, importer, loaders, manual SQL) is stored upper case by a database trigger (`portal_uppercase`). E-mail addresses are the exception.
- **Editing is protected.** Every field is validated; a record changed by someone else since you opened it is refused (compare-and-set); each save is one transaction; derived fields (cover, PM status, ageing, flags) are recomputed with the same rules the loaders use (`tools/itam_rules.py`); every change is written to the change log. Manual edits are remembered (`portal_lock`) so the next import does not overwrite them; the record shows "source file: ..." and a Reset link.
- **Nothing is deleted from the portal.** "Archive" hides a record and can be undone.
- **Sign-in.** scrypt password hashes; policy (length, character classes, history, expiry, lock-out) in *Users and security*; optional authenticator-app two-factor with recovery codes (RFC 6238); idle time-out (default 15 min) with a warning; first sign-in and reset passwords must be changed.
- **Activity log.** Sign-ins, failures, pages opened, edits, downloads, shares, imports, backups, administration.
- **Accounts from the CIPL roster.** *Administration > Users and security > Sync from roster* creates one login per active CIPL employee (user name = ECODE), skips designation "Office Boy" (no login), and makes Site In-charge/SI and Sr Server Engineer administrators - everyone else is a user. A new account made by sync gets its ECODE as the password (must change at first sign-in); an existing account only has its name/e-mail/group refreshed - its password is never touched. A malformed e-mail in the roster sheet does not block the account; it is just left blank and flagged.
- **Connections for reading are read-only** (`default_transaction_read_only`); writes use one short transaction each. Reports build SQL only from a column whitelist.

## Preventive maintenance

Financial-year quarters (Q1 APR-JUN ... Q4 JAN-MAR). The kick-off e-mail goes out **60 days after the quarter starts**; a weekly reminder follows. Record PM from an asset, or for every asset in the worklist. **Cycles and snapshots**: a snapshot is captured every Monday, after every asset import and when a quarter closes; *Close quarter* (administrator, typed confirmation, automatic backup first) freezes the snapshot and returns every in-scope asset to "PM pending" for the next quarter. Until a finished quarter is closed, the dashboard keeps showing it (marked "Not closed yet") rather than the new one - see "Closing a quarter after it has ended (2026-10-01)". **Past quarters** (PM menu) browses closed quarters from their frozen snapshots - see "Past quarters page". The *Close quarter* confirmation accepts ROLL OVER in any letter case (the page shows every typed letter in capitals, so an exact-case match used to reject a correctly typed word).

## Data import

*Data tools > Data import* (administrator): upload the raw file as received (asset inventory, HR manpower export, CIPL roster, call tracker, OEM RMA log). Step 1 runs the converter and **rehearses the load without saving anything** (since 2026-10-03 - see "The import check is a rehearsal of the load"), then shows its report and any warning; step 2 takes a safety backup and loads. The converters are the same `tools/*.py` used from the command line, so every rule decided for the templates applies. Needs the Python that made the virtual environment to have pandas (it does on this PC).

## Backup and restore

`pg_dump` custom-format files in `backups/` (override with `PORTAL_BACKUP_DIR`). Automatic daily backup (default 02:00, keeps the newest 14; manual and safety copies are never removed automatically), manual backup, verify (checksum + readable), download, restore. Restore needs the typed word RESTORE, verifies the file, takes a safety backup of today's data and replaces everything in one all-or-nothing transaction. Verified by restoring a real backup into a scratch database: every table, view and trigger came across identically.

- **Restore drill** (2026-10-03): the portal's *Verify* only checks a backup's checksum and that `pg_restore` can read it. `python portal/db/restore_drill.py` goes the whole way - it restores a backup into a brand-new scratch database (`ongc_ank_drill_<time>`), compares every table's row count with the live data, checks the search extension and views came back, runs a join over the restored data, and drops the scratch database again (it refuses to drop anything not named like that). Needs a login that may create databases: `postgres` from `~/.ongc_pgpass` on the dev PC, or `--admin-user` + `DRILL_ADMIN_PASSWORD` elsewhere. `--backup FILE` drills an existing backup instead of a fresh dump; `--keep` leaves the scratch copy for inspection. Run it monthly and before an important release; also part of the test suite (skipped where no such login exists). Checked against a stale backup: it reports the mismatches.
  **Rehearsing an upload on a copy of production** (2026-10-03): download a production backup (*Backup and restore > Download*), copy it to the dev PC and run `python portal/db/restore_drill.py --backup <file> --keep`. The scratch copy it leaves is owned by the portal login like the real database, so set `PGDATABASE=<that name>` and `PGPASSWORD=<the portal login's password>` and run any converter with `--dry-run` against it (e.g. `python tools/call_tracking.py tracker --raw <file> --dry-run`). You see exactly what the upload would do to production's data - removals, edits, refusals - without touching it. Then `DROP DATABASE <that name>;`. Verified with the real 1 Oct tracker against a restored copy.

## E-mail

*Administration > E-mail and alerts*: mail server, then per-rule switches (PM kick-off, PM reminder, quarter needs closing (a daily reminder to administrators while a finished quarter has not been closed - it never closes it itself), new asset assigned, asset removed, cover expired, cover expiring, call assigned, overdue calls, part received). Rules e-mail the assigned engineer (`company_email` from the roster, or the linked user's e-mail); each has "Preview" (shows what would be sent). Nothing is sent until e-mail is switched on and a rule is enabled; every message and failure is in the log and the same event is never sent twice. The SMTP password lives in `~/.itam_smtp_secret`.

## Layout

| Path | Purpose |
|---|---|
| `app/` | Starlette API: `main.py` (routes, response cache, live updates), `web.py` (access control), `auth.py` + `routes_auth.py` (sign-in, accounts, permissions), `edit.py` + `routes_edit.py` (validated, audited editing), `lifecycle.py` + `routes_lifecycle.py` (Replace/Redeploy asset), `queries.py`, `datasets.py` (whitelist of tables/columns/facets, status grouping), `dashboards.py`, `engineers.py`, `cards.py` (hover cards), `pm.py`, `reports.py`, `export.py`, `packs.py` (report packs), `mailer.py`, `scheduler.py`, `backup.py`, `importer.py` + `routes_tools.py` (data import, reports, PM, backups), `update.py` + `routes_update.py` (software update), `integrity.py`, `views_pref.py` (per-person column layout), `netid.py` (client host name), `live.py` (live-update hub), `db.py`, `config.py` |
| `db/` | `setup.py` (idempotent schema/trigger/first-admin set-up, run at every start), `restore_drill.py` (real restore test), `reset_admin.py` (break-glass administrator recovery) |
| `db/setup.py` | Idempotent, non-destructive: indexes, live-update triggers, upper-case trigger, tables, first administrator |
| `frontend/` | Source + `build.mjs` (esbuild). `node build.mjs` rebuilds `static/` offline |
| `static/` | Built site (hashed assets, IBM Plex fonts, Carbon icon sprite, ONGC logo) |
| `vendor/` | Offline Python wheels and the icon package |
| `tests/` | `python -m pytest -q` - about 540 tests in 24 files, run against the real database inside rolled-back transactions |
| `../tools/` | The converters (`inventory_to_master.py`, `hr_export_to_template.py`, `cipl_roster.py`, `call_tracking.py`, `sdwan_match.py`) and the shared rules (`itam_rules.py`, `itam_locks.py`, `master_db.py`), used both from the command line and by *Data import* |
| `../deploy/` | Container image, Podman unit, signed-release tooling (`build-release.ps1`, `make-release-key.ps1`), the VM update service, host scripts for the database server - see `deploy/README.md` |
| `../check.ps1` | One command: is it safe to release? |

## Tests

```powershell
cd portal; .\.venv\Scripts\python -m pytest -q     # about 540 tests (538 passed, 1 skipped on 2026-10-04; it was 200 on 2026-09-23)
```
Tests never change real data: each runs in one transaction that is rolled back. From the project root, `.\check.ps1` runs them together with the front-end build and a clean-tree check (see "Checks and tests").

## Serving to other PCs

`.venv\Scripts\python serve.py --host 0.0.0.0` makes the portal reachable on the LAN. Sign-in is required, but the connection is plain HTTP: put it behind an HTTPS reverse proxy if the network is not trusted (the session cookie becomes `Secure` automatically when the proxy sends `X-Forwarded-Proto: https`).

## Deploying to the production server (offline, no internet on either side)

> **Which route is current?** Production today uses the **container route** described in the next section and in `deploy/README.md`: PostgreSQL on the Windows Server, the portal in a Podman container on the Ubuntu VM, updated with signed release packages. The steps in this section are the original Windows-native alternative (the portal as a Scheduled Task on a Windows machine); they remain valid if that route is ever needed, but they are not how production is updated now.

The production server is on the office LAN, has no internet access, and cannot be reached from this
machine either - everything below is a **copy-and-run** procedure using files already in this repo,
no download needed on the production server itself.

**What already makes this possible, already in the repo:**
- `vendor/wheels/` - every Python package this app needs, as `.whl` files (everything in `requirements.txt` - `starlette`,
  `uvicorn`, `orjson`, `psycopg[binary,pool]`, `openpyxl`, `reportlab`, `segno`, `pillow`, `charset-normalizer` - plus their
  dependencies). `run_portal.ps1` already installs from this folder with `pip install --no-index --find-links vendor\wheels`
  - it never touches the internet. If `requirements.txt` ever changes, refresh this folder **on a machine that does have internet**
  with `python -m pip download -r requirements.txt -d vendor\wheels` before copying to production.
- `static/` - the already-built front end (hashed JS/CSS, IBM Plex fonts, Carbon icons, the ONGC logo). Production never needs
  Node.js/npm at all unless someone wants to edit the front-end source there (`vendor/npm/` has the offline npm cache for that
  case too, but it is not needed just to run the portal).
- `db/setup.py` - idempotent: creates every table/index/trigger/view it needs and is safe to run again later after an update.

**Steps, on the production server:**
1. **Install Python 3.14 (64-bit)** and **PostgreSQL 18 (64-bit)** - download the official Windows installers from
   python.org and postgresql.org (or EDB) on any internet-connected machine first, carry them over by USB/network share, then
   run each installer on the production server. Match these major versions - the vendored wheels are built for Python 3.14
   (`cp314`, `win_amd64`); a different Python version needs a fresh `vendor/wheels` refreshed for that version instead.
2. **Create the database role and database** in PostgreSQL (matching what `app/config.py` expects, overridable with the
   `PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER` environment variables - defaults are `localhost:5432`, database `ongc_ank`, role `ank_app`):
   ```sql
   CREATE ROLE ank_app LOGIN PASSWORD '...a real password...';
   CREATE DATABASE ongc_ank OWNER ank_app;
   ```
3. **Copy this whole `portal/` folder** to the production server (USB drive or a network share both machines can reach) -
   *except* `.venv/`, `__pycache__/`, `backups/`, and `service/logs/` (all regenerated automatically; carrying over a `.venv`
   built on a different machine can break instead of help).
4. **Create `%USERPROFILE%\.ongc_pgpass`** on the production server, under the Windows account that will run the portal, in
   the standard [pgpass format](https://www.postgresql.org/docs/current/libpq-pgpass.html) (`hostname:port:database:username:password`)
   with the role's real password - one line for `ongc_ank` is enough. If e-mail alerts are used, also create
   `%USERPROFILE%\.itam_smtp_secret` with just the SMTP password.
5. **First run** (builds `.venv` from `vendor\wheels` offline, prepares the database, opens the portal):
   ```powershell
   cd portal
   .\run_portal.ps1
   ```
   Sign in as `ADMIN` with the printed/temporary password and change it immediately (see "First sign-in" above).
6. **Bring over the real data**, either:
   - **Migrate the existing database**: on this machine, `pg_dump --format=custom ongc_ank > ongc_ank.backup` (or use
     *Data tools > Backup and restore > Manual backup* in the portal, which does the same thing), copy the file to production,
     then use *Backup and restore > Restore* there (or `pg_restore`) - **do this before step 5's first sign-in changes anything**,
     since restore replaces the whole database; or
   - **Start fresh and import**: use *Data tools > Data import* on the production portal to load the asset inventory, HR
     manpower export, CIPL roster, call tracker and OEM RMA files there directly.
7. **Register the always-on service** so the portal survives reboots and signs itself back in after a crash:
   ```powershell
   .\service\install.ps1
   ```
   This now registers triggers at logon **and** at boot, plus the 1-minute self-healing watchdog (see "Run automatically" above).
   **If the production server reboots with nobody physically signed in** (the normal case for a server), an "at boot" trigger
   alone is not enough - Windows still needs an interactive session for this task to run in. Configure Windows to sign this
   account in automatically on boot (Sysinternals *Autologon*, or `netplwiz` / the `AutoAdminLogon` registry value), so the
   scheduled task actually starts after an unattended restart.
8. **If other PCs on the LAN need to reach it**, see "Serving to other PCs" below, and open port 8420 (or whatever `-Port` was
   used) in Windows Firewall for the appropriate network profile.

## Container deployment (Ubuntu + Podman VM), database on the physical server, and HTTPS (2026-09-23, late)

`deploy/` at the project root runs the portal as **one Podman container** in the Ubuntu VM. The **database is not containerised**: it is the ordinary PostgreSQL on the
physical server; `deploy/host/Prepare-Database.ps1` creates the role/database there, restores a backup (only into an empty database) and lets the VM connect; the container
reaches it over the network (`PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD` environment variables - the password is a Podman secret). The portal serves **HTTPS itself**
(`serve.py --tls-cert/--tls-key`, optional `--redirect-port`; the real client address still reaches the activity log). Start with `deploy/README.md`.
Tested on the development PC with Docker and a throw-away PostgreSQL 18 standing in for a fresh server; the Quadlet unit and the Windows-only parts of `-ConfigureNetwork` (service restart, firewall) can only be tested on the real machines.
`PORTAL_DEMO_USER=off` never creates the DEMOUSER login and deactivates it if present (set in the production unit).

## Software update page (2026-09-25)

*Data tools > Software update* (administrator): install a new portal version from a signed release package - firmware-style. The portal receives and checks the package (`app/update.py`,
`app/routes_update.py`); the VM's update service (`deploy/updater/itam-updater.sh`, systemd path unit + heartbeat timer, root, outside the container) re-verifies the signature (owner's private key, never on a
server) and checksum, backs the database up, loads the image, restarts the portal, waits for the NEW container to be healthy and rolls back by itself if not. Hand-over through one shared folder
(`/var/lib/itam-updates` on the VM = `/updates` in the container: `incoming/`, `request.json`, `status.json`, `update.log`, `history.jsonl`, heartbeat). See `deploy/README.md`, "Updating".
`build-release.ps1` makes the package; `make-release-key.ps1` makes the key pair once.

## Barcode labels and activity-log hostname (2026-09-27)

- **Barcode stickers**: *Print barcode labels* on the assets register downloads a PDF sticker sheet (Code128, one per asset matching the
  current search/filters, 45×22mm, 4-across) for the Asset (CI) number - built server-side with `reportlab` (`app/export.py`), no new
  dependency. The asset hover card also shows the same barcode (rendered as SVG, same code path) for every asset, always on.
- **Activity log now records the client's hostname alongside its IP** (`app/netid.py`): addresses on this LAN are DHCP-assigned, so the IP
  alone does not reliably identify a machine after the fact. Resolved via NetBIOS name service (works for any Windows PC with no DNS setup
  needed) falling back to reverse DNS, both bounded to a few hundred milliseconds and cached; a lookup failure never costs the audit row
  (`auth.log` never raises). Shown as a second line under the IP in *Activity log*, with its own filter and facet.

## Colourway: module accents and a richer chart palette (2026-09-27)

The portal read as flat gray-and-blue outside its charts. Recoloured deliberately, without adding any library, font or request:
- **One accent colour per module** (`--mod-assets/calls/eng/pm/rep/spr/adm/data` in `tokens.css`, light + dark): tints a nav item's icon
  (`shell.js`, `layout.css`) and, on each dashboard, any KPI tile that has no more specific tone (`.page[data-mod]` -> `--page-mod`,
  `components.css`). A KPI with a real tone (`ok`/`warn`/`bad`) still wins over the module colour - status always outranks wayfinding.
- **Chart palette extended from 8 to 10 hues** (`--chart-1`..`--chart-10`) and revalued to be more distinct; `ui/charts.js`'s `colorVar()`
  cycles through all 10, so every dashboard chart, doughnut and legend picked up the richer set with no code change beyond the token values.
- **Asset class gets a small colour dot** in the assets register's Class column (`ui/table.js`, `classDotVar()` in `ui/badge.js`) - a dot,
  not a tinted chip, so it stays legible at thousands of rows. Status badges are unchanged: they were already tinted only when the tone is
  `ok`/`warn`/`bad`/`info` (never for a plain `mute` status) - the same "colour only when it earns it" rule this batch extended elsewhere.
- Previewed for approval first as a private, throwaway Claude Artifact (three side-by-side modes: current / recommended / full-colour) built
  from real register data before any real file was touched. Adds ~1 KB to `app.css`, nothing to the JS bundle size worth noting (22.1 -> 22.3 KB).

## Add user restored (2026-09-30)

*Add user* (button in *Users and security*, next to *Sync from roster*) is back: username, full name, e-mail, group and an optional
engineer link, `admin="strict"`, `POST /api/admin/users/create`. It had been removed entirely with a regression test guarding against
its return (`test_scoping.py`'s old `test_add_user_route_no_longer_exists`, now repurposed to check a manually created account gets no
broader default access than a roster one) - re-added at the user's explicit request, specifically for logins that don't belong to
anyone on the CIPL roster (a general ONGC employee, say). Uses the same `auth.create_user()` the roster sync already called; the only
change is a new HTTP path to it. See "Account creation" above for the full behaviour.

## Closing a quarter after it has ended (2026-10-01)

On the first day of Q3 the PM dashboard showed Q2's done/pending counts under a "Q3 OCT-DEC 2026" heading, and *Close quarter* offered
"Close Q3 and open Q4 JAN-MAR 2027" (as an early cut-over). Cause: `pm.dashboard()`, `pm.rollover()`, `pm.rollover_preview()` and the
snapshot labels all took "the current quarter" from `pm.quarter(today)`, which flips at midnight - but a quarter is normally closed a
day or more *after* it ends. Now they use `pm.active_quarter()`: the quarter the in-scope assets actually carry in `pm_quarter`, with
`overdue = True` from the day it ends until it is closed.
- **Close quarter** closes the assets' quarter and opens the one containing today (or the next one, for an early cut-over). No
  "early" tick is needed once the quarter is over. A PM already dated inside the quarter being opened (recorded between the quarter
  ending and the close) is kept and becomes *Done* for the new quarter instead of being wiped.
- **Dashboard** keeps the old quarter's label while it is unclosed, with a "Not closed yet" badge and a line pointing to
  *Cycles and snapshots*. After the close: Done 0, Pending = everything in scope.
- **Snapshots** (manual, Monday, after import) are labelled with the assets' quarter, never the calendar one.
- **PM kick-off / weekly reminder e-mails** stay silent while a quarter is overdue; the *quarter needs closing* reminder covers it.
- Test: `test_closing_a_quarter_after_it_has_ended_closes_that_quarter_not_the_new_one`; the two older PM tests now pin
  `pm_quarter` themselves instead of depending on where the real data is on the day they run.

## Inward / outward / RMA line identity (2026-10-02, RMA added 2026-10-03)

Uploading the 1 Oct call tracker was refused with "Load blocked (SPARE_OUTWARD): 30 of 113 current rows are missing (>20%)". Nothing was
missing: the tracker's SERIAL column had been filled in for 29 outward lines that were blank before, and the loader built each line's ID
from that column (`OUT-0084`), or from the sheet row when it was blank (`OUT-R85`). Same lines, new IDs - so the old IDs looked deleted.
SERIAL is typed by hand (blank, retyped, renumbered on a sort), so it can no longer decide identity.
- A line is **the same line** when it has the same call (SR) and the same part (inward: and the same date logged). Received date, bill,
  AWB, gate pass, sent date and remarks are deliberately *not* part of it - they are filled in later and must update the record.
  Whitespace and letter case are ignored. `call_tracking.reuse_ids()` does the matching, in the check step and again at load.
- **IDs are sticky.** A matched line keeps the ID it already has (including old `OUT-R85` ones), so portal edits, locks, history and
  the change log stay attached. Only a genuinely new line gets a new ID: the next free number after the highest in the table, the same
  scheme the portal uses when someone adds a line by hand (both take the same database lock, so they cannot collide).
- **Edits are recognised, and reported.** A line that no longer matches but obviously is an edit keeps its record: same call + part with a
  corrected date; or the only leftover line of its call on both sides (part text corrected). The converter report lists each one. If a
  call has several leftovers, nothing is guessed - they count as removed + added.
- A line that was removed and later comes back revives its old record. Repeats of one identical line pair up in file order and the
  second is flagged `DUPLICATE_LINE` (replaces the old `DUPLICATE_SOURCE_SERIAL`). The 20% mass-removal safety stop is unchanged.
- SERIAL is still read, only as a provisional label when the database is unreachable. The sheet row is kept in `SOURCE_ROW`.
- **OEM RMA lines use the same engine** (2026-10-03): the sheet's "Sr. no" used to be the key (`RMA-0001`), the same weakness. A line is now
  matched by faulty part serial + call date (unique across all 74 current lines); a corrected call date, or a corrected faulty serial where
  the RMA number pairs one-for-one, is an edit; the same serial repaired again on a later date is a new record. A blank RMA number never pairs.
- Replay of the real 21 Sep -> 1 Oct files: outward 112 matched (29 under their old `OUT-R` IDs), 1 new, 0 removed; inward 0 removed.
  Tests: `tests/test_call_ids.py` (14).

## Replace asset / Redeploy asset (2026-10-02)

A machine's **physical identity** is its serial number, ONGC asset ID and census number. Its asset key / CI number / hostname are *labels* that can move. Two administrator-only actions on the asset page (`app/lifecycle.py`, `app/routes_lifecycle.py`, `frontend/src/js/views/asset-lifecycle.js`) handle the two real cases. Both run in **one transaction** (all or nothing) and re-key every table that refers to the asset (asset, snapshots, change log, calls, inward, outward, OEM RMA, serial history, PM records and snapshots, verifications, components, the manual-edit locks and the audit trail).
- **Replace** (button on a live asset): a new machine, already in the register as its own asset, takes this machine's key. The old machine becomes `<key>-RET-<yyyymmdd>` (archived, status REPLACED, with replaced-by, date, reason, disposal reference and a data-wiped flag) **and keeps its own history**; the new machine is re-keyed to the vacated name, must be given its serial number, and may carry over the user (CPF, department), location and engineer. It starts with clean PM and keeps its own cover. Blocked while the old machine has an open call or an OEM RMA not yet returned, if both records are the same machine, or if the serial already belongs to another live asset.
- **Redeploy** (button on a retired or archived asset): the *same physical machine* goes back into service under a new key/CI/hostname. It stays one record, history included; serial, ONGC ID and census number are shown locked and the server rejects any attempt to send them. Needs the "old data wiped" confirmation; a condemned, lost or generically-archived machine also needs the manager's approval reference. The new name must be unused and not a former name of a different machine.
- **Former names** are kept in the new `asset_alias` table and shown on the asset page ("Former names"), with dates. A replaced machine cannot be Restored - only Redeployed.
- **Spreadsheet guard** (`itam_locks.alias_conflicts`, called by `master_db.load_assets`): after a replacement, an inventory file that still uses a former name, or still puts the old machine's serial/ONGC ID on the reused name, is **refused with the row numbers to fix** instead of silently creating a duplicate or overwriting the new machine. Nothing is loaded in that case.
- Audit entries: `RETIRE` (on the retired key), `REPLACE`/`REDEPLOY` (on the live key). Re-keying moves earlier audit rows with the machine, so each asset's history follows it; the action's own audit entry records the key change.
- Schema (added by `db/setup.py`, idempotent, additive): `asset.retired_on / retired_reason / replaced_by_key / disposal_ref / data_wiped` and table `asset_alias`.
- Still manual after a replacement: correct the inventory spreadsheet (new machine's row takes the name, old row removed) and amend the AMC contract if needed.
- Tests: `tests/test_lifecycle.py` (21, rolled back, synthetic `TST-*` assets).
- **Updating each environment** (no data is touched; the new columns/table are added when the new version starts):
  - *Development PC:* `cd portal\frontend; node build.mjs`, then restart the **ITAM Portal** scheduled task (`Stop-ScheduledTask 'ITAM Portal'; Start-ScheduledTask 'ITAM Portal'`) - `db\setup.py` runs at start.
  - *Production Windows server:* copy `portal\app`, `portal\db`, `portal\static`, `portal\frontend\src`, `tools\itam_locks.py`, `tools\master_db.py` over the installed folder, run `.\.venv\Scripts\python db\setup.py` in `portal`, restart the scheduled task. Take a backup first (*Data tools > Backup and restore*).
  - *Ubuntu VM container:* build a new signed release (`.\deploy\build-release.ps1`) and install it from *Data tools > Software update*, or from the VM: `sudo ./update-portal.sh releases/itam-release-<version>.itamrel`; the container's entrypoint runs `db/setup.py`. Going back needs no database restore (the old version ignores the new columns).

## Checks and tests (2026-10-03)

`.\check.ps1` (project root) is the one command that answers "is this safe to release?": it lists uncommitted work and unpushed commits, builds the front end, runs every test, and with `-Drill` also runs the restore drill. `deploy\build-release.ps1` refuses to build while anything is uncommitted (`-AllowDirty` overrides) and writes the commit id into the signed notes.
- Suite: ~500 tests, all inside rolled-back transactions. Highlights: `test_permission_matrix.py` (every kind of account x every sensitive action through the real routes), `test_call_ids.py` (record identity), `test_import_rehearsal.py`, `test_restore_drill.py`.
- **No hosted CI yet, deliberately.** The tests read the dev database's real data, so a hosted runner would have nothing to test against. The prerequisite is a small fixed test dataset (synthetic assets, calls, engineers) that tests create for themselves; the three tests that used to fail whenever an upload changed the data now assert behaviour instead, which is the first step. There are also no browser tests: Playwright would need a ~150 MB browser download on the dev PC, which should be your call.
- Known gaps that no test covers: accessibility (WCAG 2.2 AA is claimed but never measured - an axe scan would confirm or correct that), and nobody is alerted if the production portal goes down (it cannot e-mail about its own outage; that needs a check from another machine).

## Unused starting passwords can expire (2026-10-03)

Roster accounts start with a password anyone who knows an ECODE could guess, and the Users page keeps warning that a dozen accounts have never signed in. New security setting **Unused starting password expires after N days** (`starting_pw_days`, Users and security > Security settings; **0 = never, and that is the default** - switch it on deliberately, because it affects people who simply have not signed in yet). With it on, an account that is still on its starting password (never changed, or reset by an administrator) and older than N days is refused at sign-in *after* the password has been checked: "Your starting password has expired ... Ask an administrator to reset it" (logged as `LOGIN_REFUSED`). A wrong password still gets the generic answer, so nothing leaks. *Manage user > Reset password* re-arms the clock; the Users page shows a red **Starting password expired** badge. A password the person chose themselves, and the read-only demo account, are never affected. Tests: `tests/test_starting_password.py`.

## The import check is a rehearsal of the load (2026-10-03)

The 1 Oct call tracker passed the import check and was only refused when *Load* was pressed ("30 of 113 current rows are missing"), because the check never touched the database. Now every loader accepts `--dry-run` (`ITAM_DRY_RUN=1`): `master_db.connect()` hands out a connection whose `commit()` does nothing and whose `close()` rolls back, so the check runs the real load end to end - the same reads, the same safety stops, the same change counts - and saves nothing ("DRY RUN: nothing was saved"). The import page's check step uses it for assets, CIPL roster, call tracker, RMA and (by environment) the HR master.
- A safety stop the person may knowingly override (mass removal; the message says `--force`) no longer waits for the load: the check passes with a warning box and the log opens with "THE LOAD WOULD BE REFUSED" and why. A stop that cannot be overridden (a snapshot older than what is loaded; a file that undoes a Replace/Redeploy made in the portal; duplicate keys) fails the check, with the reason.
- The call-tracker converter now lists **what points at nothing** before loading: calls whose asset (CI) is not in the register, calls with no asset, spare lines whose call is not in the tracker. They still load (the portal flags them), but are named in the report instead of surfacing later on the integrity page.
- Not built: an "undo this load" button. A load changes several tables and merges with manual edits made since, so a safe undo is not a simple reverse; the protection is the rehearsal, the automatic safety backup before every load, and the restore drill (`db/restore_drill.py`).
- Tests: `tests/test_import_rehearsal.py`; the old check test now asserts the rehearsal saved nothing.

## Detail window, group headings and status wording (2026-10-04)

- **Floating detail window.** A record opens as an inset, rounded window that can be dragged by its title bar (shared `ui/drawer.js`), not a full-height panel on the right edge. The row being viewed stays highlighted above the dimmed page, on every register including Engineers. Every value in the window is IBM Plex Mono at one size, with bold section headings.
- **Group heading rows with counts.** Each register's default sort keeps its rows in status groups, with a heading row per group showing the **whole group's** count (taken from the status facet, not just the rows loaded so far). Headings appear only while the table is sorted by the grouping column; sort another column and they go away. The order is set per register in `datasets.py` (`_group(...)`):

  | Register | Grouped by | Order |
  |---|---|---|
  | Calls | call status | Blank, Raised, Resolved (newest first within each) |
  | Assets | asset status | Deployed, In stock, Standby, Not in use, Offline, Surplus, Transferred, Out of AMC, blank |
  | PM worklist | PM status | Scheduled, Completed late, Completed, N/A, blank |
  | Inward / Outward | received / sent | pending first, then received / sent |
  | OEM RMA | return status | Awaiting return, Returned, blank |
  | Engineers | employment status | Active, Left roster, Resigned, blank |
- **Friendlier status wording, labels only.** The screens show the words below; the **stored codes are unchanged**, so an Excel upload that uses the old codes still loads. CSV, Excel and PDF exports and report packs use the same words (applied only when the file is written, `export.py` `STATUS_LABELS`), so a re-importable stored value is never rewritten.

  | Field | Stored code -> shown as |
  |---|---|
  | Asset status | IN_USE -> Deployed, IN_STORE -> In stock, NOT_ON_NETWORK -> Offline, REMOVED_FROM_AMC -> Out of AMC |
  | Cover | EXPIRING_90D -> Renewal due, EXPIRED -> Lapsed |
  | PM | PENDING -> Scheduled, DONE -> Completed, DONE_OUTSIDE_QUARTER -> Completed late |
  | Call | OPEN -> Raised, CLOSED -> Resolved |
  | Spare part | NO_SPARE_NEEDED -> No part required, PART_PENDING -> Awaiting part |
  | OEM RMA return | PENDING -> Awaiting return |
  | Faulty spare | SENT -> Returned to OEM, NOT_SENT -> Not yet returned |
  The wording is column-aware (an asset's PM "Scheduled" is not reused for an RMA's return status). The Calls register briefly defaulted to "Raised + blank" with checkbox groups; that was reverted at the user's request - **all calls show by default** with the radio status filter, and the grouping is only the default sort.

## Past quarters page (2026-10-04)

**Preventive maintenance > Past quarters** (`#/pm/history`) answers "what did an earlier quarter look like?" after it has been closed. Closing never deletes anything: every close (plus the Monday and after-import ones) leaves rows in `pm_snapshot`, and this page reads them.
- Top table: every quarter with a snapshot, newest first, shown as of its **last** snapshot (the closing picture) - in scope, completed, completed late, scheduled-not-done, done %. Select one to open it.
- Opened quarter: KPI strip, by engineer / class / location, the PM recorded through the portal in that quarter (`pm_record`), and the asset-by-asset list. Filters: snapshot date (when a quarter has several), status, free-text search. State is in the URL, so a view can be bookmarked.
- **Download** (Excel, CSV, PDF) takes the same filters; the file also carries the breakdowns. Logged as `EXPORT_PM_HISTORY`. Status uses the on-screen wording (Completed / Completed late / Scheduled).
- Who sees what: open to everyone who can see PM. An administrator sees the whole fleet; a User sees only the assets that were on their own `engineer_key` when the snapshot was taken. A User with no linked engineer sees nothing (the PM dashboard falls back to everything in that case; this page deliberately does not).
- API: `GET /api/pm/history`, `GET /api/pm/history/detail?quarter=&as_of=&status=&q=`, `POST /api/pm/history/export`. Code: `pm.history*`, `frontend/src/js/views/pm-history.js`. Tests: `test_past_quarters_*` in `tests/test_tools.py`.
- Not covered: asset *fields* other than PM (a snapshot holds PM status/date/signers, class, engineer, location, cover status - not the whole asset row). For anything else about a past date use the asset's Change history or the Change log.

## Design basis

WCAG 2.2 AA (contrast, focus, keyboard, target size, reduced motion; status is always icon + text), ISO 9241-210, ISO/IEC 19770-1 asset vocabulary, ITIL 4 incident/asset practices. Type: IBM Plex. Icons: Carbon (Apache-2.0). Charts: Chart.js (MIT, tree-shaken, lazy). Colour: one accent per module plus a 10-hue categorical chart palette (`tokens.css`), status colour always paired with an icon and a text label.

## Bulk assign / hand over assets (2026-10-05)

Assets register -> **Assign engineer** (administrators and Users with full asset access). It acts on everything matching the current search and filters (up to 5,000 assets): pick the **New engineer**, and optionally an **Old engineer** - then only listed assets currently held by the old engineer move, the rest are left alone. To hand over a whole workload, filter by the old engineer and assign to the new one. Each asset goes through the normal edit engine (`edit.reassign_assets` -> `edit.update`), so it is validated, protected from re-import overwrite and written to the change log; the whole batch is one transaction and is logged as `REASSIGN_ASSETS`. API: `POST /api/edit/assets/reassign`. Tests: `tests/test_bulk_assign.py`.

## Forgotten password (2026-10-05)

After **3 wrong passwords** on the sign-in page (counted per browser tab, kept across a reload; also when the account is locked) the **Sign in** button is replaced by **Forgot password**. The person enters their user name:

- **Account has an e-mail address and e-mail is on** - a new temporary password is e-mailed (valid 60 minutes). It is stored beside, not over, the real password (`portal_user.reset_hash`), so someone typing another person's user name cannot lock them out: the old password keeps working until the temporary one is used, and a normal sign-in discards an unused one. Signing in with the temporary password also lifts a lock-out, then forces a new password.
- **No address, e-mail off, or the mail failed** - the request waits in `portal_reset_request`; administrators see it as a banner on **Users and security** with a *Reset password* button. Any reset, or the person signing in, closes it.
- The page always shows the same wording, so it never reveals whether a user name exists. Limits: 10 requests per computer per 10 minutes, one per account per 5 minutes. Two-factor still applies after the temporary password. Every request is in the activity log (`PASSWORD_RESET_REQUESTED`). Needs `db/setup.py` (adds the columns/table) - it runs on the normal update.


## Inventory match (2026-10-06)

**Data tools > Inventory match** (full administrators). Upload a report or inventory received from the centre - BigFix, antivirus, patch,
EDR, anything with a computer name per row - as `.xlsx` or `.csv`, exactly as received. Every machine in the asset register whose Asset (CI)
starts with the chosen site prefix (default `ANK`) and whose class is Desktop (which includes All-in-One and Office Laptop) / Workstation / Server (changeable; plain Laptops are off by default but can be ticked) is checked:
**Asset (CI) = computer name means the tool is installed, otherwise not.** Matching ignores case and a domain suffix (`host.domain.local`).
The report is only read - nothing is loaded into the register.

Steps: upload -> the portal guesses the columns (computer name, IP, OS, last report time) and you confirm -> *Match against the register*.
Findings, with what to do about each: coverage against a target (default 95%), machines deployed to users that have no agent (High
priority; stored/surplus machines are Low), installed machines that stopped reporting (active <= 7 days, stale <= 30, dormant beyond),
machines that are in the report under a *different* name (same IP address or host name - rename or correct the CI; not counted as
installed), report names that are not in the register at all, Windows 10 and older, duplicate records in the report; broken down by
class, engineer, location, status and model.

Downloads: **Excel for the team** (key findings, coverage tables, the not-installed action list sorted by priority and engineer, silent
agents, name mismatches, devices not in the register, duplicates, full match) and **PDF for management** (KPIs, charts, findings, class
and engineer tables, the 25 worst machines). Each analysis is stored (`portal_match_run`, kept; nothing is deleted) and can be reopened
from *Earlier analyses*. Code: `app/inventory_match.py`, routes `/api/match/*` in `app/routes_tools.py`, view `inventory-match.js`,
tests `tests/test_inventory_match.py`. Uploaded files are staged in `uploads/inventory-match/`.

### Each engineer gets their own list (2026-10-06)

From a finished analysis, **E-mail engineers...** shows who would be mailed (engineer, address, machines, no agent, deployed, silent) and sends
nothing until *Send now*. Each engineer receives only the machines assigned to them (`asset.engineer_name`): a short e-mail with their most urgent
machines and their own Excel file (action list, silent agents, wrong-name cases). Tick **E-mail each engineer their own list when the match
finishes** on the upload form to do it automatically. Rules: the address is the engineer's CIPL company e-mail (same lookup as the alerts);
engineers with nothing urgent (only machines in store) or no address, and machines with no engineer, are listed but not mailed; sending is
once per analysis and engineer (`notify_log`, rule `MATCH_REPORT`), so a second press never double-mails and a failed one can be retried;
needs e-mail switched on under Administration > E-mail.

### The report is read before it is used (2026-10-06)

Reports from the centre arrive under different names and in parts, so the portal no longer trusts a file name or a typed tool name.
- **Product detection.** Each file is profiled from its column headings: Trend Micro Vision One, BigFix, or "Endpoint report" (columns guessed, you confirm). The tool name in findings and files is the detected product unless you type another. Several files are read as parts of ONE report (the centre splits a fleet by group or site); files of different products are refused.
- **Installed is not the same as healthy.** Each machine found in the report is graded Healthy or Needs attention: Trend Micro - install failed (with the reason), agent not communicating, anti-malware disabled; any product - silent beyond the stale limit. Advisory notes (pattern outdated, EDR off, old agent, never scanned) are listed separately, and when a note affects most of the fleet it is reported once as a central problem.
- **Ages are counted from the report's own date** (newest check-in in the file), not from today, and a report older than 3 days is flagged.
- **Data checks** (also in the Excel/PDF): duplicates and conflicting duplicates, devices parked in another console group, other sites' devices in this site's group, default Windows names on this site's IP ranges, shared IPs, missing columns, unreadable dates, register machines with no engineer or user.
- **Name pairing** is conservative: same IP, look-alike characters (O/0, I/1) or one character missing/extra. A different digit alone is NOT paired (DT140 and DT147 are two machines).
- **Every total is reconciled** (file rows -> unique computers -> this site -> found / not found -> healthy / attention; also by class and by engineer) and the report states it; if one fails, the PDF says not to circulate it.
Code: `profile_file`, `analyse`, `findings` in `app/inventory_match.py`; tests in `tests/test_inventory_match.py`.

### Each engineer's own report in the portal (2026-10-06)

An administrator presses **Share in the portal** on a finished analysis (reversible; the analysis is never removed). Every engineer then finds
**Reports > My inventory report**: their own machines only (taken from the engineer record linked to their account), with their own figures,
what to do first, the lists (install the agent / installed but needs attention / probably the same machine / all my machines) and a personal
Excel download. Asking for another engineer's list is ignored; an account with no linked engineer sees nothing; an unshared analysis returns
403. Administrators can preview any engineer's view. Routes `/api/match/mine*` and `/api/match/publish`; view `inventory-mine.js`.

**Automatic (2026-10-06, later).** There are no Share / E-mail buttons and no option to tick: every analysis is shared in the portal and e-mailed
to each engineer the moment the match finishes (`inventory_match.distribute`). If e-mail is switched off the analysis is still shared, the page
says no e-mail was sent and shows who would have been mailed, and the outcome is stored with the analysis. Sending stays once per analysis and
engineer, so nothing is mailed twice. The old manual routes (`/api/match/engineers`, `/api/match/publish`) are gone.

## Team Leader/SI tools for a plain User (2026-10-06)

A **User**-group account, **without** extended access, whose roster designation is Team Leader (`TEAM LEADER/SI`) is given four areas automatically -
no tick box to set: **Preventive maintenance > Cycles and snapshots** (view, capture snapshot, roll over a quarter), **Data tools > Inventory match**,
**Administration > Users and security** and **Administration > Activity log**. Nothing else changes: Data import, Backup and restore, Software update,
E-mail and alerts, Data integrity and Change log stay real-administrator-only.
- **How it is decided.** `auth.session_user` looks up the account's designation in `cipl_employee` (user name = ECODE, active and on the roster) and sets
  `lead_tools` when the group is User, extended access is off, the account is not read-only and `auth.LEAD_DESIGNATION_RE` matches. Move the person off the
  roster, change their designation, or switch them to Administrator / extended access and the access follows at the next request. An administrator or an
  extended-access account never needs it (they already have these areas).
- **Gate.** `web.read/write(admin="lead")` = administrator-level (incl. extended) plus a lead (PM cycles); `admin="lead_strict"` = real administrator plus a lead
  (Inventory match, Users, Activity log). The menu mirrors this (`shell.js`, `session.hasLeadTools()`).
- **No climbing out of the User group.** In Users and security a lead can add and manage plain User accounts only (`routes_auth._lead_limits`): no
  Administrator or extended-access accounts (so no password/2FA reset of one), not their own account, no granting Administrator or extended access.
  The Security settings button and Sync from roster (which derives groups from designations) are administrator-only and hidden.
- Tests: persona `user+lead` and the new rows in `tests/test_permission_matrix.py`, plus `test_team_leader_cannot_escalate_through_manage_user`.
