"""Trading Window: when a Session may open new positions, in broker server time."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

DAY = 86400
WEEK_MINUTES = 7 * 1440
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _hm(s: str) -> int:
    h, m = s.split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 24 and 0 <= m < 60) or h * 60 + m > 1440:
        raise ValueError(f"invalid time {s!r}, expected HH:MM")
    return h * 60 + m


@dataclass(frozen=True)
class Blackout:
    start: str  # "HH:MM", daily
    end: str  # may wrap past midnight (e.g. 23:55 -> 00:10)

    def contains(self, minute_of_day: int) -> bool:
        a, b = _hm(self.start), _hm(self.end)
        if a <= b:
            return a <= minute_of_day < b
        return minute_of_day >= a or minute_of_day < b


@dataclass(frozen=True)
class TradingWindow:
    enabled: bool = True
    open_day: int = 0  # Monday
    open_time: str = "00:10"
    close_day: int = 4  # Friday
    close_time: str = "23:00"
    blackouts: tuple[Blackout, ...] = field(default_factory=lambda: (Blackout("23:55", "00:10"),))

    @classmethod
    def from_dict(cls, d: dict | None) -> "TradingWindow":
        if not d:
            return cls()
        w = cls(
            enabled=bool(d.get("enabled", True)),
            open_day=int(d.get("open_day", 0)),
            open_time=str(d.get("open_time", "00:10")),
            close_day=int(d.get("close_day", 4)),
            close_time=str(d.get("close_time", "23:00")),
            blackouts=tuple(Blackout(b["start"], b["end"]) for b in d.get("blackouts", [{"start": "23:55", "end": "00:10"}])),
        )
        w.validate()
        return w

    def validate(self) -> None:
        if not (0 <= self.open_day <= 6 and 0 <= self.close_day <= 6):
            raise ValueError("days must be 0 (Mon) .. 6 (Sun)")
        _hm(self.open_time), _hm(self.close_time)
        for b in self.blackouts:
            _hm(b.start), _hm(b.end)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["blackouts"] = [asdict(b) for b in self.blackouts]
        return d

    def is_open(self, server_ts: int) -> bool:
        if not self.enabled:
            return True
        weekday = (server_ts // DAY + 3) % 7
        minute_of_day = (server_ts % DAY) // 60
        mow = weekday * 1440 + minute_of_day
        start = self.open_day * 1440 + _hm(self.open_time)
        end = self.close_day * 1440 + _hm(self.close_time)
        inside = start <= mow < end if start < end else (mow >= start or mow < end)
        if not inside:
            return False
        return not any(b.contains(minute_of_day) for b in self.blackouts)

    def describe(self) -> str:
        if not self.enabled:
            return "always open"
        s = f"{DAYS[self.open_day]} {self.open_time} – {DAYS[self.close_day]} {self.close_time}"
        if self.blackouts:
            s += "; blackout " + ", ".join(f"{b.start}–{b.end}" for b in self.blackouts)
        return s + " (server time)"


@dataclass(frozen=True)
class TradingHours:
    """When the broker quotes a symbol, per weekday (0 = Monday), as [start, end) minutes of the server day.
    GOLD on XM, for example, is shut from 00:00 to 01:00 every day and all weekend. The default is
    always open, which is what a symbol without known sessions is treated as."""

    sessions: tuple[tuple[tuple[int, int], ...], ...] = tuple(((0, 1440),) for _ in range(7))

    @classmethod
    def daily(cls, start: str, end: str, weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)) -> "TradingHours":
        a, b = _hm(start), _hm(end)
        return cls(tuple(((a, b),) if d in weekdays else () for d in range(7)))

    @classmethod
    def from_dict(cls, d: dict | None) -> "TradingHours":
        if not d:
            return cls()
        return cls(tuple(tuple((int(a), int(b)) for a, b in d.get(str(i), d.get(i, []))) for i in range(7)))

    def to_dict(self) -> dict:
        return {str(i): [list(s) for s in day] for i, day in enumerate(self.sessions)}

    def is_open(self, server_ts: int) -> bool:
        weekday = (server_ts // DAY + 3) % 7
        minute = (server_ts % DAY) // 60
        return any(a <= minute < b for a, b in self.sessions[weekday])


def fill_limit(bar_seconds: int, fill_window_s: int | None = None) -> int:
    """How long after the Next Tradable Time an entry may still fill: the bar, or the Strategy's
    shorter Fill Window. Shared by live Pending Entries and every Backtest (EntryGate)."""
    return min(int(bar_seconds), int(fill_window_s)) if fill_window_s else int(bar_seconds)


def next_tradable(server_ts: int, window: TradingWindow | None = None, hours: TradingHours | None = None, horizon_days: int = 8) -> int | None:
    """The first moment at or after ``server_ts`` when both the market (``hours``) and the Session's
    Trading Window are open, to the minute; None when there is none within ``horizon_days``.

    Live Pending Entries and every Backtest use this one function, so a D1 signal at the 00:00
    close is judged at the 01:00 reopen in both (spec D18)."""
    window = window or TradingWindow(enabled=False)
    hours = hours or TradingHours()

    def ok(ts: int) -> bool:
        return hours.is_open(ts) and window.is_open(ts)

    if ok(server_ts):
        return server_ts
    ts = (server_ts // 60 + 1) * 60
    end = server_ts + horizon_days * DAY
    while ts <= end:
        if ok(ts):
            return ts
        ts += 60
    return None


def weekday(ts: int) -> int:
    """Monday=0 .. Sunday=6 for server-time epoch seconds (1970-01-01 was a Thursday)."""
    return (ts // DAY + 3) % 7


def in_weekend_close(now: int, close_time: str) -> bool:
    """From Friday ``close_time`` (server time) until the week ends. Shared by the live Weekend Close
    and every Backtest (``WeekendClose``), so the Evidence models the Session's setting."""
    wd = weekday(now)
    if wd >= 5:
        return True
    if wd != 4:
        return False
    return (now % DAY) // 60 >= _hm(close_time)


@dataclass(frozen=True)
class WeekendClose:
    """A Session's Weekend Close as a Backtest applies it (spec D6). Plain data, so it pickles.

    Friday ``close_time`` is rarely a bar boundary (22:30 falls inside GOLD's 20:00 H4 bar), so a
    position is settled at the close of the bar that contains it (its stops are checked first), not
    at the next bar's open, which lies after the weekend gap. An entry whose fill falls inside the
    span is refused, as the live engine refuses it."""

    close_time: str = "22:30"
    bar_seconds: int = 3600

    def blocks_fill(self, t: int) -> bool:
        return in_weekend_close(t, self.close_time)

    def closes_bar(self, t: int) -> bool:
        """Bar opening at ``t`` reaches Friday ``close_time`` (or the weekend) before it ends."""
        return in_weekend_close(t + self.bar_seconds - 1, self.close_time)
