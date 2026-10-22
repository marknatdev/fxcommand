"""ADR 0002 and spec §4: Holdout bars need a grant the Ledger approves, and only the market
facade may read a BarSource, so nothing else can walk around that check.
"""

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from vibedesk.market.bars import Timeframe
from vibedesk.market.data import HoldoutLocked, MarketData
from vibedesk.market.grant import HOLDOUT_START, HoldoutGrant
from vibedesk.market.synthetic import SyntheticBarSource

VIBEDESK = Path(__file__).resolve().parents[1] / "src" / "vibedesk"
FETCH = re.compile(r"\.fetch\(")

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


@pytest.fixture(scope="module")
def source():
    return SyntheticBarSource(seed=11, start=date(2024, 12, 23), end=date(2025, 1, 10))


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
    """Every `.fetch(` outside root/market, where only the facade may read a BarSource."""
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
    (tmp_path / "gauntlet").mkdir()
    (tmp_path / "gauntlet" / "sneaky.py").write_text(
        "rows = cursor.fetchall()\nbars = source.fetch('GOLD', Timeframe.D1)\n"
    )
    (tmp_path / "cli.py").write_text("bars = SyntheticBarSource(1, a, b).fetch('GOLD', tf)\n")

    hits = sorted(
        (path.relative_to(tmp_path).as_posix(), line) for path, line in find_fetch_calls(tmp_path)
    )

    assert hits == [("cli.py", 1), ("gauntlet/sneaky.py", 2)]
