# Todo: domain login (see plan-domain-login.md)

- [x] User reviewed demo and answered questions (see Answers in plan)
- [x] **D1** Two-step login page and local/domain decision
- [x] **D2** Domain login against a fake directory (HR/CIPL check, auto-create USER, audit)
- [ ] Checkpoint 1: user review
- [x] **D3** Real LDAP client written + unit-tested (vendored ldap3). NOT yet proven against the real DC: run `python tools/ldap_probe.py` on the VM, then sign in once with a real account
- [x] **D4** Admin settings + Test connection
- [ ] **D5** Lifecycle and hardening
- [ ] Checkpoint 2: security review, full suite, check.ps1
- [ ] **D6** Docs and signed release
