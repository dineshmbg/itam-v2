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


class _Conn:
    """ldap3.Connection stand-in: remembers how it was asked to bind and answers from a script."""
    script, calls = {}, []

    def __init__(self, server, user=None, password=None, authentication=None, **kw):
        self.user, self.auth, self.password = user, authentication, password
        self.result, self.entries = {}, []
        _Conn.calls.append((user, authentication))

    def bind(self):
        r = _Conn.script.get(self.auth, "invalidCredentials")
        if isinstance(r, Exception):
            raise r
        self.result = {"description": r}
        return r == "success"

    def search(self, *a, **k):
        return False

    def unbind(self):
        pass


@pytest.fixture()
def ldap(monkeypatch):
    import ldap3
    _Conn.script, _Conn.calls = {}, []
    monkeypatch.setattr(ldap3, "Connection", _Conn)
    return ldap3


def test_ldap_ntlm_with_domain_name_and_cpf(ldap):
    d = domain.LdapDirectory("dc", 389, "ONGC")
    _Conn.script = {ldap.NTLM: "success"}
    assert d.bind("900001", "pw") == {} and _Conn.calls == [("ONGC\900001", ldap.NTLM)]


def test_ldap_wrong_password_is_none_and_empty_never_asks(ldap):
    d = domain.LdapDirectory("dc", 389, "ONGC")
    assert d.bind("900001", "bad") is None and d.bind("900001", "") is None and len(_Conn.calls) == 1


def test_ldap_ntlm_refused_is_down_unless_simple_allowed(ldap):
    _Conn.script = {ldap.NTLM: "unwillingToPerform", ldap.SIMPLE: "success"}
    with pytest.raises(domain.DirectoryDown):
        domain.LdapDirectory("dc", 389, "ONGC").bind("900001", "pw")
    assert domain.LdapDirectory("dc", 389, "ONGC", allow_simple=True).bind("900001", "pw") == {}
    assert [a for _, a in _Conn.calls] == [ldap.NTLM, ldap.NTLM, ldap.SIMPLE]


def test_ldap_unreachable_is_down(ldap):
    _Conn.script = {ldap.NTLM: OSError("timed out")}
    with pytest.raises(domain.DirectoryDown):
        domain.LdapDirectory("dc", 389, "ONGC").bind("900001", "pw")


def test_admin_saves_validates_and_tests_domain_settings(world, monkeypatch):
    from conftest import fake_user
    fake_user(monkeypatch, role="ADMIN")
    ok = {"domain_enabled": True, "domain_host": "dc.example.org", "domain_port": 389, "domain_netbios": "ongc", "domain_base_dns": ["ou=a,dc=b", ""], "domain_allow_simple": False}
    r = call("/api/admin/domain/save", {"domain": ok})
    assert r.status_code == 200 and r.json()["domain"]["domain_netbios"] == "ONGC" and r.json()["domain"]["domain_base_dns"] == ["ou=a,dc=b"]
    assert call("/api/admin/domain/save", {"domain": {**ok, "domain_port": 99999}}).status_code == 400
    assert call("/api/admin/domain/save", {"domain": {**ok, "domain_host": ""}}).status_code == 400       # on, but no controller
    assert call("/api/admin/domain/save", {"domain": {**ok, "domain_host": "bad host!"}}).status_code == 400
    monkeypatch.setattr(domain.LdapDirectory, "probe", lambda self: {"ntlm": "ok", "simple": "ok"})
    assert call("/api/admin/domain/test", {"domain": ok}).json() == {"ntlm": "ok", "simple": "ok"}
    assert TestClient(app).get("/api/admin/domain").json()["domain"]["domain_host"] == "dc.example.org"


def test_plain_user_cannot_reach_domain_settings(world, monkeypatch):
    from conftest import fake_user
    fake_user(monkeypatch, role="USER")
    assert call("/api/admin/domain/save", {"domain": {}}).status_code == 403
    assert TestClient(app).get("/api/admin/domain").status_code == 403


def test_probe_reports_ntlm_ok_when_controller_rejects_made_up_account(ldap):
    _Conn.script = {ldap.NTLM: "invalidCredentials", ldap.SIMPLE: "unwillingToPerform"}
    out = domain.LdapDirectory("dc", 389, "ONGC").probe()
    assert out["ntlm"] == "ok" and out["simple"].startswith("refused")
