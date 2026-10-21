# Task list - restore a dump from another machine

Plan: `tasks/plan-backup-upload.md`. Tests: `cd portal; .\.venv\Scripts\python -m pytest -q`. Front end: `cd portal\frontend; node build.mjs`. Commit only your own hunks; one logical change per commit.

- [ ] **T1** (now small: dumps are the portal's own `-Fc --no-owner`) Drill-restore one real downloaded production backup; record any version/extension issue. Verify: `restore_drill.py --backup`.

### Checkpoint A - restore flags/privileges known

- [ ] **T2** `accept_upload()` + streamed `/api/admin/backups/upload` + "Upload a backup" UI. Accept: valid custom dump listed as UPLOADED; .sql / truncated / non-ITAM / oversize / non-admin refused, nothing left behind. Verify: `tests/test_backup_upload.py`, build, browser.
- [ ] **T3** Restore the uploaded dump (drill + UI), warning text for foreign dumps. Verify: drill passes; counts match; safety backup listed.

### Checkpoint B - user restores a real production dump on dev; suite + `check.ps1` green

- [ ] **T4** README + manual `pg_restore` fallback; check VM/TLS body-size limits.
