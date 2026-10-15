# Plan: Software and OS observations per asset

Status: **APPROVED 2026-10-15**; tasks 1-7 built (task 7 = docs + release, see portal/README.md "Software and OS observations"). (`tasks/plan.md` is the PM-module plan - this is a separate feature.)

## Goal
Keep an append-only record of what software/OS an asset was *observed* to have (Windows edition+build, Trend Micro Vision One, BigFix, Zscaler VPN / any other VPN, SAP GUI version, O365 account+version, extras), from several sources, shown **only in the asset's record window**, and used by the Inventory match to cross-check reports.

## Decisions (agreed)
- Scope: DESKTOP, WORKSTATION, SERVER. Access: same row-scoping as today.
- Sources: BigFix report, Trend report, check script (`.bat`, no PowerShell - blocked by GPO), manual entry, Excel bulk template. A source never overwrites another: conflicts are shown side by side, **the engineer decides** (the portal never picks).
- **Inspection date is automatic**: server `now()` at insert, never accepted from a form/API/file, rows cannot be updated or deleted (DB trigger). A separate `data_as_of` carries a report's own timestamp.
- VPN standard = Zscaler; any other VPN client found = exception flag. O365 account normally `cpfno@ongc.co.in`; Trend's last-logged-on `ONGC\<cpf>` is the cross-check.
- Stale: >30 days stale, >90 days very stale. Windows 10 flagged unsupported.
- Reports: only rows matching our assets are stored; the rest are counted and listed, not stored.
- `.bat` delivered from `\\10.205.64.203\ank-helpdesk\common-for-all\` (team has RW); posts via `curl.exe` to the portal, else writes a result file to a drop folder on the share (else the user's Documents). Portal download = backup route, dropped if Zscaler/SmartScreen blocks it. Must handle UNC working dir (`pushd`).
- Extras in v1: Windows build + support status, BitLocker, TPM/Secure Boot, Office version/channel, domain-joined, last boot, Edge/Chrome version, local admins.

## Tasks (in order; each = tests first, own commit)

1. **Observation store** - table `portal_sw_obs`, `record()`, `latest_by_source()`. AC: server-stamped `inspected_at` (no way to pass it); unknown source/field rejected; UPDATE/DELETE refused by trigger; latest per source returned; stale flag at 30/90 days.
2. **Report ingest** - turn BigFix/Trend files (already parsed by `inventory_match`) into observations for matching assets (full build from BigFix; Trend build, agent status, last user, connection state; `data_as_of` from report). AC: matched stored, unmatched counted not stored, re-import adds rows not edits, tests use sample-shaped fixtures.
3. **Record view** - "Software and OS" section in the asset record window: latest per source side by side, history, stale/Win10 flags, inspection date read-only. AC: shown only there (no register column), User sees only own assets.
4. **Inventory match integration** - "portal-observed" column + conflict flags. AC: existing match results unchanged when no observations exist.
5. **Check script + upload** - `.bat` (reg query / sc query / whoami /upn / curl.exe), token-protected ingest endpoint, file-upload fallback. AC: parser tests with captured sample output; run on one real PC before release.
6. **Manual entry + Excel bulk template** (template in `templates/`).
7. **Release + docs** - README section, signed release, manual update steps.

## Open risks
`.bat` untested on real PCs; Zscaler-only networks cannot reach the portal (file fallback covers); matching depends on computer name = CI.
