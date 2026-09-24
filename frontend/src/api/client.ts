import type {
  AccountView,
  ArenaDetail,
  NotifySettings,
  OutboxEntry,
  PreflightReport,
  LearningOverview,
  OptimizerRunT,
  PendingT,
  AppSettings,
  BarsResponse,
  JournalEntry,
  LogLine,
  Overview,
  PositionView,
  RiskOverview,
  RiskProfile,
  Session,
  SessionInput,
  SimState,
  Snapshot,
  StrategyInfo,
  Stats,
  SymbolRow,
  Timeframe,
  Trade,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  const data = text ? JSON.parse(text) : undefined;
  if (!res.ok) {
    let msg = data?.message ?? res.statusText;
    if (Array.isArray(data?.detail)) {
      msg = data.detail.map((d: { loc: string[]; msg: string }) => `${d.loc.slice(1).join(".")}: ${d.msg}`).join("; ");
    }
    throw new ApiError(res.status, data?.code ?? "error", msg);
  }
  return data as T;
}

const qs = (p: Record<string, string | number | boolean | undefined | null | string[]>) => {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(p)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => u.append(k, x));
    else u.set(k, String(v));
  }
  const s = u.toString();
  return s ? `?${s}` : "";
};

export const api = {
  status: () => req<Snapshot>("GET", "/status"),
  overview: () => req<Overview>("GET", "/overview"),

  sessions: () => req<Session[]>("GET", "/sessions"),
  session: (id: number) => req<Session>("GET", `/sessions/${id}`),
  createSession: (body: SessionInput) => req<Session>("POST", "/sessions", body),
  updateSession: (id: number, body: SessionInput) => req<Session>("PUT", `/sessions/${id}`, body),
  deleteSession: (id: number) => req<void>("DELETE", `/sessions/${id}`),
  startSession: (id: number) => req<Session>("POST", `/sessions/${id}/start`),
  pauseSession: (id: number) => req<Session>("POST", `/sessions/${id}/pause`),
  resumeSession: (id: number) => req<Session>("POST", `/sessions/${id}/resume`),
  stopSession: (id: number, close_positions: boolean) => req<Session>("POST", `/sessions/${id}/stop`, { close_positions }),

  symbols: (q = "", limit = 60) => req<SymbolRow[]>("GET", `/symbols${qs({ q, limit })}`),
  symbolNames: () => req<string[]>("GET", "/symbols/names"),
  bars: (symbol: string, timeframe: Timeframe, count = 300) =>
    req<BarsResponse>("GET", `/symbols/${encodeURIComponent(symbol)}/bars${qs({ timeframe, count })}`),

  strategies: () => req<StrategyInfo[]>("GET", "/strategies"),

  risk: () => req<RiskOverview>("GET", "/risk"),
  setLimits: (body: RiskOverview["limits"]) => req<RiskOverview>("PUT", "/risk/limits", body),
  createProfile: (body: Omit<RiskProfile, "id">) => req<RiskProfile>("POST", "/risk/profiles", body),
  updateProfile: (id: number, body: Omit<RiskProfile, "id">) => req<RiskProfile>("PUT", `/risk/profiles/${id}`, body),
  deleteProfile: (id: number) => req<void>("DELETE", `/risk/profiles/${id}`),
  killSwitch: () => req<{ stopped_sessions: string[]; closed_positions: number; left_open: number }>("POST", "/kill-switch"),
  setLiveCaps: (body: { max_risk_pct: number; max_volume: number; confirm_login?: number | null }) => req<RiskOverview>("PUT", "/risk/live-caps", body),
  resetFloor: (confirm_login: number, pct: number) => req<RiskOverview>("POST", "/risk/equity-floor/reset", { confirm_login, pct }),
  preflight: (session_id?: number) => req<PreflightReport>("GET", `/preflight${qs({ session_id })}`),

  positions: () => req<PositionView[]>("GET", "/positions"),
  closePosition: (ticket: number) => req<unknown>("POST", `/positions/${ticket}/close`),

  trades: (p: { session_id?: number; symbol?: string; strategy?: string; status?: string; limit?: number }) =>
    req<{ trades: Trade[]; stats: Stats; curve: { ts: number; value: number }[] }>("GET", `/trades${qs(p)}`),
  journal: (p: { session_id?: number; kind?: string[]; level?: string; alerts?: boolean; symbol?: string; limit?: number }) =>
    req<JournalEntry[]>("GET", `/journal${qs(p)}`),
  logs: (p: { level?: string; after?: number; limit?: number }) => req<LogLine[]>("GET", `/logs${qs(p)}`),

  account: () => req<AccountView>("GET", "/account"),
  setLiveEnabled: (enabled: boolean, confirm: string) => req<AccountView>("PUT", "/account/live-enabled", { enabled, confirm }),
  reconnect: () => req<AccountView>("POST", "/account/reconnect"),

  settings: () => req<{ app: AppSettings; defaults: AppSettings; mode: string; notify: NotifySettings }>("GET", "/settings"),
  saveNotify: (body: { enabled?: boolean; telegram_token?: string; telegram_chat_id?: string }) => req<NotifySettings>("PUT", "/settings/notify", body),
  testNotify: () => req<OutboxEntry>("POST", "/settings/notify/test"),
  outbox: () => req<OutboxEntry[]>("GET", "/notify/outbox"),
  saveSettings: (body: Partial<AppSettings>) => req<{ app: AppSettings; defaults: AppSettings; mode: string }>("PUT", "/settings", body),

  learning: () => req<LearningOverview>("GET", "/learning"),
  arena: (symbol: string, timeframe: string) => req<ArenaDetail>("GET", `/learning/arenas/${encodeURIComponent(symbol)}/${timeframe}`),
  optimize: (symbol: string, timeframe: string) => req<{ run_id: number }>("POST", `/learning/arenas/${encodeURIComponent(symbol)}/${timeframe}/optimize`),
  learnNow: () => req<{ queued: { symbol: string; timeframe: string; run_id: number }[] }>("POST", "/learning/run"),
  run: (id: number) => req<OptimizerRunT>("GET", `/learning/runs/${id}`),
  promote: (challengerId: number, sessionId: number, force = false) =>
    req<PendingT>("POST", `/learning/challengers/${challengerId}/promote`, { session_id: sessionId, force }),
  rollback: (sessionId: number, symbol: string) => req<PendingT>("POST", `/learning/slots/${sessionId}/${encodeURIComponent(symbol)}/rollback`),
  setAutoPromote: (sessionId: number, symbol: string, enabled: boolean) =>
    req<unknown>("PUT", `/learning/slots/${sessionId}/${encodeURIComponent(symbol)}/auto-promote`, { enabled }),
  cancelPending: (id: number) => req<void>("DELETE", `/learning/pending/${id}`),

  simState: () => req<SimState>("GET", "/sim/state"),
  simAdvance: (bars: number) => req<SimState>("POST", "/sim/advance", { bars }),
  simShock: (symbol: string, pct: number) => req<SimState>("POST", "/sim/shock", { symbol, pct }),
  simClock: (speed: number) => req<SimState>("POST", "/sim/clock", { speed }),
  simFault: (kind: string, count = 1) => req<SimState>("POST", "/sim/fault", { kind, count }),
  simAccount: (body: { login?: number; is_demo?: boolean; margin_mode?: string; algo_trading?: boolean }) => req<SimState>("POST", "/sim/account", body),
};
