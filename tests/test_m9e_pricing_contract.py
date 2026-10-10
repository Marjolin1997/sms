"""M9-e — kontrata `cp.pricing.v1`: golden, hash-e, validim strikt, precedenca e përbashkët, rrumbullakimi."""

import ast
import copy
import json
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from packages.contracts.control_plane.pricing import v1 as pv
from tests.golden.control_plane_pricing import regenerate as gen

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).parent / "golden" / "control_plane_pricing"
CASES = json.loads((GOLDEN / "cases.json").read_text())


def base():
    return copy.deepcopy(
        next(c for c in CASES if c["name"] == "operator_and_prefix_precedence")["parsed"]
    )


def test_package_is_a_stdlib_only_leaf():
    src = (ROOT / "packages/contracts/control_plane/pricing/v1.py").read_text()
    mods = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0])
    assert mods <= {
        "hashlib",
        "json",
        "re",
        "uuid",
        "dataclasses",
        "datetime",
        "decimal",
        "typing",
    }, mods


def test_fixture_inventory():
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names)) == 9
    assert {p.name for p in GOLDEN.glob("*.body")} == {c["body_file"] for c in CASES}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_golden_bytes_roundtrip_and_hash(case):
    body = (GOLDEN / case["body_file"]).read_bytes()
    s = pv.PricingSnapshotV1.from_bytes(body)
    assert (
        s.to_bytes() == body
        and s.snapshot_hash == case["snapshot_hash"]
        and json.loads(body) == case["parsed"]
    )
    assert pv.body_hash(s.doc["enterprises"], s.doc["books"]) == s.snapshot_hash


def test_golden_is_reproduced_by_the_generator_without_writing():
    for (name, s), case in zip(gen.cases(), CASES, strict=True):
        assert s.to_bytes() == (GOLDEN / case["body_file"]).read_bytes(), name


def test_canonical_form_is_independent_of_input_order_and_ascii():
    d = base()
    d["books"][0]["versions"][0]["rules"].reverse()
    d = {k: d[k] for k in reversed(list(d))}
    b = pv.PricingSnapshotV1.parse(d).to_bytes()
    assert (
        b == (GOLDEN / "operator_and_prefix_precedence.body").read_bytes()
        and b.decode("ascii")
        and b" " not in b
    )


@pytest.mark.parametrize(
    "mutate,why",
    [
        (
            lambda d: d["books"][0]["versions"][0]["rules"][0].update(unit_price="9.000000"),
            "content_hash",
        ),  # rregull e ndryshuar
        (
            lambda d: d["books"][0]["versions"][0]["rules"].pop(),
            "content_hash",
        ),  # rregull e munguar (version i paplotë)
        (
            lambda d: d["books"][0]["versions"][0].update(
                effective_from="2031-01-01T00:00:00.000000+00:00"
            ),
            "snapshot_hash",
        ),
        (lambda d: d["enterprises"][0]["assignments"].pop(), "snapshot_hash"),
        (lambda d: d.update(snapshot_hash="0" * 64), "snapshot_hash"),
    ],
)
def test_incomplete_or_tampered_snapshots_do_not_validate(mutate, why):
    d = base()
    mutate(d)
    with pytest.raises(pv.ContractError):
        pv.PricingSnapshotV1.parse(d)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(x=1),
        lambda d: d.pop("books"),
        lambda d: d.update(schema="cp.pricing.v2"),
        lambda d: d.update(epoch="nope"),
        lambda d: d.update(revision=-1),
        lambda d: d.update(revision=True),
        lambda d: d.update(authorization_generation=0),
        lambda d: d["books"][0].update(code="Bad Code"),
        lambda d: d["books"][0].update(currency="eur"),
        lambda d: d["books"][0]["versions"][0].update(status="draft"),
        lambda d: d["books"][0]["versions"][0].update(rules=[]),
        lambda d: d["books"][0]["versions"][0]["rules"][0].update(unit_price=0.05),
        lambda d: d["books"][0]["versions"][0]["rules"][0].update(unit_price="-0.050000"),
        lambda d: d["books"][0]["versions"][0]["rules"][0].update(unit_price="0.05"),
        lambda d: d["books"][0]["versions"][0]["rules"][0].update(prefix="0355"),
        lambda d: d["books"][0]["versions"][0]["rules"][0].update(prefix="+355"),
        lambda d: d["books"][0]["versions"][0]["rules"][0].update(channel="push"),
        lambda d: d["books"][0]["versions"][0]["rules"][1].update(
            prefix="355", operator=""
        ),  # scope i dyfishtë
        lambda d: d["books"][0]["versions"][0]["rules"][1].update(
            rule_id=d["books"][0]["versions"][0]["rules"][0]["rule_id"]
        ),
        lambda d: d["enterprises"][0]["assignments"][0].update(
            price_book_id="00000000-0000-0000-0000-00000000dead"
        ),
        lambda d: d["books"].append(copy.deepcopy(d["books"][0])),
        lambda d: d["enterprises"].append(copy.deepcopy(d["enterprises"][0])),
    ],
)
def test_invalid_snapshots_are_rejected(mutate):
    d = base()
    mutate(d)
    with pytest.raises(pv.ContractError):
        pv.PricingSnapshotV1.parse(d, verify_hash=False) if False else pv.PricingSnapshotV1.parse(d)


def test_email_rules_have_empty_scope_and_one_per_version():
    d = copy.deepcopy(next(c for c in CASES if c["name"] == "sms_and_email_books")["parsed"])
    r = d["books"][1]["versions"][0]["rules"][0]
    r["prefix"] = "355"
    with pytest.raises(pv.ContractError):
        pv.PricingSnapshotV1.parse(d)


def test_unsupported_schema_and_invalid_json():
    d = base()
    d["schema"] = "x"
    with pytest.raises(pv.UnsupportedSchemaError):
        pv.PricingSnapshotV1.parse(d)
    with pytest.raises(pv.ContractError):
        pv.PricingSnapshotV1.from_bytes(b"{no")


# --- rregullat e përbashkëta ---------------------------------------------------------------------------------------------


def rules(*specs):
    return [{"prefix": p, "operator": o, "price": pr} for p, o, pr in specs]


def test_precedence_longest_prefix_then_operator_specific_deterministically():
    c = rules(("355", "", "a"), ("35569", "", "b"), ("35569", "27601", "c"), ("3556", "", "d"))
    assert pv.pick_rule(c, "")["price"] == "b"  # prefiksi më i gjatë i përgjithshëm
    assert (
        pv.pick_rule(c, "27601")["price"] == "c"
    )  # operatori specifik mbi të përgjithshmin brenda të njëjtit prefiks
    assert pv.pick_rule(c, "99999")["price"] == "b"  # operator i panjohur ⇒ rregulla e përgjithshme
    assert (
        pv.pick_rule(rules(("355", "27601", "x")), "") is None
    )  # rregull operatori nuk vlen pa operator
    assert pv.pick_rule([], "") is None
    import random

    for _ in range(20):  # rendi i kandidatëve s'ndikon rezultatin
        random.shuffle(c)
        assert pv.pick_rule(c, "27601")["price"] == "c"


def test_candidate_prefixes_and_number_validation():
    assert pv.candidate_prefixes("+35569123") == [
        "3",
        "35",
        "355",
        "3556",
        "35569",
        "355691",
        "3556912",
        "35569123",
    ]
    for bad in ("0355", "123", "abc", "+", "", None):
        with pytest.raises(pv.ContractError):
            pv.candidate_prefixes(bad)  # type: ignore[arg-type]


def v(eff, status="active", n=0):
    return {"id": n, "version_id": str(n), "status": status, "effective_from": eff}


def test_version_selection_latest_effective_not_retired_and_no_silent_fallback():
    t = datetime(2030, 6, 1, tzinfo=UTC)
    vs = [
        v("2030-01-01T00:00:00.000000+00:00", n=1),
        v("2030-05-01T00:00:00.000000+00:00", n=2),
        v("2031-01-01T00:00:00.000000+00:00", n=3),
    ]
    assert pv.select_version(vs, t)[0]["id"] == 2  # e ardhmja s'zgjidhet
    assert pv.select_version(vs, datetime(2030, 3, 1))[0]["id"] == 1  # naive = UTC
    assert pv.select_version(vs, datetime(2029, 1, 1, tzinfo=UTC)) == (None, "no_version")
    vs[1]["status"] = "retired"  # tërhequr ⇒ s'zgjidhet kurrë, dhe NUK bie te versioni 1
    assert pv.select_version(vs, t) == (None, "retired")


def test_assignment_selection_by_product_and_time():
    a = [{"product_id": "p1", "effective_from": "2030-01-01T00:00:00.000000+00:00", "x": 1},
         {"product_id": "p1", "effective_from": "2031-01-01T00:00:00.000000+00:00", "x": 2},
         {"product_id": "p2", "effective_from": "2030-01-01T00:00:00.000000+00:00", "x": 3}]  # fmt: skip
    assert pv.select_assignment(a, "p1", datetime(2030, 6, 1, tzinfo=UTC))["x"] == 1
    assert pv.select_assignment(a, "p1", datetime(2031, 6, 1, tzinfo=UTC))["x"] == 2
    assert pv.select_assignment(a, "p2", datetime(2030, 6, 1, tzinfo=UTC))["x"] == 3
    assert pv.select_assignment(a, "p3", datetime(2030, 6, 1, tzinfo=UTC)) is None
    assert pv.select_assignment(a, "p1", datetime(2029, 6, 1, tzinfo=UTC)) is None


# --- rrumbullakimi / Decimal --------------------------------------------------------------------------------------------


def test_line_total_is_exact_decimal_with_an_explicit_rounding_rule():
    assert pv.line_total(D("0.050000"), 3) == D("0.150000")
    assert pv.line_total(D("0.000001"), 1) == D("0.000001")
    assert pv.line_total(D("0.333333"), 7) == D("2.333331")  # saktësisht (pa float)
    assert pv.line_total(D("99999999999999.999999"), 1) == D("99999999999999.999999")
    assert pv.line_total(D("0.1"), 3) == D("0.300000")  # 0.1×3 s'jep 0.30000000000000004
    assert str(pv.line_total(D("0.050000"), 0)) == "0.000000"
    for bad_price, bad_qty in ((0.05, 3), (D("0.05"), 1.5), (D("0.05"), True), (D("0.05"), -1)):
        with pytest.raises(pv.ContractError):
            pv.line_total(bad_price, bad_qty)  # type: ignore[arg-type]
    # çmimi i pavlefshëm (7 shifra) kuantizohet me ROUND_HALF_UP në kontekst eksplicit, jo me kontekstin implicit të Python
    assert pv.line_total(D("0.0000005"), 1) == D("0.000001") and pv.line_total(
        D("0.0000004"), 1
    ) == D("0.000000")


def test_format_price_rules():
    assert pv.format_price(D("1.5")) == "1.500000"
    for bad in (1.5, D("-1"), D("1.0000001"), "1"):
        with pytest.raises(pv.ContractError):
            pv.format_price(bad)  # type: ignore[arg-type]
