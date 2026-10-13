"""M11-H — roja "pa ofrues të vërtetë" e harness-it pilot: duhet të dështojë shpejt kur ka konfigurim të një dërgimi të jashtëm."""

import pytest

from e2e.pilot import guard


class S:
    twilio_account_sid = ""
    twilio_auth_token = ""
    http_provider_url = ""
    smtp_host = ""
    email_provider = "fake"
    payment_provider = "fake"


def test_clean_environment_passes():
    assert (
        guard.violations(
            {"SMS_ENV": "development", "SMS_EMAIL_PROVIDER": "fake", "PATH": "/bin"}, S(), ["fake"]
        )
        == []
    )


@pytest.mark.parametrize(
    "env",
    [
        {"TWILIO_ACCOUNT_SID": "AC1"},
        {"SMS_TWILIO_AUTH_TOKEN": "x"},
        {"SMS_SMTP_HOST": "smtp.example.com"},
        {"SMS_HTTP_PROVIDER_URL": "https://p.example"},
        {"VONAGE_API_KEY": "k"},
        {"STRIPE_SECRET_KEY": "sk"},
    ],
)
def test_real_provider_environment_is_refused(env):
    assert guard.violations(env)


@pytest.mark.parametrize(
    "attr,val",
    [
        ("twilio_account_sid", "AC1"),
        ("smtp_host", "smtp.example.com"),
        ("http_provider_url", "https://p"),
        ("email_provider", "smtp"),
        ("payment_provider", "stripe"),
    ],
)
def test_real_provider_settings_are_refused(attr, val):
    s = S()
    setattr(s, attr, val)
    assert guard.violations({}, s)


def test_non_fake_routes_are_refused():
    assert guard.violations({}, None, ["fake", "twilio"])
    with pytest.raises(guard.RealProviderConfigured):
        guard.assert_fake_only({"TWILIO_ACCOUNT_SID": "AC1"})
