"""Unit tests for Layer B and C Wave 0 contract interfaces and stubs."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibedesk.app.api_spec import (
    LAYER_BC_ENDPOINT_TABLE,
)
from vibedesk.app.settings import load
from vibedesk.arena import (
    LIVE_GATE_MIN_TRADES,
    ArenaControls,
    ArenaRuntime,
    ArenaStepResult,
    ArenaStrategy,
    FillTrackKind,
    PaperAccount,
    PaperTrade,
    StrategyStatus,
    TwinType,
    check_demo_ready,
    compute_leaderboard,
    evaluate_live_gate,
    generate_fills_report,
    price_fill,
    record_override,
)
from vibedesk.blocks.custom import (
    CustomBlockRegistry,
    validate_custom_block_code,
)
from vibedesk.blocks.spec import StrategySpec, validate
from vibedesk.nightshift import (
    NightShiftConfig,
    NightShiftState,
    compile_morning_brief,
    format_morning_brief,
    run_night_shift,
)
from vibedesk.notify import NoticeKind
from vibedesk.regime import (
    RegimeDecision,
    RegimeReason,
    compute_daily_features,
    evaluate_regimes,
)


def _dummy_spec() -> StrategySpec:
    return validate(
        {
            "name": "London breakout",
            "symbol": "GOLD",
            "timeframe": "H1",
            "direction": "long",
            "entry": {"block": "donchian_breakout", "params": {"period": 20}},
            "filters": [],
            "exit": {"stop": {"block": "atr_stop", "params": {"mult": 2.0}}},
            "sizing": {"block": "fixed_risk", "params": {"risk_pct": 1.0}},
        }
    )


# --- Arena Strategy & Accounts Tests ---


def test_arena_strategy_init_and_accounts():
    spec = _dummy_spec()
    strategy = ArenaStrategy(
        strategy_id=1,
        hypothesis_id=10,
        spec=spec,
        symbol="GOLD",
        timeframe="H1",
        twin_type=TwinType.STANDARD,
        status=StrategyStatus.ACTIVE,
    )
    assert strategy.id == 1
    assert strategy.symbol == "GOLD"
    assert strategy.timeframe == "H1"
    assert strategy.status == StrategyStatus.ACTIVE
    assert strategy.twin_type == TwinType.STANDARD

    # Two parallel accounts initialized
    assert strategy.account_backtest.balance == 10_000.0
    assert strategy.account_live.balance == 10_000.0

    # Step on active strategy raises NotImplementedError in Wave 0
    with pytest.raises(NotImplementedError, match="ArenaStrategy.step"):
        strategy.step({"time": 1000, "open": 2000.0, "close": 2005.0})

    # Catch-up raises NotImplementedError in Wave 0
    with pytest.raises(NotImplementedError, match="ArenaStrategy.catch_up"):
        strategy.catch_up([{"time": 1000}])

    # Paused strategy returns empty step result without error
    strategy.status = StrategyStatus.PAUSED
    res = strategy.step({"time": 1000})
    assert isinstance(res, ArenaStepResult)
    assert res.trades_opened == 0


def _sample_trades() -> list[PaperTrade]:
    return [
        PaperTrade(
            id=1,
            symbol="GOLD",
            direction=1,
            entry_time=1000,
            exit_time=2000,
            entry_price=2000.0,
            exit_price=2020.0,
            size=1.0,
            pnl=500.0,
            r=2.0,
            cost_paid=10.0,
            exit_reason="target",
        ),
        PaperTrade(
            id=2,
            symbol="GOLD",
            direction=1,
            entry_time=2100,
            exit_time=3000,
            entry_price=2020.0,
            exit_price=2010.0,
            size=1.0,
            pnl=-250.0,
            r=-1.0,
            cost_paid=10.0,
            exit_reason="stop",
        ),
    ]


def test_paper_account_empty_metrics():
    acct = PaperAccount(initial_balance=10_000.0, balance=10_000.0, equity=10_500.0)
    assert acct.net_r == 0.0
    assert acct.trades_count == 0
    assert acct.profit_factor == 0.0
    assert acct.max_drawdown == 0.0


def test_paper_account_trade_returns():
    acct = PaperAccount(closed_trades=_sample_trades())
    assert acct.net_r == 1.0
    assert acct.trades_count == 2


def test_paper_account_trade_performance():
    acct = PaperAccount(closed_trades=_sample_trades())
    assert acct.profit_factor == 2.0  # 500 / 250
    assert acct.win_rate == 0.5


def test_fill_pricing_tracks():
    # Long trade with spread floor = 20, bar spread = 15, live spread = 25
    bt_pricing = price_fill(
        track=FillTrackKind.BACKTEST_COST,
        bar_open=100.0,
        direction=1,
        point=0.01,
        spread_floor=20.0,
        bar_spread=15.0,
        live_spread=25.0,
        slippage_points=1.0,
    )
    # uses max(15, 20) = 20 pts + 1 pt slippage = 21 pts = 0.21
    assert round(bt_pricing.fill_price, 3) == 100.21
    assert bt_pricing.spread_points == 20.0

    live_pricing = price_fill(
        track=FillTrackKind.LIVE_SPREAD,
        bar_open=100.0,
        direction=1,
        point=0.01,
        spread_floor=20.0,
        bar_spread=15.0,
        live_spread=25.0,
        slippage_points=1.0,
    )
    # uses live spread = 25 pts + 1 pt slippage = 26 pts = 0.26
    assert round(live_pricing.fill_price, 3) == 100.26
    assert live_pricing.spread_points == 25.0


def test_arena_controls():
    controls = ArenaControls()
    assert len(controls.actions) == 0

    controls.pause_strategy(strategy_id=1, reason="manual pause")
    controls.resume_strategy(strategy_id=1)
    controls.remove_strategy(strategy_id=1, reason="bad edge")
    controls.stop_arena(reason="system test")

    assert len(controls.actions) == 4
    assert controls.actions[0] == {"strategy_id": 1, "action": "paused", "reason": "manual pause"}
    assert controls.actions[1] == {"strategy_id": 1, "action": "resumed", "reason": ""}
    assert controls.actions[2] == {"strategy_id": 1, "action": "removed", "reason": "bad edge"}
    assert controls.actions[3] == {
        "strategy_id": None,
        "action": "stopped",
        "reason": "system test",
    }


def test_arena_runtime_stubs():
    runtime = ArenaRuntime(root=Path("."), ledger=None)
    with pytest.raises(NotImplementedError, match="gap detection"):
        runtime.check_and_recover_gaps()


def test_leaderboard_computation():
    spec = _dummy_spec()
    s1 = ArenaStrategy(1, 10, spec, "GOLD", "H1")
    s2 = ArenaStrategy(2, 20, spec, "EURUSD", "M15")

    # Add trades: s1 has 12 trades, s2 has 5 trades (below min_trades 10)
    for i in range(12):
        s1.account_backtest.closed_trades.append(
            PaperTrade(i, "GOLD", 1, 0, 1, 100.0, 101.0, 1.0, 100.0, 1.0, 1.0, "target")
        )
    for i in range(5):
        s2.account_backtest.closed_trades.append(
            PaperTrade(i, "EURUSD", 1, 0, 1, 1.10, 1.11, 1.0, 50.0, 2.0, 1.0, "target")
        )

    board = compute_leaderboard([s1, s2], min_trades=10)
    assert len(board) == 1
    assert board[0].strategy_id == 1
    assert board[0].net_r == 12.0
    assert board[0].rank == 1


def test_demo_ready_gate_and_overrides():
    # Qualifying metrics
    passing_metrics = {
        "days_active": 30,
        "trades_count": 35,
        "profit_factor": 1.25,
        "max_drawdown": 0.04,
        "decay_flag": False,
    }
    ok, reason = check_demo_ready(passing_metrics)
    assert ok is True
    assert reason is None

    # Failing trade count
    failing_metrics = {**passing_metrics, "trades_count": 25}
    ok, reason = check_demo_ready(failing_metrics)
    assert ok is False
    assert "Trade count 25 < 30" in str(reason)

    # Failing drawdown
    dd_metrics = {**passing_metrics, "max_drawdown": 0.08}
    ok, reason = check_demo_ready(dd_metrics)
    assert ok is False
    assert "Drawdown" in str(reason)

    # Owner override record
    rec = record_override(
        ledger=None, strategy_id=1, gate_type="demo_ready", reason="solid forward behavior"
    )
    assert rec["strategy_id"] == 1
    assert rec["reason"] == "solid forward behavior"

    with pytest.raises(ValueError, match="non-empty reason"):
        record_override(ledger=None, strategy_id=1, gate_type="demo_ready", reason="   ")


def test_live_gate_thresholds():
    assert LIVE_GATE_MIN_TRADES["M15"] == 100
    assert LIVE_GATE_MIN_TRADES["H1"] == 60
    assert LIVE_GATE_MIN_TRADES["H4"] == 40
    assert LIVE_GATE_MIN_TRADES["D1"] == 30

    # Test M15 passing
    metrics = {
        "trades_count": 105,
        "pf_lower_bound": 1.05,
        "max_drawdown": 0.05,
    }
    passed, checks = evaluate_live_gate(metrics, "M15")
    assert passed is True
    assert checks["trades_count"]["passed"] is True

    # Test H1 failing sample size
    metrics_h1 = {
        "trades_count": 45,
        "pf_lower_bound": 1.05,
        "max_drawdown": 0.05,
    }
    passed, checks = evaluate_live_gate(metrics_h1, "H1")
    assert passed is False
    assert checks["trades_count"]["passed"] is False


def test_fills_report_divergence():
    spec = _dummy_spec()
    strat = ArenaStrategy(1, 10, spec, "GOLD", "H1")
    t_bt = PaperTrade(1, "GOLD", 1, 1000, 2000, 2000.0, 2020.0, 1.0, 500.0, 2.0, 20.0, "target")
    t_lv = PaperTrade(1, "GOLD", 1, 1000, 2000, 2000.0, 2018.0, 1.0, 450.0, 1.8, 25.0, "target")
    strat.account_backtest.closed_trades.append(t_bt)
    strat.account_live.closed_trades.append(t_lv)

    report = generate_fills_report([strat])
    assert len(report) == 1
    assert report[0].strategy_id == 1
    assert report[0].trade_id == 1
    assert report[0].backtest_r == 2.0
    assert report[0].live_spread_r == 1.8
    assert report[0].r_divergence == 0.2


# --- Night Shift Tests ---


def test_night_shift_config_and_stubs():
    cfg = NightShiftConfig()
    assert cfg.max_trials == 20
    assert cfg.token_share == 0.50
    assert cfg.morning_brief_time == "07:00"

    state = NightShiftState(
        trials_run=15,
        candidates_found=2,
        tokens_used=42_000,
        stopped_reason="hit_trial_cap_20",
    )
    brief_text = format_morning_brief(state)
    assert "Trials run: 15" in brief_text
    assert "CANDIDATEs found: 2" in brief_text
    assert "Tokens used: 42000" in brief_text

    notice = compile_morning_brief(state)
    assert notice.kind == NoticeKind.DAILY_DIGEST
    assert notice.data["trials_run"] == 15

    with pytest.raises(NotImplementedError, match="run_night_shift"):
        run_night_shift(desk=None, budget=None)


# --- Regime Desk Tests ---


def test_regime_reasons_and_stubs():
    assert RegimeReason.HIGH_VOLATILITY == "HIGH_VOLATILITY"
    assert RegimeReason.NORMAL == "NORMAL"

    dec = RegimeDecision(
        strategy_id=1,
        active=True,
        reason=RegimeReason.NORMAL,
        evaluated_at="2026-10-30T00:05:00Z",
    )
    assert dec.active is True

    with pytest.raises(NotImplementedError, match="compute_daily_features"):
        compute_daily_features(bars=None, symbol="GOLD")

    with pytest.raises(NotImplementedError, match="evaluate_regimes"):
        evaluate_regimes(features=[], strategies=[], llm=None)


# --- Custom Blocks Tests ---


def test_custom_block_registry_and_approval():
    registry = CustomBlockRegistry()
    assert len(registry.list_blocks()) == 0

    valid_code = "def custom_indicator(bars, p=10):\n    return bars['close'] * 2\n"
    meta = registry.register(name="double_close", kind="filter", code=valid_code)
    assert meta.name == "double_close"
    assert meta.approved is False
    assert meta.approved_at is None
    assert len(meta.sha256) == 64

    # Duplicate registration raises
    with pytest.raises(ValueError, match="already registered"):
        registry.register(name="double_close", kind="filter", code=valid_code)

    # Approve block
    approved_meta = registry.approve("double_close", approver="owner")
    assert approved_meta.approved is True
    assert approved_meta.approved_at is not None
    assert registry.get("double_close").approved is True


def test_custom_block_code_validator():
    # Clean code passes
    clean_code = "def my_entry(bars, period=14):\n    return bars['high'] > period\n"
    ok, err = validate_custom_block_code(clean_code)
    assert ok is True
    assert err is None

    # Syntax error fails
    syntax_err = "def bad(: pass"
    ok, err = validate_custom_block_code(syntax_err)
    assert ok is False
    assert "Syntax error" in str(err)

    # Forbidden imports fail
    forbidden_import = "import os\ndef hack():\n    return os.listdir('.')"
    ok, err = validate_custom_block_code(forbidden_import)
    assert ok is False
    assert "Forbidden import: os" in str(err)

    # Forbidden call fails
    forbidden_call = "def hack():\n    eval('2 + 2')"
    ok, err = validate_custom_block_code(forbidden_call)
    assert ok is False
    assert "Forbidden call: eval" in str(err)


# --- Settings Tests for Night Shift ---


def test_nightshift_settings(tmp_path: Path):
    s = load(root=tmp_path, env={})
    assert s.nightshift_trial_cap == 20
    assert s.nightshift_token_share == 0.50

    pub = s.public()
    assert pub["nightshift_trial_cap"] == 20
    assert pub["nightshift_token_share"] == 0.50

    # Test environment variable override
    s2 = load(
        root=tmp_path,
        env={
            "VIBEDESK_NIGHTSHIFT_TRIAL_CAP": "30",
            "VIBEDESK_NIGHTSHIFT_TOKEN_SHARE": "0.40",
        },
    )
    assert s2.nightshift_trial_cap == 30
    assert s2.nightshift_token_share == 0.40


# --- API Endpoint Table Tests ---


def test_api_spec_includes_layer_bc_endpoints():
    paths = {ep["path"]: ep for ep in LAYER_BC_ENDPOINT_TABLE}
    assert "/api/arena/strategies" in paths
    assert "/api/arena/leaderboard" in paths
    assert "/api/arena/fills/report" in paths
    assert "/api/arena/uptime" in paths
    assert "/api/nightshift/status" in paths
    assert "/api/nightshift/run" in paths
    assert "/api/regime/status" in paths
    assert "/api/blocks/custom" in paths

    # Check owner-only flags
    assert paths["/api/arena/strategies/{id}/pause"]["owner_only"] is True
    assert paths["/api/arena/strategies/{id}/resume"]["owner_only"] is True
    assert paths["/api/arena/strategies/{id}/override"]["owner_only"] is True
    assert paths["/api/arena/stop"]["owner_only"] is True
    assert paths["/api/nightshift/run"]["owner_only"] is True
    assert paths["/api/blocks/custom/{name}/approve"]["owner_only"] is True
