"""Tekstet e serverit për përdoruesit fundorë: shqip si parazgjedhje në prodhim, anglisht me SMS_DEFAULT_LANGUAGE=en."""

import pytest

from app.core.config import settings
from app.core.texts import tr


@pytest.fixture
def sq(monkeypatch):
    monkeypatch.setattr(settings, "default_language", "sq")


def test_tr_language_switch(monkeypatch):
    monkeypatch.setattr(settings, "default_language", "en")
    assert tr("Unsubscribe") == "Unsubscribe"
    monkeypatch.setattr(settings, "default_language", "sq")
    assert tr("Unsubscribe") == "Çregjistrohu"
    assert tr("një tekst i panjohur") == "një tekst i panjohur"  # pa përkthim: kthehet si është


def test_unsubscribe_page_albanian(client, sq):
    r = client.get("/u/not-a-token")
    assert r.status_code == 404
    assert "Kjo lidhje nuk është e vlefshme." in r.text and "lang=sq" in r.text


def test_email_footer_albanian(sq):
    from app.services.email_mime import _footer_html

    assert "Çregjistrohu" in _footer_html("https://x.test/u/abc")
