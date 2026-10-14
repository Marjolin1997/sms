"""M11-I/F-03: siguria e nisjes në prodhim (proxy hops, DLR/provider, Central). Vetëm vlera testi."""

import pytest

from app.core.config import Settings
from apps.central.core.config import Settings as CentralSettings
from apps.central.main import create_app

STRONG = "x7Kq9" * 6  # 30 karaktere, jo placeholder
SENTINEL = "ZZ-SENTINEL-NEVER-PRINT-ZZ"


def prod(**kw) -> Settings:
    kw.setdefault("trusted_proxy_hops", 1)
    return Settings(_env_file=None, env="production", **kw)


def problems(**kw) -> list[str]:
    return prod(**kw).production_problems()


def f03(ps: list[str]) -> list[str]:
    keys = ("TRUSTED_PROXY", "DLR_SECRETS", "TWILIO", "SMTP_HOST")
    return [p for p in ps if any(k in p for k in keys)]


# ---- proxy hops --------------------------------------------------------------------------
@pytest.mark.parametrize("hops", [0, 2, 5])
def test_production_rejects_unsafe_trusted_proxy_hops(hops):
    ps = problems(trusted_proxy_hops=hops)
    assert any("SMS_TRUSTED_PROXY_HOPS must be 1" in p for p in ps)


def test_production_accepts_hops_1():
    assert not [p for p in problems() if "TRUSTED_PROXY" in p]


def test_dev_unchanged_for_hops():
    s = Settings(_env_file=None)  # development, hops=0
    assert s.env != "production" and s.trusted_proxy_hops == 0
    s.validate_production()  # no-op jashtë prodhimit


# ---- DLR / provider ----------------------------------------------------------------------
def test_dlr_not_required_when_no_provider_active():
    assert not f03(problems())  # parazgjedhje: pa provider HTTP/Twilio
    assert not f03(problems(dlr_secrets={}))


@pytest.mark.parametrize("secrets", [{}, {"http": ""}, {"http": "short"}, {"http": "changeme" * 4}])
def test_active_http_provider_requires_strong_dlr_secret(secrets):
    ps = problems(http_provider_url="https://prov.example/send", dlr_secrets=secrets)
    assert any("SMS_DLR_SECRETS must contain a strong secret" in p and "'http'" in p for p in ps)


def test_active_http_provider_with_strong_secret_passes():
    ps = problems(http_provider_url="https://prov.example/send", dlr_secrets={"http": STRONG})
    assert not f03(ps)


def test_custom_http_provider_name_is_the_key_checked():
    kw = dict(http_provider_url="https://p.example/s", http_provider_name="acme")
    assert f03(problems(**kw, dlr_secrets={"http": STRONG}))  # çelësi i gabuar
    assert not f03(problems(**kw, dlr_secrets={"acme": STRONG}))


def test_weak_entry_rejected_even_when_provider_inactive():
    ps = problems(dlr_secrets={"other": "test-secret"})
    assert any("entry 'other' is weak" in p for p in ps)


def test_optional_provider_credentials_not_globally_mandatory():
    ps = problems()  # asnjë kredencial provider-i
    assert not f03(ps)
    # http_provider_key është opsionale edhe kur provider-i është aktiv
    ps = problems(http_provider_url="https://p.example/s", dlr_secrets={"http": STRONG})
    assert not f03(ps)


def test_twilio_requires_both_halves_or_neither():
    assert not f03(problems())
    assert not f03(problems(twilio_account_sid="ACtest", twilio_auth_token=STRONG))
    for half in ({"twilio_account_sid": "ACtest"}, {"twilio_auth_token": STRONG}):
        assert any("must be set together" in p for p in problems(**half))


def test_twilio_does_not_need_hmac_dlr_secret():
    ps = problems(twilio_account_sid="ACtest", twilio_auth_token=STRONG, dlr_secrets={})
    assert not f03(ps)  # verifikim me X-Twilio-Signature, jo dlr_secrets


def test_smtp_host_required_only_when_smtp_selected():
    assert any("SMS_SMTP_HOST" in p for p in problems(email_provider="smtp"))
    assert not f03(problems(email_provider="smtp", smtp_host="smtp.example"))
    assert not f03(problems(email_provider="other"))


# ---- pa sekrete në mesazhe ---------------------------------------------------------------
def test_no_secret_values_in_messages():
    with pytest.raises(RuntimeError) as e:
        Settings(
            _env_file=None,
            env="production",
            trusted_proxy_hops=0,
            http_provider_url="https://p.example/s",
            http_provider_key=SENTINEL,
            twilio_auth_token=SENTINEL,
            smtp_password=SENTINEL,
            dlr_secrets={"http": "test" + SENTINEL, "x": "dev" + SENTINEL},
        ).validate_production()
    msg = str(e.value)
    assert "SMS_DLR_SECRETS" in msg and "SMS_TRUSTED_PROXY_HOPS" in msg
    assert SENTINEL not in msg


def test_validate_production_still_raises_existing_gates():
    with pytest.raises(RuntimeError, match="unsafe production configuration"):
        prod(webhook_allow_http=True).validate_production()
    assert any("SMS_EMAIL_PROVIDER must not be 'fake'" in p for p in problems())  # parazgjedhja
    assert any("PostgreSQL" in p for p in problems())  # SQLite default


# ---- Central -----------------------------------------------------------------------------
def cprod(**kw) -> CentralSettings:
    return CentralSettings(_env_file=None, env="production", **kw)


@pytest.mark.parametrize("hops", [0, 2])
def test_central_rejects_unsafe_hops(hops):
    assert any("CENTRAL_TRUSTED_PROXY_HOPS must be 1" in p for p in
               cprod(trusted_proxy_hops=hops).production_problems())  # fmt: skip


def test_central_accepts_hops_1_and_disabled_mailer():
    assert cprod(trusted_proxy_hops=1).production_problems() == []


def test_central_smtp_needs_host_and_from_only_when_selected():
    assert cprod(trusted_proxy_hops=1, mailer="smtp").production_problems()
    ok = cprod(trusted_proxy_hops=1, mailer="smtp", smtp_host="h.example", smtp_from="a@b.example")
    assert ok.production_problems() == []


def test_central_dev_unchanged():
    assert CentralSettings(_env_file=None).env == "development"


def _central_prod(monkeypatch, **over):
    from apps.central.core import config as cc

    monkeypatch.setattr(cc.settings, "env", "production")
    monkeypatch.setattr(cc.settings, "auth_secret", "A" * 48)
    for k, v in over.items():
        monkeypatch.setattr(cc.settings, k, v)


def test_central_create_app_rejects_unsafe_hops(monkeypatch):
    _central_prod(monkeypatch, trusted_proxy_hops=0)
    with pytest.raises(RuntimeError, match="CENTRAL_TRUSTED_PROXY_HOPS"):
        create_app()


def test_central_existing_gates_still_fail(monkeypatch):
    _central_prod(monkeypatch, trusted_proxy_hops=1, mailer="fake")
    with pytest.raises(RuntimeError, match="CENTRAL_MAILER"):
        create_app()
    _central_prod(monkeypatch, trusted_proxy_hops=1, mailer="disabled", auth_secret="")
    with pytest.raises(RuntimeError, match="CENTRAL_AUTH_SECRET"):
        create_app()
    _central_prod(monkeypatch, trusted_proxy_hops=1, allow_unverified_auto_registration=True)
    with pytest.raises(RuntimeError, match="AUTO_REGISTRATION"):
        create_app()


def test_central_create_app_message_has_no_secret(monkeypatch):
    _central_prod(monkeypatch, trusted_proxy_hops=0, auth_secret=SENTINEL * 2,
                  smtp_password=SENTINEL)  # fmt: skip
    with pytest.raises(RuntimeError) as e:
        create_app()
    assert SENTINEL not in str(e.value)
