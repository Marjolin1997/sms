"""M3-b(ii): gabimet bazë (`DomainError`, `NotFound`, `Conflict`) kanë burim të vetëm `app.core.errors`.
Provat: snapshot i të gjitha klasave të gabimit të runtime-it (marrë PARA refactor-it), identiteti i aliaseve,
hartëzimi HTTP, përgjigje API, dhe garda e varësisë (asnjë modul jo-wallet s'i importon nga services.wallet)."""

import ast
import importlib
import json
import pkgutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app
from app.core import errors as core_errors
from app.main import create_app
from app.services import wallet as wallet_svc

APP = Path(__file__).resolve().parents[1] / "app"
GOLDEN = Path(__file__).parent / "golden" / "error_classes.json"
BASE_NAMES = {"WalletError", "NotFound", "Conflict"}
# Klasat që ndryshuan vendndodhje (ndryshim strukturor, jo sjelljeje): WalletError u bë DomainError.
MOVED = {
    "app.services.wallet.WalletError": ("app.core.errors.DomainError", "DomainError"),
    "app.services.wallet.NotFound": ("app.core.errors.NotFound", "NotFound"),
    "app.services.wallet.Conflict": ("app.core.errors.Conflict", "Conflict"),
    # M10-S0: autorizimi kanonik i sender-it mban gabimet e domenit (sender_ids i ri-eksporton)
    "app.services.sender_ids.InvalidSender": (
        "app.services.sender_authorization.InvalidSender",
        "InvalidSender",
    ),
    "app.services.sender_ids.SenderNotAllowed": (
        "app.services.sender_authorization.SenderNotAllowed",
        "SenderNotAllowed",
    ),
}


def _runtime_snapshot():
    mods = [
        m.name
        for m in pkgutil.walk_packages(app.__path__, "app.")
        if not m.name.endswith("__main__")
    ]
    for m in mods:
        importlib.import_module(m)
    out, seen = {}, set()

    def walk(c):
        for s in c.__subclasses__():
            if s in seen:
                continue
            seen.add(s)
            if s.__module__.startswith("app."):
                try:
                    inst = s("boom")
                except TypeError:  # p.sh. ProviderError ka konstruktor të vetin
                    inst = None
                out[f"{s.__module__}.{s.__qualname__}"] = {
                    "name": s.__qualname__, "module": s.__module__, "code": getattr(s, "code", None),
                    "app_mro": [k.__name__ for k in s.__mro__ if k.__module__.startswith("app.")],
                    "builtin_base": [k.__name__ for k in s.__mro__ if not k.__module__.startswith("app.")][:2],
                    "str": None if inst is None else str(inst),
                    "args": None if inst is None else list(inst.args), "own_init": "__init__" in s.__dict__,
                }  # fmt: skip
            walk(s)

    walk(Exception)
    http = {}
    for m in mods:
        d = getattr(sys.modules[m], "_STATUS", None) if m.startswith("app.api.") else None
        if isinstance(d, dict):
            http[m] = dict(sorted(d.items()))
    return dict(sorted(out.items())), dict(sorted(http.items()))


def _golden_after_known_moves():
    g = json.loads(GOLDEN.read_text())
    classes = {}
    for key, v in g["classes"].items():
        v = dict(v)
        v["app_mro"] = ["DomainError" if n == "WalletError" else n for n in v["app_mro"]]
        if key in MOVED:
            key, v["name"] = MOVED[key]
            v["module"] = key.rsplit(".", 1)[0]
        classes[key] = v
    return dict(sorted(classes.items())), g["http_status"]


# --- Snapshot ------------------------------------------------------------------------------------------


def test_every_runtime_error_class_keeps_name_code_message_args_and_inheritance():
    now_classes, _ = _runtime_snapshot()
    expected, _ = _golden_after_known_moves()
    assert (
        now_classes == expected
    )  # 47 klasa + 3 (M7-d) + 7 (M7-e) + 3 (M7-g) + 8 (M9-c) + 2 (M9-d) + 5 (M9-e): .code, str, args, __init__, zinxhiri i trashëgimisë
    assert (
        len(expected) == 85
    )  # M10-S0: + SenderDecisionImmutableError; M10-S2: + 6 gabime të sender_sync; M10-S3: + SenderRequestImmutableError


def test_http_status_mapping_dictionaries_are_identical():
    _, http_now = _runtime_snapshot()
    _, http_before = _golden_after_known_moves()
    assert http_now == http_before and len(http_before) == 10


def test_exactly_the_documented_classes_changed_module():
    """Klasat që HUMBËN çelësin e tyre (module.qualname) në golden = ato që u zhvendosën; klasat e reja me emër të njëjtë (p.sh. `ApplyError` e sender_sync) nuk llogariten si zhvendosje."""
    now_classes, _ = _runtime_snapshot()
    g = json.loads(GOLDEN.read_text())["classes"]
    moved = {g[k]["name"] for k in g.keys() - now_classes.keys()} - {"WalletError"}
    assert moved == {
        "NotFound",
        "Conflict",
        "InvalidSender",
        "SenderNotAllowed",
    }  # + WalletError→DomainError (alias); M10-S0: sender_authorization


# --- Aliase dhe sjellje -----------------------------------------------------------------------------------------


def test_wallet_error_is_domain_error_and_base_aliases_are_the_same_objects():
    assert wallet_svc.WalletError is core_errors.DomainError
    assert wallet_svc.NotFound is core_errors.NotFound
    assert wallet_svc.Conflict is core_errors.Conflict
    assert issubclass(core_errors.NotFound, core_errors.DomainError)
    assert issubclass(core_errors.Conflict, core_errors.DomainError)


def test_base_error_behavior_is_unchanged():
    assert core_errors.DomainError.code == "wallet_error"  # parazgjedhja e trashëguar
    e = core_errors.NotFound("contact not found")
    assert (e.code, str(e), e.args) == ("not_found", "contact not found", ("contact not found",))
    c = core_errors.Conflict("dup")
    assert (c.code, str(c)) == ("conflict", "dup")
    assert repr(core_errors.Conflict("dup")) == "Conflict('dup')"


def test_legacy_callers_catch_new_errors_by_the_old_names():
    from app.services.contacts import InvalidContact

    for exc in (core_errors.NotFound("x"), core_errors.Conflict("x"), InvalidContact("x")):
        with pytest.raises(wallet_svc.WalletError):
            raise exc
        assert isinstance(exc, core_errors.DomainError) and isinstance(exc, wallet_svc.WalletError)
    with pytest.raises(wallet_svc.NotFound):
        raise core_errors.NotFound("x")
    assert issubclass(wallet_svc.InsufficientFunds, core_errors.DomainError)
    assert issubclass(wallet_svc.InvalidAmount, wallet_svc.WalletError)
    assert wallet_svc.InsufficientFunds.code == "insufficient_funds"
    assert wallet_svc.InvalidAmount.code == "invalid_amount"


def test_errors_are_not_pickled_or_serialised_anywhere():
    for f in APP.rglob("*.py"):
        t = f.read_text()
        assert "pickle" not in t and "copyreg" not in t and "__reduce__" not in t, f.name


# --- Përgjigje API (payload-e të marra para refactor-it) --------------------------------------------------------------


@pytest.fixture
def staff():
    return TestClient(create_app(), headers={"X-Admin-Key": "test-key"})


def test_representative_api_error_payloads_are_unchanged(staff):
    c = staff
    assert (
        c.post("/v1/contacts", json={"owner_ref": "o1", "phone": "+355691230003"}).status_code
        == 201
    )

    def err(r):
        return r.status_code, r.json()

    nf = lambda what: (404, {"detail": {"code": "not_found", "message": f"{what} not found"}})  # noqa: E731
    assert err(c.get("/v1/contacts/999999", params={"owner_ref": "o1"})) == nf("contact")
    assert err(c.get("/v1/campaigns/999999", params={"owner_ref": "o1"})) == nf("campaign")
    assert err(c.get("/v1/billing/invoices/999999", params={"owner_ref": "o1"})) == nf("invoice")
    assert err(c.post("/v1/templates/999999/versions", json={"body": "x"})) == nf("template")
    assert err(c.patch("/v1/webhooks/endpoints/999999", params={"owner_ref": "o1"},
                       json={"description": "x"})) == nf("webhook endpoint")  # fmt: skip
    assert err(c.get("/v1/wallets/999999")) == nf("wallet")
    assert err(c.post("/v1/contacts", json={"owner_ref": "o1", "phone": "x"})) == (
        422, {"detail": {"code": "invalid_contact", "message": "phone must be E.164"}})  # fmt: skip
    w = c.post("/v1/wallets", json={"owner_ref": "o1", "currency": "EUR"}).json()["id"]
    assert err(c.post(f"/v1/wallets/{w}/adjustments", json={"delta": "-5", "key": "k1", "note": "test adj"})) == (
        402, {"detail": {"code": "insufficient_funds", "message": "insufficient available funds"}})  # fmt: skip
    lst = {"owner_ref": "o1", "name": "dup"}
    assert c.post("/v1/lists", json=lst).status_code == 201
    r = c.post("/v1/lists", json=lst)  # Conflict nga domain-i
    assert r.status_code == 409 and r.json()["detail"]["code"] == "conflict"


# --- Garda e varësisë -----------------------------------------------------------------------------------------------------


def _imports_base_errors_from_wallet(path: Path):
    bad = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module == "app.services.wallet":
            bad += [a.name for a in node.names if a.name in BASE_NAMES]
    return bad


def test_only_the_wallet_modules_may_use_the_base_error_aliases_from_services_wallet():
    allowed = {"services/wallet.py", "api/wallets.py"}
    offenders = {
        f.relative_to(APP).as_posix(): _imports_base_errors_from_wallet(f)
        for f in APP.rglob("*.py")
        if f.relative_to(APP).as_posix() not in allowed and _imports_base_errors_from_wallet(f)
    }
    assert offenders == {}


def test_core_errors_imports_nothing_from_the_application():
    for node in ast.walk(ast.parse((APP / "core" / "errors.py").read_text())):
        assert not isinstance(node, ast.Import | ast.ImportFrom), (
            "core.errors s'duhet të importojë asgjë"
        )


def test_domain_errors_subclass_the_neutral_base_not_the_wallet_alias():
    """Në burim: asnjë `class X(WalletError)` jashtë wallet-it; ata trashëgojnë nga DomainError/NotFound/Conflict."""
    for f in APP.rglob("*.py"):
        rel = f.relative_to(APP).as_posix()
        if rel in {"services/wallet.py", "api/wallets.py"}:
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.ClassDef):
                assert "WalletError" not in {getattr(b, "id", None) for b in node.bases}, (
                    rel,
                    node.name,
                )


def test_services_wallet_is_no_longer_imported_just_for_error_classes():
    """Varësia e kundërt: modulet që importojnë services.wallet e bëjnë për funksionalitet/entitete të wallet-it."""
    for f in APP.rglob("*.py"):
        rel = f.relative_to(APP).as_posix()
        if rel == "services/wallet.py":
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == "app.services.wallet":
                assert {a.name for a in node.names} - BASE_NAMES, rel


def test_there_is_no_import_cycle_even_counting_lazy_imports():
    """Cikli lazy `context → enterprises → wallet → scope → context` u prish: enterprises s'varet më nga wallet."""
    import ast as _ast

    mods = {}
    for f in APP.rglob("*.py"):
        name = ".".join(("app",) + f.relative_to(APP).with_suffix("").parts)
        mods[name[:-9] if name.endswith(".__init__") else name] = f

    def resolve(c):
        while c and c not in mods:
            c = c.rsplit(".", 1)[0] if "." in c else ""
        return c or None

    graph = {}
    for name, f in mods.items():
        deps = set()
        for n in _ast.walk(_ast.parse(f.read_text())):
            cs = []
            if isinstance(n, _ast.ImportFrom) and n.module:
                cs = [n.module] + [f"{n.module}.{a.name}" for a in n.names]
            elif isinstance(n, _ast.Import):
                cs = [a.name for a in n.names]
            deps |= {r for c in cs if c.startswith("app") and (r := resolve(c)) and r != name}
        graph[name] = deps
    state, stack, cycles = {}, [], []

    def dfs(v):
        state[v] = 1
        stack.append(v)
        for w in graph[v]:
            if state.get(w) == 1:
                cycles.append(stack[stack.index(w) :] + [w])
            elif w not in state:
                dfs(w)
        stack.pop()
        state[v] = 2

    for v in graph:
        if v not in state:
            dfs(v)
    assert cycles == []
