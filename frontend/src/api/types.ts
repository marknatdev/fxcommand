export type SessionStatus = "stopped" | "running" | "paused" | "interrupted";
export type Side = "long" | "short";
export type Timeframe = "M1" | "M5" | "M15" | "M30" | "H1" | "H4" | "D1";
export const TIMEFRAMES: Timeframe[] = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];

export interface Account {
  login: number;
  name: string;
  server: string;
  company: string;
  currency: string;
  balance: number;
  equity: number;
  margin: number;
  margin_free: number;
  leverage: number;
  is_demo: boolean;
  trade_allowed: boolean;
  margin_mode: string;
}

export type Execution = "broker" | "paper";

export interface LiveCaps {
  max_risk_pct: number;
  max_volume: number;
}

export interface EquityFloor {
  floor: number;
  pct: number;
  set_at: number;
  breached_at: number | null;
}

export interface PreflightCheck {
  id: string;
  label: string;
  status: "pass" | "warn" | "fail";
  detail: string;
  blocking: boolean;
}

export interface PreflightReport {
  ok: boolean;
  live: boolean;
  paper: boolean;
  enforced: boolean;
  checks: PreflightCheck[];
  failed: string[];
  session_id: number | null;
}

export interface NotifySettings {
  enabled: boolean;
  telegram_token: string;
  telegram_chat_id: string;
  configured: boolean;
}

export interface OutboxEntry {
  wall: number;
  text: string;
  status: string;
}

export interface PositionView {
  ticket: number;
  symbol: string;
  side: Side;
  volume: number;
  price_open: number;
  price_current: number;
  sl: number;
  tp: number;
  profit: number;
  magic: number;
  time: number;
  comment: string;
  owned: boolean;
  session_id: number | null;
  session_name: string | null;
  strategy: string | null;
  timeframe: string | null;
  risk_amount: number | null;
  paper: boolean;
}

export interface SessionSummary {
  id: number;
  name: string;
  status: SessionStatus;
  magic: number;
  open_positions: number;
  day_pnl: number;
  stop_reason: string;
  execution: Execution;
  login: number | null;
}

export interface Snapshot {
  mode: "sim" | "mt5";
  connected: boolean;
  last_error: string;
  server_time: number;
  account: Account | null;
  live_enabled: boolean;
  day_start: number;
  day_start_equity: number;
  day_pnl: number;
  sessions: SessionSummary[];
  positions: PositionView[];
  kill_switch_at: number | null;
  passes: number;
  heartbeat_age: number | null;
  broker_busy_for: number;
  disconnected_for: number;
  equity_floor: EquityFloor | null;
  live_caps: LiveCaps;
}

export interface Stats {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  net_profit: number;
  gross_profit: number;
  gross_loss: number;
  profit_factor: number | null;
  avg_win: number;
  avg_loss: number;
  expectancy: number;
  best: number;
  worst: number;
  max_drawdown: number;
}

export interface JournalEntry {
  id: number;
  ts: number;
  wall: number;
  session_id: number | null;
  symbol: string | null;
  kind: string;
  level: "info" | "warn" | "error";
  alert: boolean;
  message: string;
  data: Record<string, unknown>;
}

export interface Trade {
  id: number;
  ticket: number;
  session_id: number | null;
  session_name?: string | null;
  assignment_id: number | null;
  magic: number;
  symbol: string;
  side: Side;
  volume: number;
  strategy: string;
  timeframe: string;
  open_time: number;
  open_price: number;
  sl: number;
  tp: number;
  initial_risk: number;
  risk_amount: number;
  signal_reason: string;
  status: "open" | "closed";
  close_time: number | null;
  close_price: number | null;
  profit: number | null;
  close_reason: string;
  adopted: boolean;
  paper: boolean;
}

export interface AssignmentState {
  last_bar_time: number | null;
  last_eval: number | null;
  last_signal: { action: string; reason: string; indicators: Record<string, number> } | null;
  atr: number;
  status: string;
}

export interface Assignment {
  id: number;
  session_id: number;
  symbol: string;
  timeframe: Timeframe;
  strategy: string;
  params: Record<string, number | boolean>;
  risk_profile_id: number;
  risk_profile_name: string | null;
  reverse_on_opposite: boolean;
  enabled: boolean;
  state: AssignmentState | null;
}

export interface Blackout {
  start: string;
  end: string;
}

export interface TradingWindow {
  enabled: boolean;
  open_day: number;
  open_time: string;
  close_day: number;
  close_time: string;
  blackouts: Blackout[];
}

export interface Session {
  id: number;
  name: string;
  status: SessionStatus;
  magic: number;
  auto_resume: boolean;
  window: TradingWindow;
  window_text: string;
  max_positions: number;
  daily_loss_pct: number;
  notes: string;
  created_at: number;
  started_at: number | null;
  stopped_at: number | null;
  stop_reason: string;
  execution: Execution;
  login: number | null;
  weekend_close: boolean;
  weekend_close_time: string;
  assignments: Assignment[];
  open_positions: number;
  day_pnl: number;
  stats: Stats;
  positions?: PositionView[];
  trades?: Trade[];
  journal?: JournalEntry[];
  equity?: { ts: number; value: number }[];
}

export interface AssignmentInput {
  symbol: string;
  timeframe: Timeframe;
  strategy: string;
  params: Record<string, number | boolean>;
  risk_profile_id: number | null;
  reverse_on_opposite: boolean;
  enabled: boolean;
}

export interface SessionInput {
  name: string;
  notes: string;
  auto_resume: boolean;
  max_positions: number;
  daily_loss_pct: number;
  window: TradingWindow;
  assignments: AssignmentInput[];
  execution: Execution;
  weekend_close: boolean;
  weekend_close_time: string;
  confirm_login?: number | null;
}

export interface StrategyParam {
  name: string;
  label: string;
  type: "int" | "float" | "bool";
  default: number | boolean;
  min: number | null;
  max: number | null;
  step: number | null;
  help: string;
}

export interface StrategyInfo {
  key: string;
  title: string;
  description: string;
  params: StrategyParam[];
  stats: Stats;
  assignments: number;
  by_symbol: Record<string, Stats>;
}

export interface RiskProfile {
  id: number;
  name: string;
  risk_pct: number;
  max_spread_points: number;
  breakeven: boolean;
  breakeven_at_r: number;
  trailing: boolean;
  trailing_atr: number;
  trailing_start_r: number;
  allow_min_lot: boolean;
  min_lot_max_risk_pct: number;
  used_by?: number;
}

export interface RiskOverview {
  limits: { max_positions_global: number; daily_loss_pct_global: number };
  profiles: RiskProfile[];
  exposure: {
    open_positions: number;
    day_pnl: number;
    day_start_equity: number;
    daily_loss_cap: number;
    floating: number;
    risk_at_stop: number;
  };
  kill_switch_at: number | null;
  live_caps: LiveCaps;
  equity_floor: EquityFloor | null;
  account_live: boolean;
  login: number | null;
  equity: number | null;
}

export interface SymbolRow {
  name: string;
  description: string;
  digits: number;
  point: number;
  trade_tick_size: number;
  trade_tick_value: number;
  contract_size: number;
  volume_min: number;
  volume_max: number;
  volume_step: number;
  stops_level: number;
  trade_mode: string;
  freeze_level: number;
  bid: number;
  ask: number;
  spread_points: number;
  time: number;
  used_by: { session_id: number; session_name: string; status: SessionStatus; timeframe: string; strategy: string; active: boolean }[];
  open_positions: number;
}

export interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface BarsResponse {
  symbol: string;
  timeframe: Timeframe;
  bars: Bar[];
  trades: Trade[];
}

export interface Overview extends Snapshot {
  equity_curve: { ts: number; equity: number; balance: number }[];
  alerts: JournalEntry[];
  recent: JournalEntry[];
  today: Stats;
  all_time: Stats;
}

export interface AppSettings {
  learning_enabled: boolean;
  learning_candidates: number;
  learning_bars: number;
  poll_interval: number;
  sim_speed: number;
  close_on_auto_stop: boolean;
  equity_snapshot_seconds: number;
  default_window: TradingWindow;
}

export interface LogLine {
  seq: number;
  time: number;
  level: string;
  logger: string;
  message: string;
}

export interface AccountView {
  mode: "sim" | "mt5";
  connected: boolean;
  last_error: string;
  server_time: number;
  account: Account | null;
  live_enabled: boolean;
  terminal_path: string | null;
  db: string;
  equity_floor: EquityFloor | null;
  heartbeat_age: number | null;
}

export interface SimState {
  server_time: number;
  open_positions: number;
  closed_trades: number;
  balance: number;
  prices: Record<string, number>;
  speed: number;
  faults: string[];
  login: number;
  is_demo: boolean;
  margin_mode: string;
}

// ------------------------------------------------------------------ learning
export interface RStatsT {
  n: number;
  mean: number;
  std: number;
  sqn: number;
  win_rate: number;
  total: number;
  max_dd: number;
}

export interface CandidateT {
  strategy: string;
  params: Record<string, number | boolean>;
  key: string;
  label: string;
}

export interface GuardCheck {
  name: string;
  ok: boolean;
  value: number | null;
  threshold: number | null;
  detail: string;
}

export interface ChallengerT {
  id: number;
  candidate: CandidateT;
  started_ts: number;
  note: string;
  oos: Partial<RStatsT>;
  robust: boolean | null;
  shadow: RStatsT;
  champion_shadow: RStatsT;
  checks: GuardCheck[];
  promotable: boolean;
}

export interface FilterT {
  mode: "observe" | "no_edge" | "active" | "disabled";
  samples: number;
  threshold: number;
  lift_r: number;
  keep_share: number;
  holdout: number;
  note: string;
  weights: number[];
}

export interface OptimizerRunT {
  id: number;
  symbol: string;
  timeframe: string;
  trigger: string;
  status: "queued" | "running" | "done" | "insufficient" | "failed";
  champion_key: string;
  queued_wall: number;
  started_wall: number | null;
  finished_wall: number | null;
  note: string;
  result?: {
    finalists?: { candidate: CandidateT; walk_forward: { oos: RStatsT; in_sample: RStatsT; positive_folds: number }; robust: boolean | null }[];
    champion?: { walk_forward: { oos: RStatsT } } | null;
    evaluated?: number;
    bars?: number;
    insufficient?: string | null;
  };
}

export interface PendingT {
  id: number;
  session_id: number;
  symbol: string;
  candidate_key: string;
  kind: string;
  reason: string;
  status: string;
  created_wall: number;
}

export interface ArenaT {
  symbol: string;
  timeframe: Timeframe;
  session: { id: number; name: string; status: SessionStatus };
  champion: CandidateT & { shadow: RStatsT };
  challengers: ChallengerT[];
  filter: FilterT;
  last_run: OptimizerRunT | null;
  pending: PendingT | null;
  auto_promote: boolean;
  auto_promote_allowed: boolean;
  versions: number;
}

export interface LearningOverview {
  enabled: boolean;
  mode: "sim" | "mt5";
  live_account: boolean;
  queue: { symbol: string; timeframe: string; run_id: number }[];
  arenas: ArenaT[];
  pending: PendingT[];
}

export interface ArenaDetail extends ArenaT {
  curves: Record<string, { ts: number; r: number }[]>;
  history: { id: number; version: number; kind: string; reason: string; label: string; candidate_key: string; server_ts: number; session_id: number }[];
  runs: OptimizerRunT[];
  signals: { id: number; ts: number; side: string; p_win: number | null; filter_mode: string; decision: string; ticket: number | null; r: number | null }[];
}
