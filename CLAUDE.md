# project-claude

IT asset/HR data conversion tooling plus a full ITAM web portal, both built around one PostgreSQL
database (`ongc_ank`) for ONGC Ankleshwar's IT asset management. Everything runs locally and offline -
no CDN, no internet at run time, anywhere in this project.

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
..\check.ps1 [-Drill]                         # is it safe to release? uncommitted work, front-end build, every test, optional restore drill
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

**Container / VM deployment** - see `deploy/CLAUDE.md` and `deploy/README.md`. The portal is ONE container in the Ubuntu VM; the **database is NOT containerised** (PostgreSQL on the physical server). Build the image from the project root: `podman build -f deploy/Containerfile -t localhost/itam-portal:latest .`

## Data conversion tools (`tools/`)

Converters in `tools/*.py` take a raw export **as received (not pre-cleaned)** and produce a validated master/upload file using the same rules as the portal importer (`itam_rules.py`; `itam_locks.py` stops a re-import overwriting hand-edited fields). Run them with the portal's venv Python (it has `pandas`); see `portal/tests/test_edit.py` for the import fallback. Monthly HR export: see the `hr-manpower-monthly-upload` memory. Outputs go to `masters/`, raw exports live in `data/`; loose root `.xlsx` files are raw exports not yet filed.

## Standing instructions from the user (permanent - do not drop or dilute)

- **Default working folder:** `ai-workspace/projects` on the user's **Google Drive** (not on a local
  PC). At the start of work, always ask which project to open, or whether to create a new one.
- **New project = new GitHub repo:** whenever a new project folder is created, automatically create
  and sync a GitHub repository named exactly after the project directory.
- **Git is pre-authorised:** pull before editing; commit and push when the work is ready. Don't ask.
- **NO DATA MAY BE DELETED OR REMOVED WITHOUT THE USER'S EXPLICIT CONSENT.** This covers files,
  folders, database rows/tables, backups, branches, commits/history (force-push, `reset --hard`,
  rebase), and existing content inside files. Before any removal:
  1. Give a detailed explanation: the root cause / why removal is needed, the steps taken so far,
     what changes, exactly what data would be removed, and what is saved or backed up.
  2. Give the user a short confirmation phrase to reply with, in the form
     `CONFIRM DELETE <ITEM-ID>` (e.g. `CONFIRM DELETE OLD-GIT-RULE`).
  3. Remove nothing until the user replies with that exact phrase. Anything else = not confirmed.
- **Work setup:**
  | Machine | Role | Address |
  |---|---|---|
  | Dev PC or laptop | Where development happens - separate from production | - |
  | Production physical server | Windows Server 2019 Standard, PostgreSQL runs natively | 10.205.64.47 |
  | Hyper-V VM on that server | Ubuntu Linux, runs the portal container via Podman | 10.205.64.25 |

## Working rules (apply across this whole project, not just the portal)

- **Templates go in `templates/`, samples/previews go in `samples/`** - always, by default, unless the
  user asks for somewhere else.
- **One logical change per commit, with a why-focused message.** Pull before editing, commit and
  push when the work is ready - pre-authorised by the user, don't ask.
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
