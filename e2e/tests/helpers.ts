import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const FAST = { fast: 2, slow: 3, atr_period: 5, sl_atr: 3, tp_atr: 6 };

export async function api<T = any>(request: APIRequestContext, method: "GET" | "POST" | "PUT" | "DELETE", path: string, data?: unknown): Promise<T> {
  const res = await request.fetch(`/api${path}`, { method, data });
  expect(res.ok(), `${method} ${path} -> ${res.status()} ${await res.text()}`).toBeTruthy();
  return res.status() === 204 ? (undefined as T) : res.json();
}

export async function createSession(
  request: APIRequestContext,
  name: string,
  symbols: string[],
  extra: Record<string, unknown> = {},
  params: Record<string, number | boolean> = FAST,
) {
  return api(request, "POST", "/sessions", {
    name,
    assignments: symbols.map((symbol) => ({ symbol, timeframe: "M1", strategy: "ema_cross", params })),
    ...extra,
  });
}

export const advance = (request: APIRequestContext, bars: number) => api(request, "POST", "/sim/advance", { bars });
export const shock = (request: APIRequestContext, symbol: string, pct: number) => api(request, "POST", "/sim/shock", { symbol, pct });

/** Advance bar by bar until the session holds a position (fast EMA on M1 trades within a few bars). */
export async function untilPosition(request: APIRequestContext, magic: number, maxBars = 150) {
  for (let i = 0; i < maxBars; i++) {
    await advance(request, 1);
    const positions = await api<any[]>(request, "GET", "/positions");
    const mine = positions.filter((p) => p.magic === magic);
    if (mine.length) return mine;
  }
  throw new Error(`no position for magic ${magic} after ${maxBars} bars`);
}

export async function stopAllSessions(request: APIRequestContext) {
  const sessions = await api<any[]>(request, "GET", "/sessions");
  for (const s of sessions) {
    if (s.status !== "stopped") await api(request, "POST", `/sessions/${s.id}/stop`, { close_positions: true });
  }
}

/** Collect console errors and failed requests for a page; assert empty at the end of a test. */
export function watchErrors(page: Page) {
  const errors: string[] = [];
  page.on("console", (m) => {
    // Chrome logs every 4xx as "Failed to load resource"; deliberate 4xx are asserted by the tests themselves
    if (m.type() === "error" && !/status of 4\d\d/.test(m.text())) errors.push(`console: ${m.text()}`);
  });
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("response", (r) => {
    // 4xx from deliberate negative tests are asserted separately; 5xx is always a bug
    if (r.status() >= 500) errors.push(`HTTP ${r.status()} ${r.url()}`);
  });
  return errors;
}
