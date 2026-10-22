from datetime import UTC, datetime

import numpy as np
import pytest

from vibedesk.market.server_time import server_offset

HOUR = 3600


def epoch(*args) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp())


@pytest.mark.parametrize(
    "utc, hours",
    [
        (epoch(2026, 10, 21, 12), 3),  # matches the 2026-10-21 probe: tick time ran UTC+3
        (epoch(2026, 1, 15, 12), 2),
        (epoch(2026, 3, 6, 20), 2),  # Friday before the US switch on 2026-03-08
        (epoch(2026, 3, 8, 21), 3),  # Sunday 21:00 UTC is server Monday 00:00 after the switch
        (epoch(2026, 3, 9, 1), 3),
        (epoch(2026, 10, 30, 20), 3),  # Friday before the US switch back on 2026-11-01
        (epoch(2026, 11, 1, 22), 2),  # Sunday 22:00 UTC is server Monday 00:00 in winter
    ],
)
def test_server_clock_is_new_york_plus_seven(utc, hours):
    assert server_offset(np.array([utc]))[0] == hours * HOUR


def test_server_offset_handles_empty_input():
    assert server_offset(np.array([], dtype=np.int64)).shape == (0,)
