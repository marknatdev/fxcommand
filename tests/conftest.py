"""Shared test data. Twelve synthetic years of GOLD M15 and M1 take seconds to generate, so the
speed tests share one copy per session."""

from datetime import date

import pytest

from vibedesk.market.bars import Timeframe
from vibedesk.market.synthetic import SyntheticBarSource


@pytest.fixture(scope="session")
def twelve_years():
    source = SyntheticBarSource(seed=1, start=date(2014, 6, 2), end=date(2026, 6, 1))
    return source.fetch("GOLD", Timeframe.M15), source.fetch("GOLD", Timeframe.M1)
