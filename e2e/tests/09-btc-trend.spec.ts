import { expect, test } from "@playwright/test";
import { api, stopAllSessions, watchErrors } from "./helpers";

// BTC Trend (spec-btc-strategies): a Paper Session on BTCUSD H4 with the Trend Breakout Strategy at
// 100/50, its Trading Window turned off so it trades 7 days a week; the editor points out that a
// Monday–Friday window skips the weekends of a symbol that trades every day.
test.afterEach(async ({ request }) => {
  await stopAllSessions(request);
});

test("a BTC Trend Paper Session trades every day with the window off", async ({ page, request }) => {
  const errors = watchErrors(page);
  await page.goto("/sessions/new");
  await page.getByTestId("session-name").fill("E2E BTC Trend");
  await page.getByTestId("session-execution").selectOption("paper");
  const row = page.getByTestId("assignment-row").nth(0);
  await row.getByTestId("assignment-symbol").fill("BTCUSD");
  await row.getByTestId("assignment-timeframe").selectOption("H4");
  await row.getByTestId("assignment-strategy").selectOption("trend_breakout");
  await expect(row.getByTestId("assignment-strategy").locator("option:checked")).toContainText("Trend Breakout");
  await row.getByTestId("param-entry").fill("100");
  await row.getByTestId("param-exit").fill("50");
  await expect(row.getByTestId("assignment-cost")).toHaveAttribute("data-blocked", "false");

  // the default window runs Monday–Friday: the hint says BTCUSD trades every day
  await expect(page.getByTestId("window-every-day-hint")).toContainText("BTCUSD trades every day");
  await page.getByTestId("window-enabled").click();
  await expect(page.getByTestId("window-every-day-hint")).toHaveCount(0);

  await page.getByTestId("save-session").click();
  await expect(page).toHaveURL(/\/sessions\/\d+$/);
  const id = Number(page.url().split("/").pop());
  const s = await api<any>(request, "GET", `/sessions/${id}`);
  expect(s.window.enabled).toBe(false);
  expect(s.weekend_close).toBe(false);
  expect(s.assignments.map((a: any) => [a.symbol, a.timeframe, a.strategy, a.params.entry, a.params.exit])).toEqual([["BTCUSD", "H4", "trend_breakout", 100, 50]]);
  const cost = await api<any>(request, "GET", `/sessions/${id}/cost-check`);
  expect(cost.assignments.map((c: any) => c.swap_every_night)).toEqual([true]);
  expect(errors).toEqual([]);
});
