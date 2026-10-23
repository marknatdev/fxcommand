"""T5: the `desk` commands, on FakeMt5 and temp folders: never the real terminal or data."""

import re
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from vibedesk.cli import DATA, main
from vibedesk.ledger import Ledger
from vibedesk.market.bars import Timeframe
from vibedesk.market.cache import CachedBarSource, read_meta
from vibedesk.market.data import MarketData
from vibedesk.market.fake_mt5 import FakeMt5, FakeTick
from vibedesk.market.synthetic import SyntheticBarSource

WEDNESDAY = datetime(2024, 2, 21, 10, 7, 30, tzinfo=UTC)
SATURDAY = datetime(2024, 2, 24, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def source():
    return SyntheticBarSource(6, date(2023, 11, 1), date(2024, 2, 26))


def desk(argv, api, clock, capsys):
    code = main(argv, api=api, clock=lambda: clock)
    return code, capsys.readouterr().out


def test_the_default_data_folder_is_the_repos(tmp_path):
    assert Path(__file__).resolve().parents[1] / "data" == DATA


# smoke -----------------------------------------------------------------------------------------


def test_smoke_reports_and_passes_on_a_live_terminal(source, capsys):
    code, out = desk(["smoke"], FakeMt5(source, now=WEDNESDAY), WEDNESDAY, capsys)

    assert code == 0 and out.rstrip().endswith("OK")
    assert "GOLD    point 0.01  digits 2" in out
    assert "live tick: GOLD +0 s from this computer's UTC clock" in out
    assert re.search(r"GOLD M15, last 24 hours: [1-9]\d* bars", out)


def test_smoke_fails_when_the_terminal_wont_start(source, capsys):
    code, out = desk(["smoke"], FakeMt5(source, fail_initialize=True), WEDNESDAY, capsys)

    assert code == 1 and "FAILED: MT5 initialize" in out


def test_smoke_fails_a_weekday_tick_hours_old(source, capsys):
    # The terminal T0 met: on a weekday its last tick was 19 hours old.
    stale = FakeMt5(source, now=WEDNESDAY - timedelta(hours=19))

    code, out = desk(["smoke"], stale, WEDNESDAY, capsys)

    assert code == 1
    assert "the freshest tick (GOLD) is 19.0 h old" in out
    assert "may not be receiving quotes (or the market is shut for a holiday)" in out


def test_smoke_fails_a_clock_running_behind_the_ticks(source, capsys):
    code, out = desk(["smoke"], FakeMt5(source, now=WEDNESDAY), WEDNESDAY - timedelta(minutes=5),
                     capsys)  # fmt: skip

    assert code == 1 and "5 min in the future" in out


def test_smoke_skips_the_tick_check_at_the_servers_weekend(source, capsys):
    code, out = desk(["smoke"], FakeMt5(source, now=SATURDAY), SATURDAY, capsys)

    assert code == 0
    assert "live tick: skipped, the server's weekend (freshest GOLD, 14.0 h old)" in out


@dataclass(frozen=True)
class PricedInfo:
    """A SymbolInfo with the price fields the real one carries."""

    name: str
    point: float
    digits: int
    swap_long: float
    swap_short: float
    swap_mode: int
    swap_rollover3days: int
    bid: float = 1234.56
    ask: float = 1234.89
    last: float = 1234.71


class PricedMt5(FakeMt5):
    def symbol_info(self, symbol):
        info = super().symbol_info(symbol)
        return None if info is None else PricedInfo(**asdict(info))

    def symbol_info_tick(self, symbol):
        tick = super().symbol_info_tick(symbol)
        return None if tick is None else FakeTick(tick.time, tick.time_msc, 4321.12, 4321.55)


def prices(bars) -> set[str]:
    return {f"{v:.{bars.digits}f}" for name in ("open", "high", "low", "close")
            for v in getattr(bars, name)}  # fmt: skip


def test_smoke_never_prints_a_price(source, capsys):
    code, out = desk(["smoke"], PricedMt5(source, now=WEDNESDAY), WEDNESDAY, capsys)

    day = source.fetch("GOLD", Timeframe.M15, WEDNESDAY - timedelta(days=1), WEDNESDAY)
    printed = set(re.findall(r"\d+\.\d+", out))
    assert code == 0
    assert not printed & ({"1234.56", "1234.89", "1234.71", "4321.12", "4321.55"} | prices(day))


# floors ----------------------------------------------------------------------------------------


def test_floors_measure_and_record_each_symbol(source, tmp_path, capsys):
    api = FakeMt5(source, swaps={"GOLD": (-83.83, 20.64)})

    code, out = desk(["floors", "--symbols", "GOLD", "EURUSD", "--data", str(tmp_path)], api,
                     WEDNESDAY, capsys)  # fmt: skip

    assert code == 0
    assert re.search(r"GOLD    floor 55 points \(p90 60, [\d,]+ ticks\)", out)
    assert "swap long -83.83 / short 20.64, charged -83.83 / 0" in out
    assert (tmp_path / "costs" / "GOLD.jsonl").exists()
    assert (tmp_path / "costs" / "EURUSD.jsonl").exists()


def test_floors_report_a_failure_per_symbol(source, tmp_path, capsys):
    code, out = desk(["floors", "--symbols", "GOLD", "EURUSD", "--data", str(tmp_path)],
                     FakeMt5(source, swap_mode=2), WEDNESDAY, capsys)  # fmt: skip

    assert code == 1
    assert "GOLD: swap_mode 2 is not 1" in out and "EURUSD: swap_mode 2 is not 1" in out


# import, refresh and the data report (Checkpoint B) ----------------------------------------


def test_import_refresh_and_data_report_end_to_end(source, tmp_path, capsys):
    data = ["--symbols", "GOLD", "--data", str(tmp_path)]
    imported = desk(["import", "--start", "2023-11-01", *data], FakeMt5(source, now=SATURDAY),
                    SATURDAY, capsys)  # fmt: skip
    refreshed = desk(["refresh", *data], FakeMt5(source, now=SATURDAY), SATURDAY, capsys)
    reported = desk(["data-report", *data], None, SATURDAY, capsys)

    assert [code for code, _ in (imported, refreshed, reported)] == [0, 0, 0]
    assert "judged through 2024-02-22" in imported[1]  # Friday waits for Monday's bars
    assert "0 retries" in refreshed[1]
    out = reported[1]
    assert "GOLD: first usable month 2023-11, judged through 2024-02-22" in out
    assert "our conversion: ok" in out and "XM's clock: ok" in out
    assert re.search(r"2024  M1 [\d,]+  M15 [\d,]+  H1 [\d,]+  H4 [\d,]+  D1 [\d,]+", out)
    assert out.rstrip().endswith("OK")
    # What the commands left, read back through MarketData, is the source after the mask.
    mask = read_meta(tmp_path, "GOLD").mask
    start = datetime(2023, 11, 1, tzinfo=UTC)
    with Ledger(":memory:") as ledger:
        market = MarketData(CachedBarSource(tmp_path), ledger)
        for timeframe in Timeframe:
            got = market.bars("GOLD", timeframe, start, SATURDAY)
            want = source.fetch("GOLD", timeframe, start, SATURDAY)
            keep = mask.keep(want.time)
            assert keep.any(), timeframe
            for name in ("time", "open", "high", "low", "close", "spread"):
                assert np.array_equal(getattr(got, name), getattr(want, name)[keep]), timeframe


def test_an_import_that_fails_says_why(source, tmp_path, capsys):
    code, out = desk(["import", "--symbols", "GOLD", "--data", str(tmp_path)],
                     FakeMt5(source, fail_initialize=True), SATURDAY, capsys)  # fmt: skip

    assert code == 1 and out.startswith("FAILED: MT5 initialize failed")


def test_the_data_report_of_an_empty_folder_fails(tmp_path, capsys):
    code, out = desk(["data-report", "--symbols", "GOLD", "--data", str(tmp_path)], None,
                     SATURDAY, capsys)  # fmt: skip

    assert code == 1 and "GOLD: no cached bars" in out


def test_no_command_prints_a_holdout_price(tmp_path, capsys):
    # A cache reaching into 2025: the import and data report hold counts and times only, and
    # smoke on a 2025 Wednesday prints none of that day's prices either.
    holdout = SyntheticBarSource(9, date(2024, 12, 1), date(2025, 2, 3))
    saturday, wednesday = datetime(2025, 2, 1, 12, tzinfo=UTC), datetime(2025, 1, 29, 10, 7,
                                                                         tzinfo=UTC)  # fmt: skip
    data = ["--symbols", "GOLD", "--data", str(tmp_path)]
    outputs = [
        desk(["import", "--start", "2024-12-01", *data], FakeMt5(holdout, now=saturday),
             saturday, capsys),
        desk(["data-report", *data], None, saturday, capsys),
        desk(["smoke", "--symbols", "GOLD"], FakeMt5(holdout, now=wednesday), wednesday, capsys),
    ]  # fmt: skip

    in_2025 = set()
    for tf in Timeframe:
        in_2025 |= prices(holdout.fetch("GOLD", tf, datetime(2025, 1, 1, tzinfo=UTC)))
    printed = set(re.findall(r"\d+\.\d+", "".join(out for _, out in outputs)))
    assert [code for code, _ in outputs] == [0, 0, 0]
    assert len(in_2025) > 1000 and not printed & in_2025


# safety ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["smoke", "floors", "import", "refresh", "data-report"])
def test_no_command_loads_metatrader5_when_given_a_fake(command, source, tmp_path, monkeypatch,
                                                        capsys):  # fmt: skip
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)  # importing it now raises
    argv = [command, "--symbols", "GOLD"] + (
        [] if command == "smoke" else ["--data", str(tmp_path)]
    )
    if command == "refresh":
        desk(["import", "--start", "2024-01-01", *argv[1:]], FakeMt5(source, now=SATURDAY),
             SATURDAY, capsys)  # fmt: skip

    code, out = desk(argv, FakeMt5(source, now=SATURDAY), SATURDAY, capsys)

    assert "mt5 extra" not in out
    assert code == (1 if command == "data-report" else 0)  # nothing imported for data-report


def test_without_a_fake_the_live_terminal_needs_the_mt5_extra(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)

    code, out = desk(["smoke"], None, WEDNESDAY, capsys)

    assert code == 1 and "uv sync --extra mt5" in out


def test_an_unknown_symbol_is_bad_usage(capsys):
    with pytest.raises(SystemExit) as usage:
        main(["import", "--symbols", "DOGEUSD"])
    assert usage.value.code == 2
