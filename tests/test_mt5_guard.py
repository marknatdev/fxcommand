"""Spec §3.3 and §9.1: Vibe Desk reads MT5 and nothing more.

Three layers, each tested here:
- only market/mt5_api.py imports MetaTrader5;
- no code in src/ names an MT5 function off the allowlist, on any object (an API handed in as a
  parameter is caught as well as the module);
- at run time the real module is reached only through ReadOnlyMt5, which refuses every other name.
"""

import ast
import importlib.util
import types
from pathlib import Path

import pytest

from vibedesk.market.mt5_api import ALLOWED, ALLOWED_CALLS, ReadOnlyMt5

SRC = Path(__file__).resolve().parents[1] / "src"
ONLY_IMPORTER = SRC / "vibedesk" / "market" / "mt5_api.py"

# Every function of the MetaTrader5 module (5.0.6231). `version` is left out: it is harmless and
# too common a word to forbid as an attribute anywhere in src/.
MT5_FUNCTIONS = frozenset(
    {
        "account_info", "copy_rates_from", "copy_rates_from_pos", "copy_rates_range",
        "copy_ticks_from", "copy_ticks_range", "history_deals_get", "history_deals_total",
        "history_orders_get", "history_orders_total", "initialize", "last_error", "login",
        "market_book_add", "market_book_get", "market_book_release", "order_calc_margin",
        "order_calc_profit", "order_check", "order_send", "orders_get", "orders_total",
        "positions_get", "positions_total", "shutdown", "symbol_info", "symbol_info_tick",
        "symbol_select", "symbols_get", "symbols_total", "terminal_info",
    }
)  # fmt: skip
FORBIDDEN = MT5_FUNCTIONS - ALLOWED_CALLS


def scan(root: Path, importer: Path | None = None) -> list[tuple[str, int, str]]:
    """(file, line, what) for every MetaTrader5 import outside `importer` and every attribute
    named after an MT5 function off the allowlist."""
    hits = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        name = path.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
                hits += [
                    (name, node.lineno, alias.name)
                    for alias in node.names
                    if modules[0] == "MetaTrader5" and alias.name not in ALLOWED
                ]
            else:
                modules = []
            if any(m.split(".")[0] == "MetaTrader5" for m in modules) and path != importer:
                hits.append((name, node.lineno, "import MetaTrader5"))
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN:
                hits.append((name, node.lineno, node.attr))
            if isinstance(node, ast.Constant) and node.value == "MetaTrader5" and path != importer:
                hits.append((name, node.lineno, "'MetaTrader5'"))  # importlib by name
    return hits


def test_src_reads_mt5_only_through_the_allowlist():
    assert scan(SRC, ONLY_IMPORTER) == []


def test_the_scan_catches_what_it_must(tmp_path):
    (tmp_path / "orders.py").write_text("import MetaTrader5 as mt5\nmt5.order_send(request)\n")
    (tmp_path / "account.py").write_text("def peek(api):\n    return api.account_info()\n")
    (tmp_path / "deep.py").write_text("def history(api):\n    api.copy_rates_from_pos('GOLD')\n")
    (tmp_path / "sneaky.py").write_text(
        "import importlib\nmt5 = importlib.import_module('MetaTrader5')\n"
    )
    (tmp_path / "names.py").write_text("from MetaTrader5 import positions_get, symbol_info\n")
    (tmp_path / "fine.py").write_text("def bars(api):\n    return api.copy_rates_range('GOLD')\n")

    hits = {(file, what) for file, _, what in scan(tmp_path)}

    assert hits == {
        ("orders.py", "import MetaTrader5"),
        ("orders.py", "order_send"),
        ("account.py", "account_info"),
        ("deep.py", "copy_rates_from_pos"),
        ("sneaky.py", "'MetaTrader5'"),
        ("names.py", "import MetaTrader5"),
        ("names.py", "positions_get"),
    }


def fake_module():
    names = {name: (lambda *a, **k: "called") for name in MT5_FUNCTIONS}
    return types.SimpleNamespace(**names, TIMEFRAME_M15=15, COPY_TICKS_INFO=1)


def test_read_only_mt5_refuses_everything_off_the_allowlist():
    api = ReadOnlyMt5(fake_module())

    for name in sorted(FORBIDDEN):
        with pytest.raises(AttributeError, match="allowlist"):
            getattr(api, name)
    assert api.copy_rates_range() == "called"
    assert api.TIMEFRAME_M15 == 15
    with pytest.raises(AttributeError, match="read-only"):
        api.order_send = print


@pytest.mark.skipif(
    importlib.util.find_spec("MetaTrader5") is None, reason="the mt5 extra is not installed"
)
def test_the_real_module_behind_read_only_mt5():
    # Importing the module does not connect to a terminal; nothing here calls it.
    import MetaTrader5

    api = ReadOnlyMt5(MetaTrader5)
    public = {name for name in dir(MetaTrader5) if not name.startswith("_")}

    functions = {name for name in public if name[0].islower()} - {"version"}
    assert public >= ALLOWED
    assert functions == MT5_FUNCTIONS  # the scan's list is the real module's, no more, no less
    for name in sorted(public - ALLOWED):
        with pytest.raises(AttributeError, match="allowlist"):
            getattr(api, name)
