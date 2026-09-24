import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export const cn = (...v: ClassValue[]) => twMerge(clsx(v));

export const money = (v: number | null | undefined, currency = "", digits = 2) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const s = v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return currency ? `${s} ${currency}` : s;
};

export const signed = (v: number | null | undefined, digits = 2) => {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return v > 0 ? `+${s}` : v < 0 ? `−${s}` : s;
};

export const pnlClass = (v: number | null | undefined) =>
  v === null || v === undefined || v === 0 ? "text-dim" : v > 0 ? "text-up" : "text-down";

export const price = (v: number | null | undefined, digits = 5) =>
  v === null || v === undefined || v === 0 ? "—" : v.toFixed(digits);

/** Server timestamps are broker server wall-clock encoded as epoch seconds: format in UTC. */
export const serverTime = (ts: number | null | undefined, withDate = true) => {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const hm = d.toISOString().slice(11, 19);
  return withDate ? `${d.toISOString().slice(0, 10)} ${hm}` : hm;
};

export const shortTime = (ts: number | null | undefined) => {
  if (!ts) return "—";
  const d = new Date(ts * 1000).toISOString();
  return `${d.slice(5, 10)} ${d.slice(11, 16)}`;
};

export const wallTime = (ts: number) => new Date(ts * 1000).toLocaleTimeString();

export const digitsFor = (p: number) => (p >= 1000 ? 2 : p >= 50 ? 3 : 5);

export const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export const strategyTitle: Record<string, string> = {
  ema_cross: "EMA Cross",
  donchian_breakout: "Donchian Breakout",
  rsi_reversion: "RSI Mean Reversion",
};
