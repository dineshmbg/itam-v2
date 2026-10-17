"""Domain sign-in for non-engineers: the six cases of samples/Domain_Login_Demo.html. Runs inside one rolled-back transaction."""
import pytest
from starlette.testclient import TestClient

from portal.app import auth, db, domain
from portal.app.main import app
from test_auth import HDR, sandbox  # noqa: F401 - sandbox is the shared rolled-back connection

CPF = 900001
AD = {"900001": ("Dom#pass1", {"mail": "a.shah@example.com"})}


@pytest.fixture()
def world(sandbox, monkeypatch):                    # noqa: F811
    con = sandbox
    con.execute("CREATE TABLE IF NOT EXISTS portal_engineer (engineer_key TEXT, ecode TEXT)")
    con.execute("DELETE FROM employee WHERE cpf_no IN (900001, 900002, 900003)")
    for cpf, status in ((900001, "ACTIVE"), (900002, "ACTIVE"), (900003, "RETIRED")):
        con.execute("INSERT INTO employee (record_status, cpf_no, employee_name) VALUES (%s,%s,%s)", (status, cpf, f"EMP {cpf}"))
    con.execute("INSERT INTO portal_setting (key, value) VALUES ('domain_enabled','true'::jsonb) ON CONFLICT (key) DO UPDATE SET value='true'::jsonb")
    monkeypatch.setattr(domain, "_directory", domain.FakeDirectory(AD))
    return con


def call(path, body):
    return TestClient(app).post(path, json=body, headers=HDR)


def test_engineer_and_existing_accounts_stay_local(world):
    auth.create_user("ANK1001", "Eng", None, "USER", "t", password="Vk7#mQ2!zLp9Rw")
    assert call("/api/auth/start", {"username": "ank1001"}).json() == {"method": "local"}
    assert call("/api/auth/start", {"username": "900001"}).json() == {"method": "domain"}
    assert call("/api/auth/login", {"username": "ANK1001", "password": "Vk7#mQ2!zLp9Rw"}).status_code == 200


def test_switch_off_changes_nothing(world):
    world.execute("UPDATE portal_setting SET value='false'::jsonb WHERE key='domain_enabled'")
    assert call("/api/auth/start", {"username": "900001"}).json() == {"method": "local"}


def test_domain_success_creates_user_account(world):
    r = call("/api/auth/login", {"username": "900001", "password": "Dom#pass1"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "USER" and r.json()["user"]["auth_source"] == "domain"
    u = db.one("SELECT * FROM portal_user WHERE username='900001'")
    assert u["password_hash"] == domain.NO_PASSWORD and u["email"] == "a.shah@example.com" and not u["must_change"]
    assert call("/api/auth/start", {"username": "900001"}).json() == {"method": "domain"}      # now an existing account, still domain
    assert call("/api/auth/login", {"username": "900001", "password": "Dom#pass1"}).status_code == 200


def test_wrong_password_counts_and_locks(world):
    call("/api/auth/login", {"username": "900001", "password": "Dom#pass1"})
    codes = [call("/api/auth/login", {"username": "900001", "password": "bad"}).status_code for _ in range(5)]
    assert codes[:4] == [401] * 4 and call("/api/auth/login", {"username": "900001", "password": "Dom#pass1"}).status_code == 423


def test_not_in_hr_or_retired_refused_without_asking_domain(world, monkeypatch):
    asked = []
    monkeypatch.setattr(domain, "_directory", type("D", (), {"bind": lambda self, *a: asked.append(a)})())
    for name in ("999999", "900003", "abc"):
        assert call("/api/auth/login", {"username": name, "password": "x"}).status_code == 403
    assert asked == []


def test_domain_down_is_not_a_bad_password(world, monkeypatch):
    monkeypatch.setattr(domain, "_directory", domain.FakeDirectory(AD, down=True))
    r = call("/api/auth/login", {"username": "900001", "password": "Dom#pass1"})
    assert r.status_code == 503 and r.json()["code"] == "domain_down"


def test_domain_account_cannot_use_portal_password_or_reset(world):
    call("/api/auth/login", {"username": "900001", "password": "Dom#pass1"})
    assert auth.request_password_reset("900001", "1.1.1.1") == "ignored"
    with pytest.raises(auth.AuthError):
        auth.login("900001", "!", "1.1.1.1", "t")
