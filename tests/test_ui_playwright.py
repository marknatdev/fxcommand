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

import socket
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn
from playwright.sync_api import Page, expect
from test_carry_forwards import record_dummy_trial

from vibedesk.gauntlet.trial import CANDIDATE

from demo_server import RSI, create_demo_app

pytestmark = pytest.mark.e2e


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def demo_server():
    port = find_free_port()
    app, data_dir, scratch = create_demo_app()

    desk = app.state.desk
    rsi_gold = {**RSI, "symbol": "GOLD", "timeframe": "H1"}
    fam_id = desk.ledger.create_family("Pre-seeded Test Family for Holdout Approval")
    hyp_id = desk.ledger.register_hypothesis(fam_id, rsi_gold)
    trial_id = record_dummy_trial(desk, hyp_id, verdict=CANDIDATE)
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
    expect(page.locator("#holdoutRequestIdInput")).to_be_visible()

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

    # Enter the pre-seeded Holdout Request ID
    page.fill("#holdoutRequestIdInput", str(req_id))
    page.click("#btnApproveHoldout")

    # With the default owner cookie set by the page, approval succeeds
    approve_msg = page.locator("#holdoutApproveMsg")
    expect(approve_msg).to_be_visible(timeout=10000)
    expect(approve_msg).to_contain_text("Approved!")
    expect(approve_msg).to_contain_text("Grant issued")

    # Now simulate a non-owner by clearing the owner cookie
    page.context.clear_cookies()

    # Try an owner-only action: extend budget on Family #1
    page.locator("#familiesTable button").first.click()

    # Verify a clear message is displayed for HTTP 403
    banner = page.locator("#ledgerBanner")
    expect(banner).to_be_visible(timeout=10000)
    expected_msg = "Owner action required: owner cookie missing or invalid (HTTP 403)"
    expect(banner).to_contain_text(expected_msg)


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
