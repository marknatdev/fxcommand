"""Playwright E2E tests for Vibe Desk (spec §8.3, CCC-52, CCC-56).

Tests cover:
- Driving each page: Desk, Ideas, Ledger, Settings.
- Driving an Idea to a Strategy Spec and then running a Trial to get an inline Verdict Card.
- Inspecting the Verdict Card components: stitched equity curve (SVG), per-fold bars (SVG),
  baseline histogram with strategy marked (SVG), Pass Bar checks with Closest Miss,
  win rate shown not judged, and Cost Snapshot.
- Owner-only actions: approving a Holdout Request with the owner cookie, and verifying
  clear error message on HTTP 403 when the cookie is absent.
- Mobile viewport responsive layout: asserting no horizontal scroll at 375 px width.

Run with:
    uv run pytest -m e2e -n 0
"""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import uvicorn
from playwright.sync_api import Page, expect

from vibedesk.app.desk import Desk
from vibedesk.blocks.spec import expand_grid, validate
from vibedesk.gauntlet.trial import CANDIDATE
from vibedesk.market.bars import Timeframe
from vibedesk.market.costs import CostSnapshot
from vibedesk.rules import RULES_VERSION

from demo_server import RSI, create_demo_app

pytestmark = pytest.mark.e2e


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def record_candidate_trial(desk: Desk, hypothesis_id: int) -> int:
    """Helper to record a valid CANDIDATE trial directly into Ledger with full summary fields."""
    import numpy as np

    hyp = desk.ledger.hypothesis(hypothesis_id)
    cost = CostSnapshot("GOLD", 30.0, 0.01, 1.0, 0.0, 0.0, 3)
    bars = desk.market.bars(
        "GOLD",
        Timeframe.H1,
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, tzinfo=UTC),
    )
    combos = expand_grid(validate(hyp.spec_json))
    combinations = [(c, 1.5) for c in combos]

    rng = np.random.default_rng(42)
    daily_r_list = rng.normal(0.2, 0.5, 1000).tolist()
    trade_exit_times = [1700000000 + i * 3600 for i in range(len(daily_r_list))]

    result_data = {
        "verdict": CANDIDATE,
        "closest_miss": None,
        "checks": [],
        "cost": {"spread_floor_points": 30.0, "slippage_points": 1.0},
        "baseline": {"p95": 10.0, "percentile": 99.0, "strategy_net_r": 25.0},
        "walk_forward": {
            "metrics": {"profit_factor": 1.5, "trades": 120, "win_rate": 0.55},
            "trade_list": {"exit_time": trade_exit_times, "r": daily_r_list},
            "stress_metrics": {},
            "years": [2023],
            "fold_net_r": [25.0],
            "combinations": list(range(len(combinations))),
            "daily_r": {"day_start": list(range(1000)), "r": daily_r_list},
            "holdout_choice": 0,
        },
        "holdout": {"rate_per_year": 50.0},
    }
    return desk.ledger.record_trial(
        hypothesis_id,
        combinations=combinations,
        verdict=CANDIDATE,
        closest_miss=None,
        cost=cost,
        bars=bars,
        rules_version=RULES_VERSION,
        result_json=json.dumps(result_data),
    )


@pytest.fixture(scope="module")
def demo_server(tmp_path_factory):
    port = find_free_port()
    data_dir = tmp_path_factory.mktemp("data")
    scratch = tmp_path_factory.mktemp("scratch")
    app, data_dir, scratch = create_demo_app(data_dir=data_dir, scratch=scratch)

    desk = app.state.desk
    rsi_gold = {**RSI, "symbol": "GOLD", "timeframe": "H1"}
    fam_id = desk.ledger.create_family("Pre-seeded Test Family for Holdout Approval")
    hyp_id = desk.ledger.register_hypothesis(fam_id, rsi_gold)
    trial_id = record_candidate_trial(desk, hyp_id)
    preseeded_request_id = desk.request_holdout(hyp_id)

    config = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config=config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            res = httpx.get(f"{base_url}/api/status", timeout=1.0)
            if res.status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("Demo server failed to start within timeout")

    yield {
        "base_url": base_url,
        "app": app,
        "data_dir": data_dir,
        "scratch": scratch,
        "request_id": preseeded_request_id,
        "family_id": fam_id,
        "hypothesis_id": hyp_id,
        "trial_id": trial_id,
    }

    server.should_exit = True
    thread.join(timeout=3.0)


def test_drive_each_page(demo_server: dict[str, Any], page: Page):
    """Drive navigation between all 4 pages: Desk, Ideas, Ledger, Settings."""
    base_url = demo_server["base_url"]
    page.goto(base_url)

    # 1. Desk Page (default)
    expect(page.locator("#viewDesk")).to_be_visible()
    expect(page.locator("#input")).to_be_visible()

    # 2. Ideas Page
    page.click("#navIdeas")
    expect(page.locator("#viewIdeas")).to_be_visible()
    expect(page.locator("#ideasContainer")).to_be_visible()
    expect(page.locator("#newFamilyInput")).to_be_visible()

    # 3. Ledger Page
    page.click("#navLedger")
    expect(page.locator("#viewLedger")).to_be_visible()
    expect(page.locator("#statDeskTrials")).to_be_visible()
    expect(page.locator("#familiesTable")).to_be_visible()
    expect(page.locator("#holdoutRequestsTable")).to_be_visible()

    # 4. Settings Page
    page.click("#navSettings")
    expect(page.locator("#viewSettings")).to_be_visible()
    expect(page.locator("#settingsLlmUrl")).to_be_visible()
    expect(page.locator("#settingsDailyCap")).to_be_visible()
    expect(page.locator("#floorsTableBody")).to_be_visible()

    # 5. Back to Desk Page
    page.click("#navDesk")
    expect(page.locator("#viewDesk")).to_be_visible()


def test_idea_to_verdict_card(demo_server: dict[str, Any], page: Page):
    """Drive an Idea through the Research Desk to a Strategy Spec and inline Verdict Card."""
    base_url = demo_server["base_url"]
    page.goto(base_url)

    # Type an Idea into the composer and send
    idea_prompt = "Buy and sell GOLD breakouts of the 20 bar high on H1, stop 2 ATR, target 2R"
    page.fill("#input", idea_prompt)
    page.click("#send")

    # The Strategy Spec card should appear
    spec_card = page.locator(".card").first
    expect(spec_card).to_be_visible(timeout=10000)
    expect(spec_card.locator(".card-title")).to_contain_text("GOLD")

    # Run the walk-forward trial
    run_btn = spec_card.locator("button.run")
    expect(run_btn).to_be_visible()
    run_btn.click()

    # Wait for the inline Verdict Card to be rendered
    verdict_card = page.locator(".verdict-card").first
    expect(verdict_card).to_be_visible(timeout=30000)

    # Assert Verdict Card contains all required elements (spec §8.3):
    expect(verdict_card.locator(".verdict-badge")).to_be_visible()
    expect(verdict_card.locator("svg.chart-equity")).to_be_visible()
    expect(verdict_card.locator("svg.chart-folds")).to_be_visible()
    expect(verdict_card.locator("svg.chart-baseline")).to_be_visible()
    expect(verdict_card).to_contain_text("Win rate (shown only)")
    expect(verdict_card.locator("table")).to_be_visible()
    expect(verdict_card.locator(".cost-snapshot")).to_be_visible()
    expect(verdict_card.locator(".cost-snapshot")).to_contain_text("Spread floor")


def test_owner_only_approve_and_403(demo_server: dict[str, Any], page: Page):
    """Verify owner-only Holdout approval succeeds with cookie and fails with 403 without it."""
    base_url = demo_server["base_url"]
    req_id = demo_server["request_id"]

    page.goto(base_url)

    # Navigate to Ledger page
    page.click("#navLedger")
    expect(page.locator("#viewLedger")).to_be_visible()

    # The pre-seeded Holdout Request should be rendered in the table with an Approve button
    req_row = page.locator(f"#holdout-req-{req_id}")
    expect(req_row).to_be_visible(timeout=10000)
    approve_btn = req_row.locator(".btn-approve-holdout")
    expect(approve_btn).to_be_visible()
    approve_btn.click()

    # With the default owner cookie set by the page, approval succeeds
    approve_msg = page.locator("#holdoutApproveMsg")
    expect(approve_msg).to_be_visible(timeout=10000)
    expect(approve_msg).to_contain_text("Approved!")
    expect(approve_msg).to_contain_text("Grant issued")

    # Now simulate a non-owner by clearing the owner cookie
    page.context.clear_cookies()

    # Try an owner-only action: extend budget on Family #1
    page.locator("#familiesTableBody button").first.click()

    # Verify a clear message is displayed for HTTP 403
    banner = page.locator("#ledgerBanner")
    expect(banner).to_be_visible(timeout=10000)
    expected_msg = "Owner action required: owner cookie missing or invalid (HTTP 403)"
    expect(banner).to_contain_text(expected_msg)


def test_ideas_apostrophe_and_verdict_filter(demo_server: dict[str, Any], page: Page):
    """Verify Idea with apostrophe and script tag is safe and Chat Idea works;
    test filterVerdict."""
    base_url = demo_server["base_url"]
    page.goto(base_url)

    # 1. Navigate to Ideas page
    page.click("#navIdeas")
    expect(page.locator("#viewIdeas")).to_be_visible()

    # Create an Idea Family with an apostrophe and a <script> tag
    tricky_idea = "GOLD's trend pullback <script>window.__injected_xss = true;</script>"
    page.fill("#newFamilyInput", tricky_idea)
    page.click("#btnCreateFamily")

    # Verify family is created and displayed safely
    family_item = page.locator(".idea-item", has_text="GOLD's trend pullback")
    expect(family_item).to_be_visible(timeout=10000)

    # Verify no XSS was executed
    is_xss = page.evaluate("() => Boolean(window.__injected_xss)")
    assert not is_xss, "XSS script should not execute"

    # Click "Chat Idea" button on the family with tricky idea text
    chat_btn = family_item.locator(".btn-chat-family")
    expect(chat_btn).to_be_visible()
    chat_btn.click()

    # Should switch to Desk view with the idea text loaded into #input
    expect(page.locator("#viewDesk")).to_be_visible()
    input_val = page.locator("#input").input_value()
    assert input_val == tricky_idea

    # 2. Test filterVerdict
    page.click("#navIdeas")
    expect(page.locator("#viewIdeas")).to_be_visible()

    # Pre-seeded family has a CANDIDATE trial
    candidate_family = page.locator(
        ".idea-item", has_text="Pre-seeded Test Family for Holdout Approval"
    )
    expect(candidate_family).to_be_visible()
    expect(candidate_family.locator(".badge.good")).to_contain_text("CANDIDATE")

    # Select CANDIDATE in filterVerdict dropdown
    page.select_option("#filterVerdict", "CANDIDATE")
    expect(candidate_family).to_be_visible()

    # Select REJECT in filterVerdict dropdown (should hide candidate family)
    page.select_option("#filterVerdict", "REJECT")
    expect(candidate_family).not_to_be_visible()

    # Reset filterVerdict to All Verdicts
    page.select_option("#filterVerdict", "")
    expect(candidate_family).to_be_visible()


def test_responsive_375px_no_horizontal_scroll(demo_server: dict[str, Any], page: Page):
    """Assert clean responsive layout at 375 px phone width with no horizontal scroll."""
    base_url = demo_server["base_url"]

    # Set viewport to standard mobile width: 375 px (e.g. iPhone SE / mini)
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto(base_url)

    def assert_no_horizontal_scroll(context_name: str):
        scroll_w = page.evaluate("() => document.documentElement.scrollWidth")
        client_w = page.evaluate("() => document.documentElement.clientWidth")
        body_scroll_w = page.evaluate("() => document.body.scrollWidth")
        assert scroll_w <= client_w, (
            f"Horizontal overflow on {context_name}: "
            f"scrollWidth ({scroll_w}) > clientWidth ({client_w})"
        )
        assert body_scroll_w <= client_w, (
            f"Body overflow on {context_name}: "
            f"body.scrollWidth ({body_scroll_w}) > clientWidth ({client_w})"
        )

    # 1. Desk page at 375 px
    expect(page.locator("#viewDesk")).to_be_visible()
    assert_no_horizontal_scroll("Desk Page")

    # 2. Ideas page at 375 px
    page.click("button[data-view='Ideas']")
    expect(page.locator("#viewIdeas")).to_be_visible()
    assert_no_horizontal_scroll("Ideas Page")

    # 3. Ledger page at 375 px
    page.click("button[data-view='Ledger']")
    expect(page.locator("#viewLedger")).to_be_visible()
    assert_no_horizontal_scroll("Ledger Page")

    # 4. Settings page at 375 px
    page.click("button[data-view='Settings']")
    expect(page.locator("#viewSettings")).to_be_visible()
    assert_no_horizontal_scroll("Settings Page")
