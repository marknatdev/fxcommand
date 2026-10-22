"""Spec §9.1: Layer A never trades, so no order-related MT5 call may appear in src/."""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
FORBIDDEN = re.compile(r"\b(order_send|order_check|positions_\w*|orders_\w*)\b")


def find_forbidden(root: Path) -> list[tuple[Path, int, str]]:
    hits = []
    for path in root.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = FORBIDDEN.search(line)
            if match:
                hits.append((path, number, match.group(0)))
    return hits


def test_src_has_no_order_calls():
    assert find_forbidden(SRC) == []


def test_guard_catches_an_order_call(tmp_path):
    (tmp_path / "sneaky.py").write_text("import MetaTrader5 as mt5\nmt5.order_send(request)\n")
    (tmp_path / "also.py").write_text("mt5.positions_get()\n")

    hits = sorted((path.name, line, name) for path, line, name in find_forbidden(tmp_path))

    assert hits == [("also.py", 1, "positions_get"), ("sneaky.py", 2, "order_send")]
