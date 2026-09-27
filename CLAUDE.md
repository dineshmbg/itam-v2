# project-claude

IT asset/HR data conversion tooling plus a full ITAM web portal, both built around one PostgreSQL
database (`ongc_ank`) for ONGC Ankleshwar's IT asset management. Everything runs locally and offline -
no CDN, no internet at run time, anywhere in this project.

## Layout

| Path | What it is |
|---|---|
| `portal/` | The ITAM web portal (Starlette + PostgreSQL + esbuild front end) - see `portal/README.md` and its own `portal/CLAUDE.md`-equivalent detail below. This is the actively developed part of the project. |
| `tools/` | Standalone Python converters, shared by the command line and by the portal's own Data import feature: `inventory_to_master.py`, `hr_export_to_template.py`, `cipl_roster.py`, `call_tracking.py`, `sdwan_match.py`, `master_db.py`, `itam_rules.py` (shared validation/normalisation rules), `itam_locks.py` (manual-edit-protection, shared with the portal). |
| `templates/` | Blank upload templates (`*_Upload_Template.xlsx`) - **every template this project produces goes here by default.** |
| `samples/` | Generated preview/sample files - **every sample or preview file goes here by default.** |
| `masters/` | Converted "master" output files (`*_Master_*.xlsx`) - the result of running a `tools/*.py` converter against a raw export. |
| `data/` | Raw source exports as received (asset inventory, HR manpower, CIPL roster, call tracker) plus `hr_master.db`. |
| `uploads/` | Scratch area for files uploaded through the portal's Data import feature. |
| `backups/` | `pg_dump` backups of `ongc_ank` (portal's Backup and restore feature, and manual `pg_dump`). |
| Loose `.xlsx` files at the root | Raw exports as handed over by the user, not yet filed into `data/`. |

## The ITAM Portal (`portal/`)

Login-protected, offline web app over `ongc_ank`: live dashboards (assets, calls, engineers, PM),
editable + audited registers, PM cycles, report builder, data import, backup/restore, e-mail alerts,
CIPL-roster account sync, users/2FA/security policy, activity and change logs. Runs as a Windows
Scheduled Task named **ITAM Portal** (`portal/service/install.ps1`) so it survives reboots and
self-heals within about a minute if it ever stops. Full detail, the permission model, and the dated
batch-by-batch change history are in **`portal/README.md`** - read that before making portal changes,
it is kept current and is more detailed than this file.

**Quick reference:**
```powershell
cd portal
.\run_portal.ps1                              # dev: build .venv from vendor\wheels if needed, prepare DB, open http://127.0.0.1:8420
.\.venv\Scripts\python -m pytest -q           # tests: run inside rolled-back transactions, never touch real data
cd frontend; node build.mjs                   # rebuild the front end after any frontend/src change (offline, esbuild)
.\service\install.ps1                         # register the always-on Scheduled Task (logon + boot + 1-min self-heal watchdog)
```
First admin: `ADMIN` / a printed temporary password (see `%USERPROFILE%\.itam_first_admin.txt` after
first run) - must be changed at first sign-in. Groups are ADMIN / USER only; a User's access is
row-scoped to their own assets/calls (via their linked `engineer_key`) and, for
Calls/Inward/Outward/OEM RMA specifically, off by default unless individually granted (see
`portal/README.md`'s "Who can do what").

**Deploying to the offline production server** (a different, non-internet-connected machine on the
office LAN) - see `portal/README.md`'s "Deploying to the production server" section: copy the project
folder, install Python 3.14 (64-bit) and PostgreSQL 18 (64-bit) from installers carried over by
USB/network share, install Python packages from the already-vendored `portal/vendor/wheels/` (no
internet needed, `run_portal.ps1` already does this automatically), migrate or import the real data,
then register the Scheduled Task.

**Container / VM deployment** - `deploy/` (Containerfile, entrypoint, Podman Quadlet unit, `vm-install.sh`, `new-certificate.sh`, `make-release-key.ps1` + `build-release.ps1` (dev PC: tests + build + SIGN a `.itamrel` package), `updater/` (VM update service behind the portal's Data tools > Software update page; verifies signature, backs up, health-checks, auto-rollback), `update-portal.sh` (CLI for the same), `host/Prepare-Database.ps1`, `host/Backup-Database.ps1`,
`images/itam-portal.tar` for offline transfer) - see `deploy/README.md`. The portal is ONE container in the Ubuntu VM; the **database is NOT containerised** - it is the PostgreSQL
installed on the physical server (`Prepare-Database.ps1` there creates role/database and restores a backup). Build the image from the project root
(`podman build -f deploy/Containerfile -t localhost/itam-portal:latest .`).

## Data conversion tools (`tools/`)

Each converter takes a raw export (as received - not pre-cleaned) and produces a validated "master"
upload file, following the same rules the portal's own importer uses:
- `inventory_to_master.py` - IT asset inventory export -> `IT_Asset_Master_*.xlsx`
- `hr_export_to_template.py` - raw Ank-manpower HR export -> `Employee_Master_Upload_*.xlsx` (see the
  `hr-manpower-monthly-upload` memory for the recurring monthly version of this)
- `cipl_roster.py` - CIPL employee roster -> loads `cipl_employee` directly (also used by the portal's
  "Sync from roster" account creation)
- `call_tracking.py` - CIPL call tracker export -> `Call_Tracker_*.xlsx`
- `sdwan_match.py` - matches SD-WAN/Cisco device details against the asset inventory
- `itam_rules.py` - shared validation/normalisation rules used by every loader **and** by the portal's
  own edit engine (`portal/app/edit.py`), so a manually-edited value and an imported value are checked
  the same way
- `itam_locks.py` - manual-edit protection (`portal_lock`): once a field is changed by hand (in the
  portal) or explicitly set by certain loader paths, a later re-import will not silently overwrite it

Run any converter directly with the portal's own venv's Python (it has `pandas`) or the base
interpreter if `pandas` isn't otherwise available - see `portal/tests/test_edit.py`'s import fallback
pattern for exactly how tests locate it.

## Working rules (apply across this whole project, not just the portal)

- **Templates go in `templates/`, samples/previews go in `samples/`** - always, by default, unless the
  user asks for somewhere else.
- **One logical change per commit, with a why-focused message - but never commit or push unless
  explicitly asked.** This project is not (yet) a git repository; treat any future `git commit`
  request the same way.
- **Never run a destructive command** (`DROP DATABASE`, `rm -rf`, restoring a backup over live data,
  `git reset --hard`, force-push) **without first showing exactly what will be lost and getting an
  explicit yes.**
- **Everything must work fully offline** - no CDN, no external call at runtime, anywhere in this
  project. When a package or asset is needed, vendor it (see `portal/vendor/`) rather than fetching it
  live.
- **The portal's design must not look AI-generated** - light/dark/automatic themes, IBM Plex
  typography, Carbon icons, WCAG 2.2 AA.
- **Before declaring a change done:** run the relevant tests, exercise the feature (in a browser for
  UI changes), and report exactly what was verified - not just what was implemented.
- **Make reasonable calls instead of stopping to ask**, unless genuinely blocked by a decision only
  the user can make, or the user explicitly asked for a preview/description before proceeding.

## Where things are tracked day to day

Claude's own cross-session memory for this project (user preferences, standing project decisions,
loose ends) lives outside this repo, in the auto-memory system - see `MEMORY.md`'s index for what's
recorded there (the ITAM Portal's role/permission model and full change history, the open-RMA
tracker-ID list awaiting user input, the monthly HR upload conversion recipe, and this project's
working-rules feedback). This `CLAUDE.md` is the one-time orientation doc; the memory system is what
stays current about what's actually in progress.
