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
