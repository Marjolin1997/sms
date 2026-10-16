"""M3-d2 — kontrata e nënshkrimit V1 (outgoing webhook): ekstraktim pa ndryshim bytes.

Vektorët janë STATIKË, nxjerrë një herë me `openssl dgst -sha256 -hmac` mbi `f"{ts}." + body`.
Golden-et e d1 (`test_webhook_golden.py`) mbeten të pandryshuar dhe janë prova kryesore.
"""

import ast
import hashlib
import hmac
import inspect
import re
from pathlib import Path

import pytest

from app.contracts import signature
from app.contracts.signature import TOLERANCE_S, sign_v1, verify_v1
from app.services import webhooks

ROOT = Path(__file__).resolve().parents[1] / "app"
SECRET = "whsec_test_contract_v1"

VECTORS = [  # (secret, ts, body, v1 hex) — openssl-derived
    (SECRET, 1700000000, b'{"a":1}',
     "198079317c5e8f6d288ba27a63dd80b12a16f513c416e9cdab35ac5a63d05a52"),
    (SECRET, 1700000000, b"",
     "5132180e2d8757e1312cf80cf4a9d0a7bc2eb5971a4d8dfff313316a219f2a98"),
    ("whsec_other", 0, b"x",
     "bd49eb17e6491d7e1cd9ce5a114bb044ba4c01d43b1f87f142a61b99040d4f90"),
]  # fmt: skip


@pytest.mark.parametrize("secret,ts,body,hexd", VECTORS)
def test_sign_v1_static_vectors(secret, ts, body, hexd):
    assert sign_v1(secret, ts, body) == f"t={ts},v1={hexd}"
    assert re.fullmatch(r"t=\d+,v1=[0-9a-f]{64}", sign_v1(secret, ts, body))


def test_legacy_names_are_the_contract_functions():
    assert webhooks.sign is sign_v1
    assert webhooks.verify_signature is verify_v1
    assert list(inspect.signature(webhooks.sign).parameters) == ["secret", "timestamp", "body"]
    assert list(inspect.signature(webhooks.verify_signature).parameters) == [
        "secret", "header", "body", "tolerance", "now",
    ]  # fmt: skip
    assert inspect.signature(webhooks.verify_signature).parameters["tolerance"].default == 300
    assert TOLERANCE_S == 300


def test_matches_independent_hmac_construction():
    body = b'{"id":"evt_1"}'
    mac = hmac.new(SECRET.encode(), b"1700000000." + body, hashlib.sha256).hexdigest()
    assert sign_v1(SECRET, 1700000000, body) == f"t=1700000000,v1={mac}"


def test_one_bit_body_secret_and_timestamp_changes():
    body = b'{"a":1}'
    base = sign_v1(SECRET, 100, body)
    assert sign_v1(SECRET, 100, bytes([body[0] ^ 1]) + body[1:]) != base
    assert sign_v1(SECRET, 101, body) != base
    assert sign_v1(SECRET + "x", 100, body) != base
    assert sign_v1(SECRET, 100, body) == base


# --- verify: sjellja e ruajtur ---------------------------------------------------------

BODY, TS = b'{"a":1}', 1700000000
SIG = sign_v1(SECRET, TS, BODY)
V1 = SIG.split("v1=")[1]


def test_verify_tolerance_boundary():
    assert verify_v1(SECRET, SIG, BODY, now=TS)
    assert verify_v1(SECRET, SIG, BODY, now=TS + 299)
    assert verify_v1(SECRET, SIG, BODY, now=TS + 300)  # kufi: `> tolerance` refuzon
    assert not verify_v1(SECRET, SIG, BODY, now=TS + 301)
    assert verify_v1(SECRET, SIG, BODY, now=TS - 300)  # abs(): edhe e ardhmja
    assert not verify_v1(SECRET, SIG, BODY, now=TS - 301)
    assert not verify_v1(SECRET, SIG, BODY, tolerance=10, now=TS + 11)


def test_verify_rejects_wrong_secret_body_and_uppercase_hex():
    assert not verify_v1("whsec_other", SIG, BODY, now=TS)
    assert not verify_v1(SECRET, SIG, BODY + b" ", now=TS)
    assert not verify_v1(SECRET, f"t={TS},v1={V1.upper()}", BODY, now=TS)  # hex lowercase vetëm


@pytest.mark.parametrize(
    "header",
    [
        "", "garbage", "t=", f"t=abc,v1={V1}", f"v1={V1}",  # mungon/jo-int `t`
        f"t={TS}", f"t={TS},v1=", f"t={TS},v1=deadbeef",  # mungon/gabim `v1`
        f"t={TS},v1", f"t={TS},,v1={V1}",  # çift pa `=`
        f"t={TS}, v1={V1}", f"t = {TS},v1={V1}",  # hapësira → çelës ndryshe
    ],
)  # fmt: skip
def test_verify_malformed_headers_return_false(header):
    assert verify_v1(SECRET, header, BODY, now=TS) is False


def test_verify_duplicate_keys_last_one_wins():
    assert verify_v1(SECRET, f"v1=bad,t={TS},v1={V1}", BODY, now=TS)
    assert not verify_v1(SECRET, f"v1={V1},t={TS},v1=bad", BODY, now=TS)
    assert verify_v1(SECRET, f"t=1,t={TS},v1={V1}", BODY, now=TS)


def test_verify_extra_unknown_parts_are_ignored_so_a_v2_can_be_appended_later():
    assert verify_v1(SECRET, f"t={TS},v1={V1},v2=whatever", BODY, now=TS)


def test_characterization_now_zero_falls_back_to_wall_clock():
    """`now or time.time()`: now=0 ≡ None (sjellje e ruajtur 1:1, jo e miratuar si dizajn)."""
    assert (
        verify_v1(SECRET, sign_v1(SECRET, 5, BODY), BODY, now=0) is False
    )  # ts=5 është shumë e vjetër


def test_characterization_non_ascii_v1_raises_type_error_from_compare_digest():
    """Sjellje e ruajtur (vëzhgim sigurie, pa ndryshim): hmac.compare_digest(str jo-ASCII) hedh TypeError."""
    with pytest.raises(TypeError):
        verify_v1(SECRET, f"t={TS},v1=é", BODY, now=TS)


def test_verify_uses_timing_safe_comparison():
    src = inspect.getsource(signature.verify_v1)
    assert "hmac.compare_digest(" in src and "==" not in src.split('"""')[-1]


# --- kufiri i modulit (AST) ----------------------------------------------------------------

ALLOWED_STDLIB = {"hashlib", "hmac", "time", "json", "dataclasses", "datetime", "typing"}


def _imports(path: Path) -> set[str]:
    out = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            assert n.level == 0, "importe relative nuk lejohen në contracts"
            out.add((n.module or "").split(".")[0])
    return out


def test_contracts_package_is_stdlib_only_and_signature_is_a_leaf():
    files = sorted((ROOT / "contracts").glob("*.py"))
    assert {f.name for f in files} == {
        "__init__.py",
        "events.py",
        "signature.py",
    }  # asnjë modul speculativ
    for f in files:
        bad = _imports(f) - ALLOWED_STDLIB
        assert not bad, (f.name, bad)  # asnjë app.*, sqlalchemy, fastapi, pydantic, httpx
    assert _imports(ROOT / "contracts" / "signature.py") <= {"hashlib", "hmac", "time"}


def test_only_the_webhook_services_import_the_contracts():
    importers = set()
    for p in ROOT.rglob("*.py"):
        if "contracts" in p.parts:
            continue
        for n in ast.walk(ast.parse(p.read_text())):
            if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("app.contracts"):
                importers.add(p.relative_to(ROOT).as_posix())
    assert importers == {"services/webhooks.py", "services/events.py"}


def test_outgoing_signature_header_is_built_in_one_place_only():
    hits = [
        p.relative_to(ROOT).as_posix()
        for p in ROOT.rglob("*.py")
        if "contracts" not in p.parts and "X-SMS-Signature" in p.read_text()  # contracts: vetëm doc
    ]
    assert hits == ["services/webhooks.py"]


# --- ndarja nga DLR hyrës ---------------------------------------------------------------


def test_incoming_dlr_signature_is_a_different_contract_and_untouched():
    from app.api import webhooks as dlr

    assert dlr.verify_signature is not webhooks.verify_signature
    body = b'{"provider_message_id":"x","status":"delivered"}'
    mac = hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert dlr.verify_signature("k", body, mac)  # hex pa prefiks
    assert dlr.verify_signature("k", body, f"sha256={mac}")  # prefiks opsional
    assert not dlr.verify_signature("k", body, sign_v1("k", 1, body))  # V1 s'pranohet si DLR
    assert not verify_v1("k", f"sha256={mac}", body, now=1)  # DLR s'pranohet si V1
    src = (ROOT / "api" / "webhooks.py").read_text()
    assert "app.contracts" not in src and "sign_v1" not in src
    assert "app.contracts" not in (ROOT / "api" / "public.py").read_text()
