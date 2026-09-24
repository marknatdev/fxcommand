import { expect, test } from "@playwright/test";
import { advance, api, createSession, stopAllSessions, watchErrors } from "./helpers";

test.describe.configure({ mode: "serial" });

let session: any;

test.beforeAll(async ({ request }) => {
  await stopAllSessions(request);
  await api(request, "PUT", "/risk/limits", { max_positions_global: 20, daily_loss_pct_global: 90 });
  await api(request, "PUT", "/settings", { learning_enabled: true, learning_candidates: 40, learning_bars: 3000 });
  session = await createSession(request, "Learner", ["EURUSD"], { daily_loss_pct: 90 });
  await api(request, "POST", `/sessions/${session.id}/start`); // first start queues an Optimizer Run
});

test.afterAll(async ({ request }) => {
  await stopAllSessions(request);
});

const arenaCard = (page: any) => page.getByTestId("arena-EURUSD-M1");

test("learning page shows the Arena, its Champion and the first Optimizer Run", async ({ page, request }) => {
  const errors = watchErrors(page);
  await advance(request, 40);
  await page.goto("/learning");
  await expect(page.getByTestId("page-title")).toHaveText("Learning");
  const card = arenaCard(page);
  await expect(card).toContainText("Learner");
  await expect(card.getByTestId("champion-row")).toContainText("EMA Cross");
  await expect(card.getByTestId("last-run")).toContainText(/done|insufficient/, { timeout: 60_000 });
  await expect(card.getByTestId("filter-status")).toContainText(/observ|no edge|active|disabled/);
  await expect.poll(async () => (await card.getByTestId("champion-row").locator("td").nth(1).textContent())?.trim()).not.toBe("0");
  expect(errors).toEqual([]);
});

test("Optimize and Learn now queue runs", async ({ page }) => {
  await page.goto("/learning");
  await arenaCard(page).getByTestId("optimize").click();
  await expect(page.getByText(/Optimizer Run #\d+ queued for EURUSD M1/)).toBeVisible();
  await page.getByTestId("learn-now").click();
  await expect(page.getByText(/Queued \d+ Optimizer Run/)).toBeVisible();
});

test("details show shadow curves, versions, runs and last-run finalists", async ({ page, request }) => {
  await advance(request, 40);
  await page.goto("/learning");
  const card = arenaCard(page);
  await card.getByTestId("arena-details-toggle").click();
  await expect(card.getByTestId("version-history")).toContainText("initial");
  await expect(card.getByTestId("runs-table")).toContainText(/done|insufficient|running|queued/);
  await expect(card.getByTestId("shadow-curves")).toBeVisible();
});

test("manual promotion needs an explicit override when Guardrails fail, applies when flat, and can be rolled back", async ({ page, request }) => {
  const errors = watchErrors(page);
  await api(request, "POST", "/sim/learning/challenger", {
    symbol: "EURUSD",
    timeframe: "M1",
    strategy: "donchian_breakout",
    params: { period: 8, exit_period: 4, atr_period: 5, sl_atr: 3, tp_atr: 6 },
  });
  await page.goto("/learning");
  const card = arenaCard(page);
  const row = card.getByTestId("challenger-row").filter({ hasText: "Donchian Breakout" });
  await expect(row).toBeVisible();
  await expect(row.getByTestId("guardrails")).toContainText("shadow trades");

  await row.getByTestId("promote").click();
  const dlg = page.getByTestId("promote-dialog");
  await expect(dlg).toContainText("Shadow Trades");
  await expect(dlg.getByTestId("confirm-button")).toBeDisabled(); // Guardrails failed: override required
  await dlg.getByTestId("promote-override").click();
  await expect(dlg.getByTestId("confirm-button")).toBeEnabled();
  await dlg.getByTestId("confirm-button").click();
  await expect(card.getByTestId("pending-change")).toContainText("promotion");

  for (let i = 0; i < 60; i++) {
    const s = await api<any>(request, "GET", `/sessions/${session.id}`);
    if (s.assignments[0].strategy === "donchian_breakout") break;
    await advance(request, 2);
  }
  const s = await api<any>(request, "GET", `/sessions/${session.id}`);
  expect(s.assignments[0].strategy).toBe("donchian_breakout");
  await page.reload();
  await expect(arenaCard(page).getByTestId("champion-row")).toContainText("Donchian Breakout");
  await expect(arenaCard(page).getByTestId("pending-change")).toHaveCount(0);
  // the old champion keeps shadow-trading as a Challenger
  await expect(arenaCard(page).getByTestId("challenger-row").filter({ hasText: "previous champion" })).toBeVisible();

  await arenaCard(page).getByTestId("rollback").click();
  await page.getByTestId("rollback-dialog").getByTestId("confirm-button").click();
  await expect(arenaCard(page).getByTestId("pending-change")).toContainText("rollback");
  for (let i = 0; i < 60; i++) {
    const cur = await api<any>(request, "GET", `/sessions/${session.id}`);
    if (cur.assignments[0].strategy === "ema_cross") break;
    await advance(request, 2);
  }
  await page.reload();
  await expect(arenaCard(page).getByTestId("champion-row")).toContainText("EMA Cross");
  await arenaCard(page).getByTestId("arena-details-toggle").click();
  const history = arenaCard(page).getByTestId("version-history");
  await expect(history).toContainText("promotion");
  await expect(history).toContainText("rollback");
  // the Journal recorded both, as alerts
  const alerts = await api<any[]>(request, "GET", "/journal?alerts=true&limit=50");
  expect(alerts.some((j) => j.message.startsWith("Promoted EURUSD M1"))).toBeTruthy();
  expect(alerts.some((j) => j.message.startsWith("Rolled back EURUSD M1"))).toBeTruthy();
  expect(errors).toEqual([]);
});

test("auto-promote can be enabled on a simulated (demo) account and persists", async ({ page }) => {
  await page.goto("/learning");
  const toggle = arenaCard(page).getByTestId("auto-promote");
  await expect(toggle).toBeEnabled();
  await toggle.click();
  await expect(toggle).toHaveAttribute("data-state", "checked");
  await page.reload();
  await expect(arenaCard(page).getByTestId("auto-promote")).toHaveAttribute("data-state", "checked");
});

test("session detail has a Learning tab with the same Arena", async ({ page }) => {
  await page.goto(`/sessions/${session.id}`);
  await page.getByTestId("tab-learning").click();
  await expect(page.getByTestId("session-learning").getByTestId("arena-EURUSD-M1")).toBeVisible();
});

test("learning settings persist", async ({ page }) => {
  await page.goto("/settings");
  await page.getByTestId("setting-learning-candidates").fill("60");
  await page.getByTestId("save-settings").click();
  await expect(page.getByText("Settings saved")).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("setting-learning-candidates")).toHaveValue("60");
});
