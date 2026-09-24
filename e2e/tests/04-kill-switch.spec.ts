import { expect, test } from "@playwright/test";
import { api, createSession, stopAllSessions, untilPosition, watchErrors } from "./helpers";

test("kill switch stops every session and closes every owned position", async ({ page, request }) => {
  const errors = watchErrors(page);
  await stopAllSessions(request);
  await api(request, "PUT", "/risk/limits", { max_positions_global: 20, daily_loss_pct_global: 50 });
  const a = await createSession(request, "Kill A", ["EURUSD", "GBPUSD"], { daily_loss_pct: 50 });
  const b = await createSession(request, "Kill B", ["USDJPY", "GOLD"], { daily_loss_pct: 50 });
  await api(request, "POST", `/sessions/${a.id}/start`);
  await api(request, "POST", `/sessions/${b.id}/start`);
  await untilPosition(request, a.magic);

  await page.goto("/");
  await expect(page.getByTestId("overview-session").filter({ hasText: "Kill A" })).toContainText(/running/i);
  await page.getByTestId("kill-switch").click();
  const dlg = page.getByTestId("kill-dialog");
  await expect(dlg).toContainText("never touched");
  await dlg.getByTestId("confirm-button").click();
  await expect(page.getByText(/Kill switch: stopped \d+ sessions/)).toBeVisible();

  for (const s of [a, b]) {
    expect((await api<any>(request, "GET", `/sessions/${s.id}`)).status).toBe("stopped");
  }
  const owned = (await api<any[]>(request, "GET", "/positions")).filter((p) => p.owned);
  expect(owned).toEqual([]);
  await expect(page.getByTestId("alerts-card")).toContainText("KILL SWITCH");
  await expect(page.getByTestId("overview-session").filter({ hasText: "Kill A" })).toContainText(/stopped/i);
  expect(errors).toEqual([]);
});
