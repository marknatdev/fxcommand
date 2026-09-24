import { expect, test } from "@playwright/test";
import { api, createSession, shock, stopAllSessions, untilPosition, watchErrors } from "./helpers";

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ request }) => {
  await stopAllSessions(request);
  await api(request, "PUT", "/risk/limits", { max_positions_global: 20, daily_loss_pct_global: 50 });
});

test("session daily-loss limit auto-stops the session and raises an alert", async ({ page, request }) => {
  const errors = watchErrors(page);
  const s = await createSession(request, "Loss Limit", ["EURUSD"], { daily_loss_pct: 2 });
  await api(request, "POST", `/sessions/${s.id}/start`);
  const [p] = await untilPosition(request, s.magic);
  // gap the market through the stop-loss: the fill at the gap costs far more than the 1% risk
  await shock(request, p.symbol, p.side === "long" ? -0.004 : 0.004);

  await page.goto(`/sessions/${s.id}`);
  await expect(page.getByTestId("session-status").first()).toHaveText(/stopped/i);
  await expect(page.getByTestId("stop-reason")).toContainText("AUTO-STOP");
  await page.goto("/");
  await expect(page.getByTestId("alerts-card")).toContainText("AUTO-STOP");
  const left = (await api<any[]>(request, "GET", "/positions")).filter((x) => x.magic === s.magic);
  expect(left).toEqual([]); // close_on_auto_stop defaults to on
  expect(errors).toEqual([]);
});

test("risk rejections are journaled with the reason", async ({ page, request }) => {
  const prof = await api(request, "POST", "/risk/profiles", { name: "Tight spread", risk_pct: 1, max_spread_points: 1 });
  const s = await api(request, "POST", "/sessions", {
    name: "Rejected",
    daily_loss_pct: 50,
    assignments: [{ symbol: "EURUSD", timeframe: "M1", strategy: "ema_cross", params: { fast: 2, slow: 3, atr_period: 5 }, risk_profile_id: prof.id }],
  });
  await api(request, "POST", `/sessions/${s.id}/start`);
  await api(request, "POST", "/sim/advance", { bars: 40 });
  await page.goto("/history");
  await page.getByTestId("tab-journal").click();
  await page.getByTestId("journal-session").selectOption(String(s.id));
  await page.getByTestId("kind-risk_reject").click();
  await expect(page.getByTestId("journal-entry").first()).toContainText("spread");
  expect((await api<any[]>(request, "GET", "/positions")).filter((x) => x.magic === s.magic)).toEqual([]);
  await api(request, "POST", `/sessions/${s.id}/stop`, {});
});

test("risk profiles can be created and edited from the Risk page", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/risk");
  await page.getByTestId("new-profile").click();
  const dlg = page.getByTestId("profile-dialog");
  await dlg.getByTestId("profile-name").fill("E2E Careful");
  await dlg.getByTestId("profile-risk").fill("0.25");
  await dlg.getByTestId("save-profile").click();
  await expect(page.getByTestId("profile-row").filter({ hasText: "E2E Careful" })).toContainText("0.25%");

  // duplicate name -> error shown in the dialog
  await page.getByTestId("new-profile").click();
  await dlg.getByTestId("profile-name").fill("E2E Careful");
  await dlg.getByTestId("save-profile").click();
  await expect(dlg).toContainText("already exists");
  await page.keyboard.press("Escape");
  expect(errors).toEqual([]);
});

test("global limits persist", async ({ page }) => {
  await page.goto("/risk");
  await page.getByTestId("limit-max-positions").fill("9");
  await page.getByTestId("limit-daily-loss").fill("4.5");
  await page.getByTestId("save-limits").click();
  await expect(page.getByText("Global limits saved")).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("limit-max-positions")).toHaveValue("9");
  await expect(page.getByTestId("limit-daily-loss")).toHaveValue("4.5");
});

test("global daily-loss limit stops every running session", async ({ page, request }) => {
  await stopAllSessions(request);
  // today's realised losses so far already exceed 0.1% of start equity
  await api(request, "PUT", "/risk/limits", { max_positions_global: 20, daily_loss_pct_global: 0.1 });
  const s = await createSession(request, "Global Guard", ["GOLD"], { daily_loss_pct: 50 });
  await api(request, "POST", `/sessions/${s.id}/start`);
  await api(request, "POST", "/sim/advance", { bars: 1 });
  await page.goto(`/sessions/${s.id}`);
  await expect(page.getByTestId("stop-reason")).toContainText("global daily loss");
  await api(request, "PUT", "/risk/limits", { max_positions_global: 20, daily_loss_pct_global: 50 });
});
