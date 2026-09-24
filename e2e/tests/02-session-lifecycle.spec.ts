import { expect, test } from "@playwright/test";
import { advance, api, createSession, stopAllSessions, watchErrors } from "./helpers";

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ request }) => {
  // generous limits so the fast test strategy is not auto-stopped mid-test
  await api(request, "PUT", "/risk/limits", { max_positions_global: 20, daily_loss_pct_global: 50 });
});

test.afterAll(async ({ request }) => {
  await stopAllSessions(request);
});

test("create a multi-symbol session through the editor", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/sessions");
  await page.getByTestId("new-session").click();
  await expect(page.getByTestId("page-title")).toHaveText("New session");

  await page.getByTestId("session-name").fill("E2E Majors");
  await page.getByTestId("session-daily-loss").fill("50");

  const rows = page.getByTestId("assignment-row");
  // first assignment: EURUSD M1 EMA cross with fast params
  await rows.nth(0).getByTestId("assignment-symbol").fill("EURUSD");
  await rows.nth(0).getByTestId("assignment-timeframe").selectOption("M1");
  await rows.nth(0).getByTestId("assignment-strategy").selectOption("ema_cross");
  await rows.nth(0).getByTestId("param-fast").fill("2");
  await rows.nth(0).getByTestId("param-slow").fill("3");
  await rows.nth(0).getByTestId("param-atr_period").fill("5");
  await rows.nth(0).getByTestId("param-sl_atr").fill("3");

  // second: GBPUSD, and the editor must not offer EURUSD twice
  await page.getByTestId("add-assignment").click();
  await expect(rows).toHaveCount(2);
  await rows.nth(1).getByTestId("assignment-symbol").fill("EURUSD");
  await expect(rows.nth(1)).toContainText("Already in this session");
  await rows.nth(1).getByTestId("assignment-symbol").fill("NOTASYMBOL");
  await expect(rows.nth(1)).toContainText("Not offered by the broker");
  await rows.nth(1).getByTestId("assignment-symbol").fill("GBPUSD");
  await rows.nth(1).getByTestId("assignment-timeframe").selectOption("M1");
  await rows.nth(1).getByTestId("assignment-strategy").selectOption("donchian_breakout");
  // a newly added assignment opens with its parameters expanded
  await rows.nth(1).getByTestId("param-period").fill("5");

  await page.getByTestId("save-session").click();
  await expect(page).toHaveURL(/\/sessions\/\d+$/);
  await expect(page.getByTestId("page-title")).toHaveText("E2E Majors");
  await expect(page.getByTestId("session-status").first()).toHaveText(/stopped/i);
  await expect(page.getByTestId("assignment-state-row")).toHaveCount(2);
  expect(errors).toEqual([]);
});

test("start, trade, pause, resume and stop (closing positions) from the dashboard", async ({ page, request }) => {
  const errors = watchErrors(page);
  const sessions = await api<any[]>(request, "GET", "/sessions");
  const s = sessions.find((x) => x.name === "E2E Majors");
  await page.goto(`/sessions/${s.id}`);

  await page.getByTestId("start-session").click();
  await expect(page.getByTestId("session-status").first()).toHaveText(/running/i);

  // move the simulated market; the engine evaluates every closed bar
  await advance(request, 90);
  await page.getByTestId("tab-trades").click();
  await expect(page.getByTestId("trade-row").first()).toBeVisible();
  const trades = await page.getByTestId("trade-row").count();
  expect(trades).toBeGreaterThan(0);

  // journal shows the reasoning trail
  await page.getByTestId("tab-journal").click();
  await expect(page.getByTestId("session-journal")).toContainText("signal");
  await expect(page.getByTestId("session-journal")).toContainText("Opened");

  // engine state per assignment
  await expect(page.getByTestId("assignment-state-row").first()).toContainText(/evaluated M1 bar/);

  // candle chart with the session's trades
  await expect(page.getByTestId("candle-chart").locator("canvas").first()).toBeVisible();

  // pause: no new orders while paused
  await page.getByTestId("pause-session").click();
  await expect(page.getByTestId("session-status").first()).toHaveText(/paused/i);
  const before = (await api<any>(request, "GET", `/trades?session_id=${s.id}`)).trades.length;
  await advance(request, 40);
  const after = (await api<any>(request, "GET", `/trades?session_id=${s.id}`)).trades.length;
  expect(after).toBe(before);

  await page.getByTestId("resume-session").click();
  await expect(page.getByTestId("session-status").first()).toHaveText(/running/i);

  // make sure something is open, then stop with "close positions"
  for (let i = 0; i < 40; i++) {
    const pos = (await api<any[]>(request, "GET", "/positions")).filter((p) => p.magic === s.magic);
    if (pos.length) break;
    await advance(request, 2);
  }
  await page.getByTestId("stop-session").first().click();
  const dialog = page.getByTestId("stop-dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByTestId("stop-close-positions").click();
  await dialog.getByTestId("confirm-button").click();
  await expect(page.getByTestId("session-status").first()).toHaveText(/stopped/i);
  const left = (await api<any[]>(request, "GET", "/positions")).filter((p) => p.magic === s.magic);
  expect(left).toEqual([]);
  expect(errors).toEqual([]);
});

test("the same symbol cannot run in two sessions at once", async ({ page, request }) => {
  const a = await createSession(request, "Conflict A", ["USDJPY"]);
  const b = await createSession(request, "Conflict B", ["USDJPY"]);
  await api(request, "POST", `/sessions/${a.id}/start`);
  await page.goto(`/sessions/${b.id}`);
  await page.getByTestId("start-session").click();
  await expect(page.getByText(/USDJPY already traded by running session 'Conflict A'/)).toBeVisible();
  await expect(page.getByTestId("session-status").first()).toHaveText(/stopped/i);
  await api(request, "POST", `/sessions/${a.id}/stop`, { close_positions: true });
});

test("a duplicate symbol inside one session is rejected by the API", async ({ request }) => {
  const res = await request.post("/api/sessions", {
    data: { name: "Dup", assignments: [{ symbol: "GOLD" }, { symbol: "GOLD", timeframe: "H1" }] },
  });
  expect(res.status()).toBe(422);
  expect((await res.json()).code).toBe("duplicate_symbol");
});

test("stop with 'leave positions' keeps them open and editing is allowed only when stopped", async ({ page, request }) => {
  const s = await createSession(request, "Leaver", ["GOLD"], { daily_loss_pct: 50 });
  await api(request, "POST", `/sessions/${s.id}/start`);
  for (let i = 0; i < 60; i++) {
    const pos = (await api<any[]>(request, "GET", "/positions")).filter((p) => p.magic === s.magic);
    if (pos.length) break;
    await advance(request, 2);
  }
  await page.goto(`/sessions/${s.id}`);
  await expect(page.getByTestId("edit-session")).toBeDisabled();
  await page.getByTestId("stop-session").first().click();
  await page.getByTestId("stop-dialog").getByTestId("confirm-button").click();
  await expect(page.getByTestId("session-status").first()).toHaveText(/stopped/i);
  const left = (await api<any[]>(request, "GET", "/positions")).filter((p) => p.magic === s.magic);
  expect(left.length).toBe(1);

  // positions page shows it as owned by the session, closable manually
  await page.goto("/positions");
  const row = page.getByTestId("position-row").filter({ hasText: "Leaver" });
  await expect(row).toBeVisible();
  await row.getByTestId("close-position").click();
  await page.getByTestId("confirm-button").click();
  await expect(page.getByTestId("position-row").filter({ hasText: "Leaver" })).toHaveCount(0);

  // editing a stopped session
  await page.goto(`/sessions/${s.id}`);
  await page.getByTestId("edit-session").click();
  await expect(page.getByTestId("page-title")).toHaveText("Edit session");
  await page.getByTestId("session-max-positions").fill("3");
  await page.getByTestId("save-session").click();
  await expect(page).toHaveURL(new RegExp(`/sessions/${s.id}$`));
  expect((await api<any>(request, "GET", `/sessions/${s.id}`)).max_positions).toBe(3);
});
