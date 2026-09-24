import { expect, test } from "@playwright/test";
import { watchErrors } from "./helpers";

const PAGES: [string, string, string][] = [
  ["overview", "/", "Overview"],
  ["sessions", "/sessions", "Sessions"],
  ["symbols", "/symbols", "Symbols"],
  ["strategies", "/strategies", "Strategies"],
  ["learning", "/learning", "Learning"],
  ["risk", "/risk", "Risk"],
  ["positions", "/positions", "Positions & Orders"],
  ["history", "/history", "History & Journal"],
  ["account", "/account", "Account & Connection"],
  ["logs", "/logs", "Logs"],
  ["settings", "/settings", "Settings"],
];

test("app boots connected to the simulated broker", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/");
  await expect(page.getByTestId("page-title")).toHaveText("Overview");
  await expect(page.getByTestId("mode-badge")).toHaveText("SIM");
  await expect(page.getByTestId("connection")).toHaveText("Connected");
  await expect(page.getByTestId("server-time")).toContainText("2024-01-08");
  await expect(page.getByTestId("kpi-equity")).toContainText("10,000.00");
  expect(errors).toEqual([]);
});

test("every section has its own page reachable from the sidebar", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/");
  for (const [nav, url, title] of PAGES) {
    await page.getByTestId(`nav-${nav}`).click();
    await expect(page).toHaveURL(new RegExp(`${url === "/" ? "/$" : url}$`));
    await expect(page.getByTestId("page-title")).toHaveText(title);
  }
  expect(errors).toEqual([]);
});

test("deep links and unknown routes render (SPA fallback)", async ({ page }) => {
  for (const [, url, title] of PAGES) {
    await page.goto(url);
    await expect(page.getByTestId("page-title")).toHaveText(title);
  }
  await page.goto("/definitely/not/here");
  await expect(page.getByText("Page not found")).toBeVisible();
});

test("strategy catalog lists the three v1 strategies with parameters", async ({ page }) => {
  await page.goto("/strategies");
  for (const key of ["ema_cross", "donchian_breakout", "rsi_reversion"]) {
    const card = page.getByTestId(`strategy-${key}`);
    await expect(card).toBeVisible();
    await expect(card.getByText("Parameters (defaults)")).toBeVisible();
  }
});

test("symbols page shows live quotes and a candle chart", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/symbols");
  await expect(page.getByTestId("symbol-row")).toHaveCount(4);
  await page.getByTestId("symbol-row").filter({ hasText: "GOLD" }).click();
  await expect(page.getByTestId("symbol-chart")).toContainText("GOLD");
  await expect(page.getByTestId("candle-chart").locator("canvas").first()).toBeVisible();
  await page.getByTestId("tf-H1").click();
  await expect(page.getByTestId("candle-chart").locator("canvas").first()).toBeVisible();
  await page.getByTestId("symbol-filter").fill("jpy");
  await expect(page.getByTestId("symbol-row")).toHaveCount(1);
  expect(errors).toEqual([]);
});
