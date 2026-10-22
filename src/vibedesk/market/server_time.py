"""The XM server clock: New York time plus seven hours, so server midnight is the 17:00 NY close."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

DAY = 86400
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


def _offset_at(epoch: int) -> int:
    new_york = datetime.fromtimestamp(epoch, UTC).astimezone(_NEW_YORK)
    return int((new_york.utcoffset() + _SERVER_SHIFT).total_seconds())
