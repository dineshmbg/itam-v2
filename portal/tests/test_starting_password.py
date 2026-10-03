"""An unused starting password stops working after N days (2026-10-03; setting `starting_pw_days`, off by default).

Roster-created accounts start with a password anyone who knows an ECODE could guess, and the Users page already warns that a dozen accounts have
never signed in. The longer such an account sits unused, the more exposed it is. With the setting on, a correct starting password is refused once it
is older than the limit (the person must ask an administrator for a reset); wrong passwords still get the same generic answer, so nothing leaks.
"""
import datetime as dt

import pytest

from portal.app import auth
from test_auth import STRONG, n, sandbox  # noqa: F401  (sandbox is a fixture)


def new_account(sandbox, name="SPW1", age_days=0):
    auth.create_user(name, "Starting Password Test", "spw@example.com", "USER", "SETUP", password=STRONG)
    uid = n(sandbox, "SELECT user_id FROM portal_user WHERE username = %s", (name,))
    sandbox.execute("UPDATE portal_user SET password_changed_at = now() - make_interval(days => %s) WHERE user_id = %s", (age_days, uid))
    return uid


def set_days(days):
    auth.save_settings({"starting_pw_days": days}, "TESTER")
    auth._settings_cache["value"] = None


def test_off_by_default_an_old_starting_password_still_signs_in(sandbox):
    new_account(sandbox, age_days=400)
    assert auth.settings(fresh=True)["starting_pw_days"] == 0
    assert auth.login("SPW1", STRONG, "10.0.0.1", "test")                        # returns a session token (the forced password change comes next)


def test_an_expired_starting_password_is_refused_with_an_explanation(sandbox):
    new_account(sandbox, age_days=10)
    set_days(7)
    with pytest.raises(auth.AuthError) as e:
        auth.login("SPW1", STRONG, "10.0.0.1", "test")
    assert e.value.code == "starting_password_expired" and "administrator" in str(e.value)
    assert n(sandbox, "SELECT count(*) FROM portal_activity WHERE username = 'SPW1' AND action = 'LOGIN_REFUSED'") == 1


def test_a_wrong_password_still_gets_the_generic_answer_even_when_expired(sandbox):
    new_account(sandbox, age_days=10)
    set_days(7)
    with pytest.raises(auth.AuthError) as e:
        auth.login("SPW1", "not-the-password-1A!", "10.0.0.1", "test")
    assert e.value.code == "bad_credentials"                                      # no hint that the account exists or that its password aged out


def test_within_the_limit_it_still_works(sandbox):
    new_account(sandbox, age_days=3)
    set_days(7)
    assert auth.login("SPW1", STRONG, "10.0.0.1", "test")


def test_a_password_the_person_chose_never_expires_this_way(sandbox):
    uid = new_account(sandbox, age_days=400)
    sandbox.execute("UPDATE portal_user SET must_change = FALSE WHERE user_id = %s", (uid,))
    set_days(7)
    assert auth.login("SPW1", STRONG, "10.0.0.1", "test")


def test_an_administrator_reset_gives_a_fresh_start(sandbox):
    uid = new_account(sandbox, age_days=30)
    set_days(7)
    out = auth.reset_password(uid, "ADMIN")
    assert auth.login("SPW1", out["temporary_password"], "10.0.0.1", "test")


def test_the_users_page_flags_expired_accounts(sandbox):
    uid = new_account(sandbox, age_days=30)
    row = auth._row(sandbox, "SELECT * FROM portal_user WHERE user_id = %s", (uid,))
    assert auth.public_user(row)["starting_pw_expired"] is False                  # setting off
    set_days(7)
    assert auth.public_user(row)["starting_pw_expired"] is True
    sandbox.execute("UPDATE portal_user SET password_changed_at = now() WHERE user_id = %s", (uid,))
    assert auth.public_user(auth._row(sandbox, "SELECT * FROM portal_user WHERE user_id = %s", (uid,)))["starting_pw_expired"] is False


def test_the_setting_is_validated(sandbox):
    for bad in (-1, 366, "abc"):
        with pytest.raises(auth.AuthError):
            auth.save_settings({"starting_pw_days": bad}, "TESTER")
    auth.save_settings({"starting_pw_days": 14}, "TESTER")
    assert auth.settings(fresh=True)["starting_pw_days"] == 14
