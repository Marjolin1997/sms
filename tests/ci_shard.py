"""Shardim deterministik i suitës për CI (plugin pytest: `-p tests.ci_shard --shard-index I --shard-count N`).

Çdo test caktohet në shard-in `sha1(nodeid) mod N`: i qëndrueshëm mes ekzekutimeve, pa rastësi, pa varësi nga
numri i testeve të tjera, pa dublikim dhe pa mungesa (çdo nodeid ka saktësisht një shard). Pa opsionet, plugin-i
s'bën asgjë. Çdo test është hermetik (fixture-i autouse `schema` rikrijon skemën), prandaj ndarja për test është e
sigurt. Në fund printon (dhe shton te $GITHUB_STEP_SUMMARY) indeksin, numrin e mbledhur dhe rezultatet."""

import hashlib
import os
import time


def shard_of(nodeid: str, count: int) -> int:
    return int(hashlib.sha1(nodeid.encode("utf-8")).hexdigest(), 16) % count


def pytest_addoption(parser):
    g = parser.getgroup("ci-shard")
    g.addoption("--shard-index", type=int, default=None, help="indeksi i shard-it (0-bazë)")
    g.addoption("--shard-count", type=int, default=None, help="numri total i shard-eve")


def _params(config):
    idx, cnt = config.getoption("--shard-index"), config.getoption("--shard-count")
    if idx is None and cnt is None:
        return None
    if idx is None or cnt is None or cnt < 1 or not 0 <= idx < cnt:
        raise SystemExit(
            "--shard-index and --shard-count must be given together, with 0 <= index < count"
        )
    return idx, cnt


def pytest_configure(config):
    config._ci_shard_t0 = time.time()


def pytest_collection_modifyitems(config, items):
    p = _params(config)
    if p is None:
        return
    idx, cnt = p
    keep = [it for it in items if shard_of(it.nodeid, cnt) == idx]
    drop = [it for it in items if shard_of(it.nodeid, cnt) != idx]
    config._ci_shard_stats = (idx, cnt, len(keep), len(items))
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    st = getattr(config, "_ci_shard_stats", None)
    if st is None:
        return
    idx, cnt, kept, total = st
    s = terminalreporter.stats
    n = {k: len(s.get(k, [])) for k in ("passed", "failed", "skipped", "error")}
    runtime = f"{time.time() - config._ci_shard_t0:.0f}s"
    line = (
        f"shard {idx + 1}/{cnt} (index {idx}): collected {kept} of {total}; passed {n['passed']}, "
        f"failed {n['failed']}, skipped {n['skipped']}, errors {n['error']}; runtime {runtime}"
    )
    terminalreporter.write_sep("-", line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"- {line}\n")
        except OSError:
            pass
