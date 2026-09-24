import { expect, test } from "@playwright/test";
import { api, watchErrors } from "./helpers";

test("settings persist and the simulator can be driven from the Settings page", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/settings");
  await page.getByTestId("setting-poll").fill("0.5");
  await page.getByTestId("save-settings").click();
  await expect(page.getByText("Settings saved")).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("setting-poll")).toHaveValue("0.5");

  const before = await page.getByTestId("sim-time").textContent();
  await page.getByTestId("sim-bars").fill("15");
  await page.getByTestId("sim-advance").click();
  await expect(page.getByText("Advanced 15 bars")).toBeVisible();
  await expect(page.getByTestId("sim-time")).not.toHaveText(before ?? "");
  expect(errors).toEqual([]);
});

test("live trading toggle requires typing the account number", async ({ page }) => {
  await page.goto("/account");
  await expect(page.getByTestId("account-login")).toHaveText("99000001");
  await expect(page.getByTestId("account-connected")).toContainText("connected");
  await page.getByTestId("live-enabled").click();
  const dlg = page.getByTestId("live-dialog");
  await dlg.getByTestId("live-confirm-input").fill("12345");
  await dlg.getByTestId("confirm-live").click();
  await expect(dlg).toContainText("type the account number 99000001");
  await dlg.getByTestId("live-confirm-input").fill("99000001");
  await dlg.getByTestId("confirm-live").click();
  await expect(page.getByTestId("live-enabled")).toHaveAttribute("data-state", "checked");
  // and back off
  await page.getByTestId("live-enabled").click();
  await page.getByTestId("confirm-button").click();
  await expect(page.getByTestId("live-enabled")).toHaveAttribute("data-state", "unchecked");
});

test("history shows trades with statistics and filters", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/history");
  await expect(page.getByTestId("stats-grid")).toBeVisible();
  await expect(page.getByTestId("trade-row").first()).toBeVisible();
  await page.getByTestId("filter-symbol").selectOption("GOLD");
  const rows = page.getByTestId("trade-row");
  const n = await rows.count();
  for (let i = 0; i < n; i++) await expect(rows.nth(i)).toContainText("GOLD");
  await page.getByTestId("filter-status").selectOption("closed");
  await expect(page.getByTestId("trade-row").first()).toBeVisible();
  await expect(page.getByTestId("trade-row").getByText("open", { exact: true })).toHaveCount(0);
  await page.getByTestId("tab-journal").click();
  await page.getByTestId("alerts-only").click();
  await expect(page.getByTestId("journal-entry").first()).toBeVisible();
  expect(errors).toEqual([]);
});

test("overview reflects account, sessions and the equity curve", async ({ page, request }) => {
  await api(request, "POST", "/sim/advance", { bars: 20 });
  await page.goto("/");
  await expect(page.getByTestId("kpi-equity")).not.toContainText("10,000.00"); // trading happened
  await expect(page.getByTestId("overview-session").first()).toBeVisible();
  await expect(page.getByTestId("equity-chart")).toBeVisible();
});

test("logs page streams application logs", async ({ page, request }) => {
  await page.goto("/logs");
  await expect(page.getByTestId("log-line").first()).toBeVisible();
  const n = await page.getByTestId("log-line").count();
  await api(request, "PUT", "/settings", { poll_interval: 1 }); // produces a journal/log line
  await expect.poll(async () => page.getByTestId("log-line").count()).toBeGreaterThan(n);
  await page.getByTestId("log-search").fill("Settings changed");
  await expect(page.getByTestId("log-line").first()).toContainText("Settings changed");
});

