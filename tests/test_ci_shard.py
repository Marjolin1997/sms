"""M11-I/F-04: algoritmi i shardimit të CI (funksion i pastër + plugin-i); pa DB, pa rrjet."""

import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from tests.ci_shard import shard_of

ROOT = Path(__file__).resolve().parent.parent
IDS = [
    f"tests/test_mod{m}.py::test_case_{i}[param{p}]"
    for m in range(40)
    for i in range(25)
    for p in range(2)
]


@pytest.mark.parametrize("count", [1, 2, 4, 7])
def test_every_id_lands_in_exactly_one_shard_in_range(count):
    shards = [[i for i in IDS if shard_of(i, count) == s] for s in range(count)]
    assert sum(map(len, shards)) == len(IDS)  # asnjë e humbur, asnjë e dyfishtë
    assert len({i for sh in shards for i in sh}) == len(IDS)
    assert all(0 <= shard_of(i, count) < count for i in IDS)


def test_assignment_is_stable_and_independent_of_other_tests():
    base = {i: shard_of(i, 4) for i in IDS}
    assert base == {i: shard_of(i, 4) for i in reversed(IDS)}  # i pavarur nga rendi
    subset = IDS[::3]
    assert all(shard_of(i, 4) == base[i] for i in subset)  # i pavarur nga testet e tjera
    assert shard_of("tests/test_x.py::test_a", 4) == shard_of("tests/test_x.py::test_a", 4)


def test_distribution_is_reasonably_even():
    c = Counter(shard_of(i, 4) for i in IDS)
    assert max(c.values()) - min(c.values()) < 0.15 * len(IDS) / 4


def test_plugin_requires_valid_index_and_count_and_is_inert_without_them():
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "tests.ci_shard",
    ]
    ok = subprocess.run([*cmd, "tests/test_ci_shard.py::test_assignment_is_stable_and_independent_of_other_tests"],
                        cwd=ROOT, capture_output=True, text=True)  # fmt: skip
    assert ok.returncode == 0 and "1 test collected" in ok.stdout  # pa opsione: s'filtron asgjë
    bad = subprocess.run([*cmd, "--shard-index", "4", "--shard-count", "4", "tests/test_ci_shard.py"],
                         cwd=ROOT, capture_output=True, text=True)  # fmt: skip
    assert bad.returncode != 0 and "0 <= index < count" in (bad.stdout + bad.stderr)
