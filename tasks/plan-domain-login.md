# Plan: Domain (Active Directory) login for non-engineers

Status: **DRAFT, questions answered; ready to start D1** (`samples/Domain_Login_Demo.html`). Nothing is built in the portal. Separate from `tasks/plan.md` (PM module), which is unfinished and untouched.

## Agreed requirements
- Typed user name found in the **engineer register** -> login unchanged (local portal password, 2FA, lockout as today).
- Not found -> **domain login** against **Active Directory over LDAP (port 389)**.
- A domain user must also exist in the **HR / CIPL data**, else refused (checked before the domain controller is contacted).
- Domain passwords are never stored or logged.

## Answers (from user, 2026-10-17)
1. Host `vdainf01adc01.ongc.ongcgroup.co.in`, port **389**. Search bases (two OUs):
   - `ou=ankleshwar,ou=westernregion,dc=ongc,dc=ongcgroup,dc=co,dc=in`
   - `ou=365 user_ankleshwar,ou=ankleshwar,ou=gateway location - vadodara,dc=ongc,dc=ongcgroup,dc=co,dc=in`
   (user typed "doc=co" and "dc=ongcgroup, doc=co": taken as a typo for `dc=co`; confirm against AD.)
2. Domain login name = **CPF no** = `employee.cpf_no` (HR master). The HR/CIPL check matches on it.
3. Domain user = USER (row-scoped, no Calls/Inward/Outward/RMA).
4. Domain login is enough: no 2FA for domain users.
5. Yes: admin settings page with on/off and Test connection.
6. Yes: inactive HR record blocks the next login.

## Decisions (round 2, 2026-10-17)
- **Plain LDAP on port 389, no TLS** (user's choice). Accepted risk: on the office LAN a simple bind sends the password readable. Mitigation planned: bind with **NTLM as `ONGC\<cpfno>`** (challenge-response, password is not sent in clear); fall back to a simple bind with the same name only if the admin turns that on in settings. TLS (StartTLS) stays a settings switch for later; no CA file needed now.
- Bind name = `ONGC\<cpfno>`. The NetBIOS domain `ONGC` is a setting (confirm the real short name).
- Base DN typo fixed: `dc=co`. Both OUs are used as search bases (only for the Test-connection check and for reading the display name/e-mail after a successful bind; the login itself does not need a search).

## Confirmed (round 3)
- NetBIOS domain is `ONGC`. The Ubuntu VM can reach the DC on 389.

## Still open
- Whether the DC accepts NTLM binds is unknown. D3 tests it with a real account. The client tries NTLM first and uses a simple bind on plain LDAP only if the admin setting "allow simple bind" is on (default off), so the cleartext-password choice is a deliberate admin decision.

## Dependency graph
settings + schema -> LDAP client (bind, mockable) -> login branch in `auth.login` -> API + login page two-step -> admin settings/test -> audit + docs.

## Tasks (vertical slices)
- **D1 Two-step login page + branch decision** (S): `POST /api/login/start {username}` returns `{method: local|domain}` without revealing more than needed. Accept: engineer -> local, others -> domain; answers identical in timing/shape for unknown names. Verify: pytest + browser.
- **D2 Domain login with a fake directory** (M): `portal/app/domain.py` with an LDAP interface and an in-test fake; HR/CIPL check; auto-create/refresh USER account (`auth_source='domain'`, no hash); session + audit. Accept: demo's 6 cases pass as tests; lockout shared with local. Verify: pytest.
- **Checkpoint 1**: user reviews working flow against the fake directory.
- **D3 Real LDAP bind** (M): `ldap3` vendored in `portal/vendor/wheels`, NTLM bind as `ONGC\cpfno` (TLS optional switch), 5 s timeout, no referrals, escape DN/filter input. Accept: bind against the real DC with a test account; DC-down gives the "not reachable" message. Verify: manual on a machine that reaches the DC.
- **D4 Admin settings** (S): domain, DC list, base DN, CA, on/off, "Test connection" button; ADMIN only; changes in the change log.
- **D5 Lifecycle + hardening** (S): inactive HR record blocks login; domain accounts cannot use forgot-password or local password change; rate limiting.
- **Checkpoint 2**: security review (agent-skills:security-auditor), full suite, `check.ps1`.
- **D6 Docs + release** (S): README, manual update steps, signed release.

## Risks
- DC unreachable from the VM -> domain users locked out; engineers unaffected by design.
- Name clash: a domain name equal to a future engineer name; engineer register wins.
- LDAP injection via the user name: escape + allow-list characters.
