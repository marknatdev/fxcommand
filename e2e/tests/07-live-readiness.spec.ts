import { expect, test } from "@playwright/test";
import { api, createSession, stopAllSessions, untilPosition, watchErrors } from "./helpers";

// Going live (ADR 0006/0007), driven through the dashboard against the simulated broker.
test.describe.configure({ mode: "serial" });

test.afterEach(async ({ request }) => {
  await stopAllSessions(request);
  await api(request, "POST", "/sim/account", { is_demo: true, algo_trading: true });
});

test("a Paper session trades on real prices without touching the account", async ({ page, request }) => {
  const errors = watchErrors(page);
  await page.goto("/sessions/new");
  await page.getByTestId("session-name").fill("E2E Paper");
  await page.getByTestId("session-execution").selectOption("paper");
  await page.getByTestId("session-daily-loss").fill("50");
  const row = page.getByTestId("assignment-row").nth(0);
  await row.getByTestId("assignment-symbol").fill("GBPUSD");
  await row.getByTestId("assignment-timeframe").selectOption("M1");
  await row.getByTestId("assignment-strategy").selectOption("ema_cross");
  await row.getByTestId("param-fast").fill("2");
  await row.getByTestId("param-slow").fill("3");
  await row.getByTestId("param-atr_period").fill("5");
  await row.getByTestId("param-sl_atr").fill("3");
  await page.getByTestId("save-session").click();
  await expect(page).toHaveURL(/\/sessions\/\d+$/);
  await expect(page.getByTestId("detail-paper")).toBeVisible();
  const id = Number(page.url().split("/").pop());
  const s = await api<any>(request, "GET", `/sessions/${id}`);
  expect(s.execution).toBe("paper");
  const before = await api<any>(request, "GET", "/sim/state");
  await page.getByTestId("start-session").click();
  const dialog = page.getByTestId("start-dialog");
  await expect(dialog).toContainText("PAPER — nothing is sent to the account");
  await dialog.getByTestId("confirm-button").click();
  await expect(page.getByTestId("session-status").first()).toHaveText(/running/i);
  await untilPosition(request, s.magic);
  await page.goto("/positions");
  await expect(page.getByTestId("paper-badge").first()).toBeVisible();
  const after = await api<any>(request, "GET", "/sim/state");
  expect(after.balance).toBe(before.balance); // the account never saw an order
  expect(after.open_positions).toBe(before.open_positions);
  // stopping a Paper session always closes its paper positions
  await api(request, "POST", `/sessions/${id}/stop`, { close_positions: false });
  const left = (await api<any[]>(request, "GET", "/positions")).filter((p) => p.magic === s.magic);
  expect(left).toEqual([]);
  expect(errors).toEqual([]);
});

test("an uncertain order is not retried and the resulting position is adopted", async ({ page, request }) => {
  const s = await createSession(request, "E2E Uncertain", ["EURUSD"], { daily_loss_pct: 50 });
  await api(request, "POST", `/sessions/${s.id}/start`);
  await api(request, "POST", "/sim/fault", { kind: "timeout_filled" });
  await untilPosition(request, s.magic);
  await page.goto(`/sessions/${s.id}`);
  await page.getByTestId("tab-journal").click();
  await expect(page.getByTestId("session-journal")).toContainText("UNCERTAIN");
  const pos = (await api<any[]>(request, "GET", "/positions")).filter((p) => p.magic === s.magic);
  expect(pos).toHaveLength(1);
  const trades = (await api<any>(request, "GET", `/trades?session_id=${s.id}`)).trades;
  expect(trades.find((t: any) => t.ticket === pos[0].ticket).adopted).toBe(true);
});

test("live account: start is blocked by the pre-flight check until it passes", async ({ page, request }) => {
  const errors = watchErrors(page);
  const s = await createSession(request, "E2E Live", ["USDJPY"]);
  await api(request, "POST", "/sim/account", { is_demo: false, algo_trading: false });
  const acct = (await api<any>(request, "GET", "/account")).account;
  await api(request, "PUT", "/account/live-enabled", { enabled: true, confirm: String(acct.login) });

  await page.goto(`/sessions/${s.id}`);
  await page.getByTestId("start-session").click();
  const dialog = page.getByTestId("start-dialog");
  await expect(dialog.getByTestId("preflight-status")).toHaveText("blocked");
  await expect(dialog.getByTestId("check-algo_trading")).toHaveAttribute("data-status", "fail");
  await expect(dialog.getByTestId("confirm-button")).toBeDisabled();
  await dialog.getByRole("button", { name: "Cancel" }).click();

  await api(request, "POST", "/sim/account", { algo_trading: true });
  const pf = await api<any>(request, "GET", `/preflight?session_id=${s.id}`);
  expect(pf.failed, JSON.stringify(pf.checks.filter((c: any) => c.status !== "pass"))).toEqual([]);
  await page.getByTestId("start-session").click();
  await expect(dialog.getByTestId("preflight-status")).toHaveText("ready");
  await dialog.getByTestId("confirm-button").click();
  await expect(page.getByTestId("session-status").first()).toHaveText(/running/i);

  // Risk page: Live Caps in force and an armed Equity Floor
  await page.goto("/risk");
  await expect(page.getByTestId("live-caps")).toContainText("in force");
  await expect(page.getByTestId("equity-floor")).toContainText("armed");
  await page.getByTestId("cap-volume").fill("0.05");
  await page.getByTestId("save-caps").click();
  const confirm = page.getByTestId("caps-confirm-dialog");
  await confirm.getByTestId("caps-confirm-input").fill("1");
  await confirm.getByTestId("caps-confirm").click();
  await expect(confirm).toContainText("type the account number");
  await confirm.getByTestId("caps-confirm-input").fill(String(acct.login));
  await confirm.getByTestId("caps-confirm").click();
  await expect(confirm).toBeHidden();
  expect((await api<any>(request, "GET", "/risk")).live_caps.max_volume).toBe(0.05);

  await api(request, "PUT", "/account/live-enabled", { enabled: false, confirm: "" });
  // the 422 for the wrong account number is the asserted behaviour
  expect(errors).toEqual([]);
});

test("switching the terminal to another account interrupts active sessions", async ({ page, request }) => {
  const s = await createSession(request, "E2E Pinned", ["GOLD"]);
  await api(request, "POST", `/sessions/${s.id}/start`);
  const st = await api<any>(request, "GET", "/sim/state");
  await api(request, "POST", "/sim/account", { login: st.login + 7 });
  await page.goto(`/sessions/${s.id}`);
  await expect(page.getByTestId("session-status").first()).toHaveText(/interrupted/i);
  await api(request, "POST", "/sim/account", { login: st.login });
});

test("Telegram settings keep the token write-only and show the outbox", async ({ page, request }) => {
  const errors = watchErrors(page);
  await page.goto("/settings");
  const card = page.getByTestId("notify-card");
  await expect(card.getByTestId("notify-status")).toHaveText("not configured");
  await card.getByTestId("notify-test").click();
  await expect(page.getByText(/test message not delivered: skipped: not configured/)).toBeVisible();
  await expect(card.getByTestId("outbox")).toContainText("skipped: not configured");
  await card.getByTestId("notify-token").fill("123456:E2ESECRETVALUE");
  await card.getByTestId("notify-chat").fill("42");
  await card.getByTestId("notify-save").click();
  await expect(card.getByTestId("notify-status")).toHaveText("configured");
  const settings = await (await request.get("/api/settings")).text();
  expect(settings).not.toContain("E2ESECRET");
  await api(request, "PUT", "/settings/notify", { telegram_token: "", telegram_chat_id: "" });
  expect(errors).toEqual([]);
});

test("account page shows the pre-flight check and margin mode", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/account");
  await expect(page.getByTestId("preflight-card").getByTestId("check-terminal")).toHaveAttribute("data-status", "pass");
  await expect(page.getByTestId("margin-mode")).toHaveText("hedging");
  expect(errors).toEqual([]);
});
