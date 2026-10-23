"""The XM server clock: New York time plus seven hours, so server midnight is the 17:00 NY close.

MT5 writes every time as server wall-clock seconds; `server_to_utc` turns them into UTC, and
`check_clock` runs the structural checks that real bars must pass year by year (tasks/plan.md,
"Server clock to UTC").
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from vibedesk.market.bars import Timeframe

DAY = 86400
HOUR = 3600
_NEW_YORK = ZoneInfo("America/New_York")
_SERVER_SHIFT = timedelta(hours=7)


def server_offset(utc: np.ndarray) -> np.ndarray:
    """Seconds to add to UTC epoch times to get server-clock times.

    The offset is looked up once per UTC day, at 12:00. US daylight-saving switches happen early
    on a Sunday (06:00-07:00 UTC) while the market is closed, so every trading minute gets the
    exact offset.
    """
    utc = np.asarray(utc, dtype=np.int64)
    days, inverse = np.unique(utc // DAY, return_inverse=True)
    offsets = np.array([_offset_at(int(day) * DAY + DAY // 2) for day in days], dtype=np.int64)
    return offsets[inverse].reshape(utc.shape)


def server_to_utc(server: np.ndarray) -> np.ndarray:
    """UTC epoch seconds for server wall-clock seconds, as MT5 returns them.

    `utc = server - offset`, where the offset is New York's UTC offset plus 7 h at the New York
    wall time `server - 7 h`. New York switches at 02:00 on a Sunday (server 09:00, market
    closed): the skipped server hour reads as before the switch and the repeated one as its
    first pass (zoneinfo's fold=0). Both lie on server Sunday, where no bar opens.

    It agrees with `server_offset` on every minute outside server Sunday. Over the first seven
    UTC hours of each switch Sunday, `server_offset` already uses the new offset while the real
    clock still runs on the old one; this follows the real clock (tests/test_server_time.py).
    """
    server = np.asarray(server, dtype=np.int64)
    # New York switches on a whole hour, so one lookup per server hour is exact.
    hours, inverse = np.unique(server // HOUR, return_inverse=True)
    offsets = np.array([_offset_at_wall(int(hour) * HOUR) for hour in hours], dtype=np.int64)
    return server - offsets[inverse].reshape(server.shape)


# The structural checks real bars must pass, by timeframe; the import report fails a year that
# breaks one. They catch times that went wrong between MT5 and the cache. MT5 builds D1 and H4
# bars on its own server clock, though, so a whole-hour difference in XM's clock passes them.
CLOCK_RULES = {
    Timeframe.D1: "every D1 bar opens at 17:00 New York",
    Timeframe.H4: "every H4 bar opens at a server hour divisible by 4",
    Timeframe.M15: "every M15 bar sits on the 15-minute UTC grid",
}


def check_clock(utc_times: Mapping[Timeframe, np.ndarray]) -> dict[int, dict[Timeframe, int]]:
    """The structural clock checks (CLOCK_RULES) on bar open times in UTC, per calendar year.

    For each UTC year with bars, and each timeframe given with bars in that year, the number of
    bars that break the timeframe's rule: a year passes when every count is 0. The server clock
    is worked out for each bar's own instant, independently of `server_offset`.
    """
    report: dict[int, dict[Timeframe, int]] = {}
    for timeframe, utc in utc_times.items():
        if timeframe not in CLOCK_RULES:
            checked = ", ".join(CLOCK_RULES)
            raise ValueError(f"no clock rule for {timeframe}; there are rules for {checked}")
        utc = np.asarray(utc, dtype=np.int64)
        if timeframe is Timeframe.D1:
            ok = _real_server_times(utc) % DAY == 0  # server midnight is 17:00 New York
        elif timeframe is Timeframe.H4:
            ok = _real_server_times(utc) % (4 * HOUR) == 0
        else:
            ok = utc % Timeframe.M15.seconds == 0
        years = utc.astype("datetime64[s]").astype("datetime64[Y]").astype(np.int64) + 1970
        present, inverse = np.unique(years, return_inverse=True)
        breaks = np.bincount(inverse.ravel(), weights=~ok.ravel(), minlength=len(present))
        for year, count in zip(present.tolist(), breaks.tolist(), strict=True):
            report.setdefault(year, {})[timeframe] = int(count)
    return dict(sorted(report.items()))


def _offset_at(epoch: int) -> int:
    new_york = datetime.fromtimestamp(epoch, UTC).astimezone(_NEW_YORK)
    return int((new_york.utcoffset() + _SERVER_SHIFT).total_seconds())


def _offset_at_wall(server: int) -> int:
    """The server clock's UTC offset at a server wall time (New York wall time + 7 h)."""
    new_york = (datetime.fromtimestamp(server, UTC) - _SERVER_SHIFT).replace(tzinfo=_NEW_YORK)
    return int((new_york.utcoffset() + _SERVER_SHIFT).total_seconds())


def _real_server_times(utc: np.ndarray) -> np.ndarray:
    """The server clock at each UTC instant. New York switches on a whole UTC hour, so one
    lookup per hour is exact."""
    hours, inverse = np.unique(utc // HOUR, return_inverse=True)
    offsets = np.array([_offset_at(int(hour) * HOUR) for hour in hours], dtype=np.int64)
    return utc + offsets[inverse].reshape(utc.shape)
