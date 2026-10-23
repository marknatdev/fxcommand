"""Exchange-local time for session Blocks; daylight saving is handled per zone.

London and New York switch daylight saving on different dates, so a London-open range and a
New York-open range drift apart in UTC for a few weeks each spring and autumn.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np

DAY = 86400

ZONES = {
    "london": ZoneInfo("Europe/London"),
    "new_york": ZoneInfo("America/New_York"),
    "tokyo": ZoneInfo("Asia/Tokyo"),
}

# Session opens in exchange-local wall time.
SESSION_OPENS = {
    "asia": ("tokyo", time(9, 0)),
    "london_open": ("london", time(8, 0)),
    "ny_open": ("new_york", time(8, 0)),
}


def local_offset(utc: np.ndarray, zone: str) -> np.ndarray:
    """Seconds to add to UTC epoch times to get the zone's wall-clock time.

    Looked up once per UTC day at 12:00. European and US switches happen early on a Sunday
    (01:00 and 06:00-07:00 UTC), while the market is closed, so every trading bar is exact.
    """
    utc = np.asarray(utc, dtype=np.int64)
    tz = ZONES[zone]
    days, inverse = np.unique(utc // DAY, return_inverse=True)
    offsets = np.array(
        [
            int(
                datetime.fromtimestamp(int(d) * DAY + DAY // 2, UTC)
                .astimezone(tz)
                .utcoffset()
                .total_seconds()
            )
            for d in days
        ],
        dtype=np.int64,
    )
    return offsets[inverse].reshape(utc.shape)


def local_minute_of_day(utc: np.ndarray, zone: str) -> np.ndarray:
    """Minutes since local midnight in the zone, for each UTC epoch time."""
    utc = np.asarray(utc, dtype=np.int64)
    return ((utc + local_offset(utc, zone)) % DAY) // 60


def session_opens(session: str, start_utc: int, end_utc: int) -> np.ndarray:
    """UTC epoch times of the session's opens on local Monday to Friday, covering the range."""
    zone, wall_time = SESSION_OPENS[session]
    tz = ZONES[zone]
    first = datetime.fromtimestamp(start_utc, UTC).astimezone(tz).date() - timedelta(days=1)
    last = datetime.fromtimestamp(end_utc, UTC).astimezone(tz).date() + timedelta(days=1)
    opens = []
    day: date = first
    while day <= last:
        if day.weekday() < 5:
            opens.append(int(datetime.combine(day, wall_time, tzinfo=tz).timestamp()))
        day += timedelta(days=1)
    return np.array(opens, dtype=np.int64)
