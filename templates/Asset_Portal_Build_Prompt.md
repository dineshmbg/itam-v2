# Prompt: build an asset-management portal for a different kind of asset

How to use: copy everything below the line into a new Claude Code session opened in an EMPTY project folder.
Replace every `[BRACKETED]` item first (section 1 is the only part you must fill in; delete lines that do not apply).
This prompt is modelled on the ITAM Portal in this repository (`portal/README.md`), generalised so it fits any asset type.

---

## 0. Your role and how to work

You are a senior full-stack engineer building a production web portal for a real organisation. Work in small, verified steps.

- Start by writing `tasks/plan.md` (milestones, data model, screens, permission matrix, risks) and `tasks/todo.md`. Show me the plan and WAIT for my approval before writing application code.
- One logical change per commit, with a why-focused message. Create a GitHub repository named exactly after the project folder and keep it in sync (commit and push when a milestone is verified).
- Never declare a milestone done until you have run the tests AND exercised the feature in a browser (for UI work). Report exactly what you verified and what you did not.
- When a decision is genuinely mine (naming, who may do what, how a business rule works), ask once, in a short list, with a recommended default. Otherwise make the call and tell me.
- **Nothing may be deleted without my explicit consent.** That covers files, database rows and tables, backups, branches and commit history. Before any removal, explain why, exactly what is lost and what is backed up, then give me a phrase of the form `CONFIRM DELETE <ITEM-ID>` and remove nothing until I reply with exactly that phrase.
- Never run a destructive command (DROP DATABASE, rm -rf, restoring a backup over live data, git reset --hard, force-push) without showing what will be lost and getting a clear yes.

## 1. What this portal is for (FILL IN)

- **Organisation / site:** [NAME, LOCATION]
- **Asset type managed:** [e.g. vehicles and heavy equipment / electrical panels and transformers / fire-safety equipment / instruments and calibration gear / furniture and civil assets / tools / …]
- **Approximate volume:** [N assets, N locations, N users, N engineers/technicians/custodians]
- **Who uses it:** [roles, e.g. administrators, section heads, field technicians, contractors' engineers, read-only auditors]
- **Source data today:** [Excel exports, ERP extract, paper registers … attach or describe a sample; I will provide real exports as received, not pre-cleaned]
- **The five questions the portal must answer at a glance:** [e.g. what do we own and where is it? what is out of service? what is due for inspection/calibration/maintenance this quarter? who is responsible? what did it cost to maintain?]
- **Life-cycle of one asset:** [acquired → commissioned → in use → under repair → transferred → condemned/disposed; list the states and who may move an asset between them]
- **Maintenance model:** [AMC/warranty contracts, preventive maintenance every N months, statutory inspections, calibration certificates with expiry, consumables, spare parts]
- **Fault / call model:** [how a breakdown is logged, who attends, SLA or priority rules, vendor/OEM RMA or warranty claim steps]
- **Identifiers in use:** [asset tag format, serial number, barcode/QR, ERP number, location codes]
- **Business rules I already know:** [bullet list; anything you must treat as a hard rule]

## 2. Non-negotiable constraints

1. **Fully offline at run time.** No CDN, no external fonts, no internet call anywhere. Vendor every Python wheel (`vendor/wheels/`) and every front-end asset so a machine with no internet can install and run the portal from a USB stick. Fonts and icons are bundled locally.
2. **Stack:** Python 3.14, Starlette API, PostgreSQL 18 (psycopg 3 with a connection pool), vanilla JavaScript front end bundled offline with esbuild. No heavy front-end framework. One PostgreSQL database for everything.
3. **Windows-friendly.** Development happens on Windows with PowerShell scripts (`run_portal.ps1`, `check.ps1`); the portal can run as a Scheduled Task that survives reboots and self-heals; production also supports one Podman container on a Linux VM with the database on a separate server (not containerised).
4. **Design must not look AI-generated or like a template.** Light, dark and automatic themes; IBM Plex typography; a Carbon-style icon set bundled locally; dense, readable data tables; WCAG 2.2 AA (keyboard operation, visible focus, contrast, labelled controls, no colour-only meaning, reduced-motion respected).
5. **Security by default:** scrypt password hashes, lock-out and per-address throttling, optional two-factor, idle time-out, expiry of unused starting passwords, parameterised SQL only, every state-changing request authenticated and authorised on the server (never only in the UI), CSRF protection, secure headers, uploads size- and type-limited, secrets never written to logs or to the repository. Assume the repository is PUBLIC: no real data, credentials or personal records are ever committed (use `.gitignore` for `reports/`, `uploads/`, `backups/`, `data/`).

## 3. Data model (design it from my source data, then show me)

- One current-record table for assets, with a controlled vocabulary for class/type/make/model/status/location/department, and **soft states only**: an asset is archived, never deleted. History is kept (who changed what, when, from/to) in an append-only change log.
- Related tables: locations (hierarchy), people (custodians, users), engineers/technicians, contracts (AMC/warranty with dates and rates), maintenance plans and work orders, fault calls, spare parts and consumables, vendor/OEM return cases, documents/certificates, attachments.
- Text stored in one canonical case by a database trigger; every field validated in one shared rules module used by both the web edit path and the bulk importers.
- A manual-edit lock table so a re-import never overwrites a hand-edited field.
- Database constraints (not just app checks) for: uniqueness of the asset tag, valid state transitions, date ordering, required fields per class.
- A schema setup script (`db/setup.py`) that is idempotent: it can be run again on a live database and only adds what is missing.

## 4. Screens and features

**Dashboards (live, from the database):** asset overview (counts by class/status/location/age/contract state), faults (open, ageing, by priority, by engineer), maintenance (due, overdue, done this cycle), workload by engineer/technician. Every number drills into the matching register.

**Registers (Assets, Faults, Spare/Inward-Outward movements, Vendor returns):** one reusable register component with: a left filter bar of facets with live counts (a facet that cannot narrow the current result is hidden and summarised as "Same for all N results"; a sub-type is listed under its class), global search, sortable resizable columns that remember each person's layout, inline "edit in a side window" with compare-and-set so two people cannot overwrite each other silently, bulk actions, CSV/Excel export, and hover cards for people, engineers and assets.

**Asset record window:** details, contract/cover, location history, maintenance history, fault history, documents, QR/barcode label, and the full change history. "Replace / Redeploy" keeps the old record and links the new one.

**Maintenance:** configurable preventive-maintenance cycles per asset class, a per-asset checklist, work orders with findings and a two-step sign-off, snapshots of each closed cycle, roll-over with a typed confirmation, and e-mail reminders to the responsible technician.

**Controlled removal from contract/service:** a request-and-approve flow (not a status edit) that records who approved, the effective date and the financial credit, archives rather than deletes, and can be reversed with a reason.

**Data in and out:**
- Importers that take a raw export **as received (not pre-cleaned)**, normalise it with the shared rules, rehearse the load first (real load, rolled back) and show a dry-run report, stop on suspicious mass removal, and take an automatic backup before loading. A re-import must keep hand-edited and archived records intact.
- Report builder with Excel, CSV and PDF output, saved report definitions, and scheduled e-mail delivery.
- Upload templates are served by the portal and live in `templates/`; generated samples live in `samples/`.

**Administration:** users and groups, per-person extra grants, security policy, e-mail/SMTP setup with rules that are OFF until configured, activity log (every sign-in, view of sensitive data, upload, download, export, removal), data-integrity checks (a page that lists rule violations such as duplicate tags, expired cover still carrying a rate, assets without a custodian), backup and restore with a restore drill, and signed software update with automatic rollback.

## 5. Permission model (state it in a matrix and make it executable)

Two groups: **Administrator** and **User**, plus a read-only demo login. A User is row-scoped to the assets and faults assigned to them (via their linked engineer/custodian key). Modules such as Faults, Movements and Vendor returns are OFF for a User unless individually granted (`NONE / READ / FULL`). Optional switches: an "extended access" level that behaves like an administrator for dashboards, registers and reports but never for data tools or administration; a Team Leader flag that unlocks selected approvals. Implement the matrix once on the server and prove it with an executable test (`tests/test_permission_matrix.py`) that fails if any route is added without a rule.

## 6. Quality bar

- Tests run inside rolled-back transactions and never touch real data, and never assume what the development database contains. Aim for a test for every business rule, every permission cell, every importer rule and every migration.
- A `check.ps1` that answers "is it safe to release?": uncommitted work, front-end build, every test, optional restore drill.
- Accessibility and responsive checks in a real browser at desktop and tablet widths, in both themes.
- Performance: registers of 50,000 rows stay responsive (server-side paging, indexes chosen from real query plans, `pg_trgm` for search).
- Observability: structured logs, a `/healthz` that checks the database, and an alert if the portal stops.

## 7. Delivery and operations

- `run_portal.ps1` builds the virtual environment from the vendored wheels if needed, prepares the database and opens the portal.
- A Windows Scheduled Task installer (logon + boot + one-minute self-heal watchdog).
- A `Containerfile`, Quadlet unit, VM install script and updater; releases are single signed packages (manifest + signature + image tar) built by `deploy/build-release.ps1`, which refuses a dirty tree, runs the tests, signs the manifest and records the commit id. Every release ships with full manual update commands as well as the in-portal update page.
- First run prints a temporary administrator password that must be changed at first sign-in. No default passwords in the repository.
- Documentation you keep current as you go: `README.md` (overview, access rules, operations, dated change history), `deploy/README.md` (step-by-step production deployment), and a short `CLAUDE.md` orienting a new session.

## 8. Milestones (stop after each, show evidence, wait for my go-ahead)

1. Plan, data model, permission matrix, sample-data import of ONE real export; no UI yet.
2. Sign-in, groups, security policy, activity log, audit/change log; the empty shell with themes.
3. Assets register, record window, edit engine with locks and compare-and-set.
4. Importers with rehearsal, backups and restore drill.
5. Dashboards and report builder.
6. Faults and movements registers, assignment, e-mail rules.
7. Maintenance cycles, work orders, sign-off.
8. Contract/removal workflow, data-integrity checks.
9. Packaging: Scheduled Task, container, signed release, deployment guide.
10. Hardening: accessibility pass, security review, performance pass, handover documentation.

## 9. First message back to me

Before any code, reply with: (a) the questions from section 1 you could not answer from my input, with a recommended default for each; (b) the proposed data model as a table list with one line per table; (c) the permission matrix; (d) the milestone plan with what you will verify at each step. Then wait.
