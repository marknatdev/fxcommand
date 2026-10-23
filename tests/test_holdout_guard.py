"""ADR 0002 and spec §4: Holdout bars need a grant the Ledger approves, and only the market
facade may read a BarSource, so nothing else can walk around that check.

Every facade test runs twice: over synthetic bars, and over the Parquet cache holding the same
bars (CachedBarSource). Only market/cache.py may touch Parquet, so the cache has no other door.
"""

import ast
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from test_cache import assert_same, store

from vibedesk.market.bars import Timeframe
from vibedesk.market.cache import CachedBarSource, record_import
from vibedesk.market.data import HoldoutLocked, MarketData
from vibedesk.market.grant import HOLDOUT_START, HoldoutGrant
from vibedesk.market.synthetic import SyntheticBarSource

VIBEDESK = Path(__file__).resolve().parents[1] / "src" / "vibedesk"
# A BarSource's fetch, or the cache's unmasked read: only market/ may call either.
FETCH = re.compile(r"\.fetch\(|\bread_unmasked\(")

HOLDOUT = int(HOLDOUT_START.timestamp())
BEFORE = datetime(2024, 12, 30, tzinfo=UTC)
AFTER = datetime(2025, 1, 8, tzinfo=UTC)
GRANT = HoldoutGrant(request_id=1, family_id=7, token="s3cret")


class FakeLedger:
    """A GrantVerifier that holds approved, unspent (grant, symbol) pairs, as the Ledger does."""

    def __init__(self) -> None:
        self.approved = {(GRANT, "GOLD")}
        self.calls = 0

    def verify(self, grant: HoldoutGrant, symbol: str) -> bool:
        self.calls += 1
        return (grant, symbol) in self.approved

    def spend(self, grant: HoldoutGrant) -> None:
        self.approved = {pair for pair in self.approved if pair[0] != grant}


def synthetic():
    return SyntheticBarSource(seed=11, start=date(2024, 12, 23), end=date(2025, 1, 10))


@pytest.fixture(scope="module", params=["synthetic", "cache"])
def source(request, tmp_path_factory):
    if request.param == "synthetic":
        return synthetic()
    root = tmp_path_factory.mktemp("cache")
    store(root, synthetic())
    record_import(root, "GOLD", datetime(2025, 1, 10, tzinfo=UTC))
    return CachedBarSource(root)


@pytest.fixture
def ledger():
    return FakeLedger()


@pytest.fixture
def market(source, ledger):
    return MarketData(source, ledger)


@pytest.mark.parametrize(
    "start, end",
    [(BEFORE, AFTER), (BEFORE, HOLDOUT_START + timedelta(seconds=1)), (HOLDOUT_START, AFTER)],
    ids=["spanning", "one-second-in", "inside"],
)
def test_a_range_reaching_the_holdout_is_refused_without_a_grant(market, start, end):
    with pytest.raises(HoldoutLocked, match="Holdout Request"):
        market.bars("GOLD", Timeframe.M15, start, end)


def test_a_range_ending_exactly_at_the_holdout_start_is_allowed(market, ledger):
    bars = market.bars("GOLD", Timeframe.M15, BEFORE, HOLDOUT_START)
    with_grant = market.bars("GOLD", Timeframe.M15, BEFORE, HOLDOUT_START, grant=GRANT)

    assert bars.time[-1] == HOLDOUT - Timeframe.M15.seconds
    assert np.array_equal(with_grant.time, bars.time)
    assert ledger.calls == 0  # a research range never asks the Ledger


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_research_bars_never_close_inside_the_holdout(market, timeframe):
    """The H4 and D1 bars opening at 22:00 UTC on 2024-12-31 hold Holdout prices."""
    bars = market.bars("GOLD", timeframe, BEFORE, HOLDOUT_START)

    assert len(bars) > 0
    assert bars.time[-1] + timeframe.seconds <= HOLDOUT


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_the_facade_serves_the_synthetic_bars_whatever_the_source(market, timeframe):
    """So the guard tests over the cache test the cache, not an empty folder."""
    expected = synthetic().fetch("GOLD", timeframe, BEFORE, AFTER)

    assert_same(market.bars("GOLD", timeframe, BEFORE, AFTER, grant=GRANT), expected)


@pytest.mark.parametrize(
    "grant, symbol",
    [
        (HoldoutGrant(request_id=1, family_id=7, token="guessed"), "GOLD"),
        (HoldoutGrant(request_id=1, family_id=8, token="s3cret"), "GOLD"),
        (GRANT, "EURUSD"),
    ],
    ids=["forged", "other-family", "other-symbol"],
)
def test_a_grant_the_verifier_rejects_is_refused(market, ledger, grant, symbol):
    with pytest.raises(HoldoutLocked, match="Holdout Request"):
        market.bars(symbol, Timeframe.M15, BEFORE, AFTER, grant=grant)
    assert ledger.calls == 1


def test_an_approved_grant_opens_the_holdout(market, source):
    bars = market.bars("GOLD", Timeframe.M15, BEFORE, AFTER, grant=GRANT)
    daily = market.bars("GOLD", Timeframe.D1, BEFORE, AFTER, grant=GRANT)

    assert bars.time[-1] >= HOLDOUT
    assert np.array_equal(bars.time, source.fetch("GOLD", Timeframe.M15, BEFORE, AFTER).time)
    assert HOLDOUT - 2 * 3600 in daily.time  # the D1 bar the locked range leaves out


def test_a_grant_stops_working_once_it_is_spent(market, ledger):
    market.bars("GOLD", Timeframe.M15, BEFORE, AFTER, grant=GRANT)
    ledger.spend(GRANT)

    with pytest.raises(HoldoutLocked, match="Holdout Request"):
        market.bars("GOLD", Timeframe.M15, BEFORE, AFTER, grant=GRANT)


@pytest.mark.parametrize(
    "start, end",
    [
        (datetime(2024, 12, 30), HOLDOUT_START),
        (BEFORE, datetime(2025, 1, 1)),
        (HOLDOUT_START, BEFORE),
    ],
    ids=["naive-start", "naive-end", "reversed"],
)
def test_naive_or_reversed_times_are_rejected(market, start, end):
    with pytest.raises(ValueError):
        market.bars("GOLD", Timeframe.M15, start, end)


def find_fetch_calls(root: Path) -> list[tuple[Path, int]]:
    """Every `.fetch(` or `read_unmasked(` outside root/market, where only the facade may read
    a BarSource and only the cache reads itself without the mask."""
    hits = []
    for path in root.rglob("*.py"):
        if path.relative_to(root).parts[0] == "market":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if FETCH.search(line):
                hits.append((path, number))
    return hits


def test_only_the_market_package_reads_a_bar_source():
    assert find_fetch_calls(VIBEDESK) == []


def test_guard_catches_a_fetch_outside_the_market_package(tmp_path):
    (tmp_path / "market").mkdir()
    (tmp_path / "market" / "data.py").write_text("bars = self._source.fetch(symbol, tf)\n")
    (tmp_path / "market" / "cache.py").write_text("bars = read_unmasked(root, symbol, tf)\n")
    (tmp_path / "gauntlet").mkdir()
    (tmp_path / "gauntlet" / "sneaky.py").write_text(
        "rows = cursor.fetchall()\nbars = source.fetch('GOLD', Timeframe.D1)\n"
    )
    (tmp_path / "cli.py").write_text("bars = SyntheticBarSource(1, a, b).fetch('GOLD', tf)\n")
    (tmp_path / "report.py").write_text("bars = cache.read_unmasked(root, 'GOLD', tf)\n")

    hits = sorted(
        (path.relative_to(tmp_path).as_posix(), line) for path, line in find_fetch_calls(tmp_path)
    )

    assert hits == [("cli.py", 1), ("gauntlet/sneaky.py", 2), ("report.py", 1)]


# Any of these can open a Parquet file, so none may be used outside market/cache.py.
PARQUET_LIBRARIES = {"pyarrow", "fastparquet", "polars", "duckdb", "pandas"}
PARQUET_READERS = {"read_table", "read_parquet", "read_pandas", "ParquetFile", "ParquetDataset"}
ONLY_PARQUET = "market/cache.py"


def find_parquet_access(root: Path) -> list[tuple[str, int, str]]:
    """(file, line, what) outside root/market/cache.py for every import of a Parquet library,
    every Parquet reader named, and every string holding a `.parquet` path or a library's name
    (an import by name). Docstrings may talk about Parquet."""
    hits = []
    for path in sorted(root.rglob("*.py")):
        name = path.relative_to(root).as_posix()
        if name == ONLY_PARQUET:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
                hits += [
                    (name, node.lineno, alias.name)
                    for alias in node.names
                    if alias.name in PARQUET_READERS
                ]
            else:
                modules = []
            hits += [
                (name, node.lineno, f"import {module}")
                for module in modules
                if module.split(".")[0] in PARQUET_LIBRARIES
            ]
            if isinstance(node, ast.Attribute) and node.attr in PARQUET_READERS:
                hits.append((name, node.lineno, node.attr))
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and (".parquet" in node.value or node.value.split(".")[0] in PARQUET_LIBRARIES)
            ):
                hits.append((name, node.lineno, repr(node.value)))
    return hits


def test_only_the_cache_touches_parquet():
    assert find_parquet_access(VIBEDESK) == []


def test_the_parquet_guard_catches_what_it_must(tmp_path):
    (tmp_path / "market").mkdir()
    (tmp_path / "market" / "cache.py").write_text(
        "import pyarrow.parquet as pq\ntable = pq.read_table('2024.parquet')\n"
    )
    (tmp_path / "market" / "importer.py").write_text(
        '"""Fills data/bars/{symbol}/{timeframe}/{year}.parquet."""\n'
        "import importlib\npq = importlib.import_module('pyarrow.parquet')\n"
    )
    (tmp_path / "gauntlet").mkdir()
    (tmp_path / "gauntlet" / "peek.py").write_text("from pyarrow import parquet\n")
    (tmp_path / "cli.py").write_text("rows = reader.read_table(folder / 'GOLD.parquet')\n")
    (tmp_path / "report.py").write_text("from pandas import read_parquet\n")
    (tmp_path / "fine.py").write_text(
        '"""Reads bars through MarketData, never the Parquet files."""\nrows = db.read_rows()\n'
    )

    hits = {(file, what) for file, _, what in find_parquet_access(tmp_path)}

    assert hits == {
        ("market/importer.py", "'pyarrow.parquet'"),
        ("gauntlet/peek.py", "import pyarrow"),
        ("cli.py", "read_table"),
        ("cli.py", "'GOLD.parquet'"),
        ("report.py", "import pandas"),
        ("report.py", "read_parquet"),
    }
