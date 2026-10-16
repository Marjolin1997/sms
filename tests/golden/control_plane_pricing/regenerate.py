"""Gjeneron `cases.json` dhe `*.body` të golden-ve `cp.pricing.v1` (dokumente eksplicite → kontrata → bytes kanonike). VETËM për ndryshim të
miratuar të kontratës: `.venv/bin/python -m tests.golden.control_plane_pricing.regenerate`. Testet NUK e thërrasin."""

import json
import uuid
from pathlib import Path

from packages.contracts.control_plane.pricing import v1 as pv

HERE = Path(__file__).parent
U = lambda n: str(uuid.UUID(int=n))  # noqa: E731
EPOCH = U(0xE0)


def rule(n, channel="sms", prefix="355", operator="", price="0.050000"):
    return {
        "rule_id": U(0x300 + n),
        "channel": channel,
        "prefix": prefix,
        "operator": operator,
        "unit_price": price,
    }


def version(n, status="active", eff="2030-01-01T00:00:00.000000+00:00", rules=None, num=1):
    rules = rules or [rule(n)]
    return {"version_id": U(0x200 + n), "version": num, "status": status, "effective_from": eff,
            "content_hash": pv.rules_hash(rules), "rules": rules}  # fmt: skip


def book(n, versions, code="std", cur="EUR"):
    return {"book_id": U(0x100 + n), "code": code, "currency": cur, "versions": versions}


def asg(n, book_n, product=0xA1, eff="2030-01-01T00:00:00.000000+00:00"):
    return {
        "assignment_id": U(0x400 + n),
        "product_id": U(product),
        "price_book_id": U(0x100 + book_n),
        "effective_from": eff,
    }


def ent(n, assignments):
    return {"enterprise_id": U(0x500 + n), "assignments": assignments}


def snap(rev, enterprises, books, gen=1):
    return pv.PricingSnapshotV1.build(
        epoch=EPOCH, revision=rev, generation=gen, enterprises=enterprises, books=books
    )


def cases():
    return [
        ("empty", snap(0, [], [])),
        ("single_book_sms", snap(1, [ent(1, [asg(1, 1)])], [book(1, [version(1)])])),
        (
            "operator_and_prefix_precedence",
            snap(
                2,
                [ent(1, [asg(1, 1)])],
                [
                    book(
                        1,
                        [
                            version(
                                1,
                                rules=[
                                    rule(1, prefix="355"),
                                    rule(2, prefix="35569"),
                                    rule(3, prefix="35569", operator="27601", price="0.045000"),
                                ],
                            )
                        ],
                    )
                ],
            ),
        ),
        (
            "two_versions_one_retired",
            snap(
                3,
                [ent(1, [asg(1, 1)])],
                [
                    book(
                        1,
                        [
                            version(1, status="retired", eff="2029-01-01T00:00:00.000000+00:00"),
                            version(
                                2,
                                eff="2030-01-01T00:00:00.000000+00:00",
                                num=2,
                                rules=[rule(4, price="0.060000")],
                            ),
                        ],
                    )
                ],
            ),
        ),
        (
            "sms_and_email_books",
            snap(
                4,
                [ent(1, [asg(1, 1), asg(2, 2, product=0xA2)])],
                [
                    book(1, [version(1)]),
                    book(
                        2,
                        [version(2, rules=[rule(5, channel="email", prefix="", price="0.001500")])],
                        code="email-std",
                    ),
                ],
            ),
        ),
        (
            "two_enterprises_assignment_history",
            snap(
                5,
                [
                    ent(1, [asg(1, 1), asg(3, 1, eff="2031-01-01T00:00:00.000000+00:00")]),
                    ent(2, []),
                ],
                [book(1, [version(1)])],
            ),
        ),
        (
            "edge_micro_price",
            snap(
                6, [ent(1, [asg(1, 1)])], [book(1, [version(1, rules=[rule(6, price="0.000001")])])]
            ),
        ),
        (
            "edge_zero_price",
            snap(
                7, [ent(1, [asg(1, 1)])], [book(1, [version(1, rules=[rule(7, price="0.000000")])])]
            ),
        ),
        (
            "edge_large_revision",
            snap(9007199254740993, [ent(1, [asg(1, 1)])], [book(1, [version(1)])], gen=2),
        ),
    ]


def main() -> None:
    out = []
    for name, s in cases():
        (HERE / f"{name}.body").write_bytes(s.to_bytes())
        out.append(
            {
                "name": name,
                "body_file": f"{name}.body",
                "snapshot_hash": s.snapshot_hash,
                "parsed": s.to_dict(),
            }
        )
    (HERE / "cases.json").write_text(json.dumps(out, indent=1, ensure_ascii=True) + "\n")
    print(len(out), "cases")


if __name__ == "__main__":
    main()
