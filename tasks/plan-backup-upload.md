# Implementation Plan: restore a database dump that did not originate on this machine

Status: **DRAFT for review** - nothing below has been built. Saved as `plan-backup-upload.md` because `tasks/plan.md` / `tasks/todo.md` hold an unfinished plan (PM work orders); they are untouched.

## Problem
*Data tools > Backup and restore* can only restore a file that is **already listed** (a row in `portal_backup` AND the file present in `backups/`). A dump made on the production server (pgAdmin, `pg_dump`, or that server's own portal backup downloaded to the dev PC) cannot be brought in: there is no upload, and copying the file into `backups/` does not help because `restore()` -> `_resolve()`/`verify()` only accept names the portal knows (and the name must match `[\w.-]+\.dump`). The only workaround today is `pg_restore` by hand (README "Rehearsing an upload on a copy of production").

## What exists (reuse, do not rebuild)
- `app/backup.py`: `verify()`, `restore()` (verify -> PRE_RESTORE safety backup -> `pg_restore --clean --if-exists --no-owner --single-transaction`), `_resolve()`, `_sha()`, `list_backups()`.
- `app/routes_update.py:upload` + `update.Upload`: pattern for streaming a large raw body to disk with X-Filename, same-origin/admin checks, progress bar in `admin-update.js:upload`. Normal JSON routes cap bodies at 1 MB (`web.py`), so upload needs its own streamed route.
- `admin-backup.js`: table with Verify / Download / Restore, typed-RESTORE modal. Restore route needs `admin="strict"` (full administrator).

## Design decisions
- **Upload, then it is a normal backup.** A new kind `UPLOADED` is stored in `backups/` and recorded in `portal_backup` (sha256, size, who, original name). From there Verify / Restore / Download work unchanged - no second restore path.
- **Accept only pg_dump custom format** (`-Fc`). Detect by the `PGDMP` magic bytes before the file is kept; a plain `.sql` dump is refused with a clear message. The stored name is sanitised and made unique (`<name>_<timestamp>_uploaded.dump`); the client's path is never used.
- **Size cap** and free-disk check, as in `update.Upload`.
- **Content sanity before it can be restored:** `pg_restore --list` readable (existing `verify`), and it must contain portal tables (e.g. `portal_user`) - otherwise "this is not an ITAM portal database". A dump from a newer PostgreSQL than the local `pg_restore` is refused by pg itself; surface that message.
- **Foreign-dump restore:** `--no-owner` is already set, but check that a dump taken with owners/ACLs (pgAdmin default) restores cleanly under the portal login, and that the `pg_trgm` extension does not need superuser (if it does: document it / use the create-capable login as `restore_drill.py` does).
- **Restore stays all-or-nothing and still takes a safety backup first.** A production dump replaces users, sessions, settings and the `portal_backup` history itself, so the modal warns about that.
- Full administrators only; every step in the activity log (`BACKUP_UPLOAD`, existing `RESTORE_*`).
- Out of scope: plain-SQL dumps, restoring into a different database name, a CLI.

## Dependency graph
Foreign-dump restore check (T1) -> `backup.accept_upload()` -> streamed route `/api/admin/backups/upload` -> UI "Upload a backup" -> (existing) Verify / Restore.

## Phases and tasks

### Phase 1 - Prove a production dump restores (de-risk)
- **T1: Reproduce with a real foreign dump** (S) - take `pg_dump -Fc` (with owners) from production, drop it in `backups/` by hand, run `pg_restore --list`, and restore it into a scratch database with `db/restore_drill.py --backup`.
  - Accept: we know whether owners/ACLs, the extension or the version cause failures, with the exact error text.
  - Verify: drill output recorded in this plan.

### Checkpoint A - the restore command is known to work for such a file (or the extra flags it needs are listed).

### Phase 2 - Upload end to end (one vertical slice)
- **T2: `accept_upload()` + route + minimal UI** (M)
  - Back end: stream to a temp file in `backups/`, check `PGDMP` magic, size cap, free space, `pg_restore --list`, require portal tables, compute sha256, rename to the final unique name, insert the `portal_backup` row (kind `UPLOADED`, `made_by`, note = original file name); delete the temp file on any failure. Add `UPLOADED` to the allowed kinds and to the UI badge map.
  - Front end: "Upload a backup" button, file input and progress bar (copy of the `admin-update.js` pattern); the new row appears with Verify / Restore.
  - Accept: [ ] a production custom-format dump uploads and is listed; [ ] a `.sql`, a truncated file, a non-ITAM dump, an oversize file and a non-admin are each refused with a clear message and leave nothing in `backups/`; [ ] uploading alone restores nothing.
  - Verify: `tests/test_backup_upload.py` (temp `PORTAL_BACKUP_DIR`; refusals need no PG, the happy path uses a small real dump and is skipped where `pg_dump` is absent); `node build.mjs`; browser walk-through on the dev portal.
  - Files: `app/backup.py`, `app/routes_tools.py`, `frontend/src/js/views/admin-backup.js`, tests.
- **T3: Restore the uploaded dump** (S)
  - Apply T1's findings to `restore()` if a flag or privilege change is needed; modal wording for foreign dumps (who is signed out, history replaced).
  - Accept: [ ] the uploaded production dump restores into a scratch DB via the drill and into the dev DB through the UI; [ ] a safety backup is taken first and listed afterwards; [ ] a failure rolls back and leaves data unchanged.
  - Verify: `restore_drill.py --backup <uploaded file>`; UI restore on the dev PC after a manual backup; row counts compared before/after.

### Checkpoint B - the user uploads a real production dump and restores it on the dev PC; `pytest -q` and `check.ps1` green.

### Phase 3 - Housekeeping
- **T4: Docs** (S) - README "Backup and restore" (upload flow, `pg_restore` fallback); the production-to-dev rehearsal paragraph points at the button. Check that the VM/TLS front end allows a large upload body.

## Risks
| Risk | Mitigation |
|---|---|
| A foreign dump wipes dev users/settings | typed RESTORE, safety backup first, explicit warning text |
| Large upload exhausts disk | size cap, free-space check, temp file removed on failure |
| Dump from a newer PostgreSQL | pg's own error surfaced; documented |
| Hostile file | admin-only, magic-byte check, never executed, name sanitised, only `pg_restore` parses it |

## Answers from the user (2026-10-21)
1. Dumps come from the **portal's own Back up now**, downloaded - so they are `pg_dump -Fc --no-owner`, the same flags this portal restores. The owner/ACL risk in T1 largely disappears; T1 shrinks to one real check (a downloaded production backup restores via the drill).
2. Direction is **production dump -> dev PC** only. No dev -> production path or wording.
3. **No plain-SQL support.**
