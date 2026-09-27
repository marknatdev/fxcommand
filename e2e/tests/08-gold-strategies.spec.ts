import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { FAST, api, stopAllSessions, watchErrors } from "./helpers";

// Better strategies (spec v5): two GOLD Assignments in one Session, Evidence badges, the Cost Check's
// block and override, the Paper Account, statistics split, the scorecard and the Reviews page.
test.describe.configure({ mode: "serial" });

test.afterEach(async ({ request }) => {
  await stopAllSessions(request);
  await api(request, "PUT", "/settings", { cost_check_max_r: 10 }); // the global setup's limit for the FAST M1 strategy
});

async function fillAssignment(page: Page, i: number, symbol: string, timeframe: string, strategy: string) {
  const row = page.getByTestId("assignment-row").nth(i);
  await row.getByTestId("assignment-symbol").fill(symbol);
  await row.getByTestId("assignment-timeframe").selectOption(timeframe);
  await row.getByTestId("assignment-strategy").selectOption(strategy);
  return row;
}

const evidenceDone = async (request: APIRequestContext) =>
  expect.poll(async () => (await api<any[]>(request, "GET", "/evidence?symbol=GOLD")).every((e) => e.status === "done"), { timeout: 60_000 }).toBe(true);

test("two GOLD strategies in one Session, with Evidence and Cost Check per Assignment", async ({ page, request }) => {
  const errors = watchErrors(page);
  await page.goto("/sessions/new");
  await page.getByTestId("session-name").fill("E2E GOLD pair");
  await page.getByTestId("session-execution").selectOption("paper");
  const trend = await fillAssignment(page, 0, "GOLD", "H4", "trend_breakout");
  await page.getByTestId("add-assignment").click();
  const reopen = await fillAssignment(page, 1, "GOLD", "H1", "session_drift");
  await expect(trend.getByTestId("evidence-badge")).toHaveText("no evidence");
  await expect(trend.getByTestId("assignment-cost")).toHaveAttribute("data-blocked", "false");
  await expect(reopen.getByTestId("assignment-cost")).toHaveAttribute("data-blocked", "false");

  // Weekend Close on H4 warns, with the Evidence both ways
  await page.getByTestId("session-weekend-close").click();
  await expect(page.getByTestId("weekend-warning")).toBeVisible();
  await expect(trend.getByTestId("weekend-evidence")).toBeVisible();
  await page.getByTestId("session-weekend-close").click();
  await expect(page.getByTestId("weekend-warning")).toHaveCount(0);

  await trend.getByTestId("run-evidence").click();
  await evidenceDone(request);
  await expect(trend.getByTestId("evidence-badge")).toContainText("trades", { timeout: 20_000 });
  await expect(reopen.getByTestId("evidence-badge")).toHaveText("no evidence");

  await page.getByTestId("save-session").click();
  await expect(page).toHaveURL(/\/sessions\/\d+$/);
  const id = Number(page.url().split("/").pop());
  const s = await api<any>(request, "GET", `/sessions/${id}`);
  expect(s.assignments.map((a: any) => `${a.symbol} ${a.timeframe}`)).toEqual(["GOLD H4", "GOLD H1"]);
  expect(new Set(s.magics).size).toBe(2); // one Magic Number per Assignment

  await page.goto("/strategies");
  await expect(page.getByTestId("evidence-row").first()).toContainText("GOLD H4");
  await expect(page.getByTestId("evidence-status").first()).toHaveText("done");
  expect(errors).toEqual([]);
});

test("the Cost Check blocks a start until the Assignment is overridden", async ({ page, request }) => {
  const errors = watchErrors(page);
  await api(request, "PUT", "/settings", { cost_check_max_r: 0.15 });
  const s = await api<any>(request, "POST", "/sessions", {
    name: "E2E Costly",
    daily_loss_pct: 50,
    assignments: [{ symbol: "GOLD", timeframe: "M1", strategy: "ema_cross", params: FAST }],
  });
  await page.goto(`/sessions/${s.id}`);
  await page.getByTestId("start-session").first().click();
  const dialog = page.getByTestId("start-dialog");
  await expect(dialog.getByTestId("start-cost-blocked")).toContainText("GOLD M1");
  await expect(dialog.getByTestId("confirm-button")).toBeDisabled();
  await expect(dialog.getByTestId("confirm-button")).toHaveText("Blocked by the Cost Check");
  await page.keyboard.press("Escape");

  await page.goto(`/sessions/${s.id}/edit`);
  const row = page.getByTestId("assignment-row").first();
  await expect(row.getByTestId("assignment-cost")).toHaveAttribute("data-blocked", "true");
  await row.getByTestId("cost-override").click();
  await row.getByTestId("cost-override-reason").fill("demo scalping test");
  await page.getByTestId("save-session").click();
  await expect(page).toHaveURL(new RegExp(`/sessions/${s.id}$`));
  expect((await api<any>(request, "GET", `/sessions/${s.id}`)).cost_overrides[0].reason).toBe("demo scalping test");

  await page.getByTestId("start-session").first().click();
  await expect(dialog.getByTestId("start-cost-check")).toContainText("overridden");
  await expect(dialog.getByTestId("confirm-button")).toBeEnabled({ timeout: 15_000 });
  await dialog.getByTestId("confirm-button").click();
  await expect(page.getByTestId("session-status").first()).toHaveText(/running/i);
  expect(errors).toEqual([]);
});

test("the Paper Account, the scorecard and statistics kept apart from the real account", async ({ page, request }) => {
  const errors = watchErrors(page);
  await page.goto("/account");
  const card = page.getByTestId("paper-account-card");
  await card.getByTestId("paper-edit").click();
  await page.getByTestId("paper-balance-input").fill("7500");
  await page.getByTestId("paper-edit-save").click();
  await expect(card).toContainText("7,500.00"); // the start balance (equity adds earlier specs' Paper P&L)

  await card.getByTestId("paper-reset").click();
  const reset = page.getByTestId("paper-reset-dialog");
  await expect(reset.getByTestId("paper-reset-confirm")).toBeDisabled();
  await page.getByTestId("paper-reset-input").fill("RESET PAPER");
  await reset.getByTestId("paper-reset-confirm").click();
  await expect(card).toContainText("epoch 1");
  await expect(card.getByTestId("paper-equity")).toContainText("7,500.00"); // a new epoch starts flat

  const gold = page.getByTestId("scorecard-row").filter({ hasText: "GOLD H4" });
  await expect(gold).toBeVisible();
  await expect(gold.getByTestId("scorecard-verdict")).toHaveText(/too few trades|no evidence/);

  await page.goto("/");
  await expect(page.getByTestId("kpi-paper-equity")).toContainText("7,500.00");
  await expect(page.getByTestId("kpi-real-win")).toBeVisible();
  await page.goto("/history");
  await expect(page.getByTestId("history-account")).toHaveText("Real account");
  await page.getByTestId("filter-account").selectOption("paper");
  await expect(page.getByTestId("history-account")).toContainText("Paper Account");
  await page.goto("/strategies");
  await expect(page.getByTestId("strategy-trend_breakout").getByTestId("stats-paper")).toBeVisible();
  expect(errors).toEqual([]);
});

test("the Reviews page shows reports, the ledger and holdout results", async ({ page, request }) => {
  const errors = watchErrors(page);
  const snap = await api<any>(request, "GET", "/research/snapshot?symbol=GOLD&timeframe=H4&tail=1");
  await api(request, "POST", "/research/trials", {
    symbol: "GOLD", timeframe: "H4", hypothesis: "Turtle 40/20", strategy: "trend_breakout", params: { entry: 40 },
    data_from: snap.first_ts, data_to: snap.bars.time[0], result: { mean_r: 0.05 },
  });
  await api(request, "POST", "/reviews", {
    title: "E2E week", summary: "One hypothesis, no finalist.", report: "## Mistakes\n- none", arenas: ["GOLD H4"],
    actions: [{ label: "PR #1", url: "https://github.com/example/repo/pull/1" }],
  });
  await page.goto("/reviews");
  await expect(page.getByTestId("ledger-row").filter({ hasText: "GOLD H4" })).toContainText("1");
  const item = page.getByTestId("review-item").filter({ hasText: "E2E week" });
  await item.getByTestId("review-toggle").click();
  await expect(item.getByTestId("review-report")).toContainText("Mistakes");
  await expect(item.getByRole("link", { name: "PR #1" })).toHaveAttribute("href", "https://github.com/example/repo/pull/1");
  expect(errors).toEqual([]);
});
