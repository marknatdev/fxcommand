import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CircleDot, Info, Pause, Play, Square, XCircle } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { api } from "../api/client";
import type { JournalEntry, PositionView, SessionStatus, Stats, Trade } from "../api/types";
import { cn, digitsFor, money, price, serverTime, shortTime, strategyTitle } from "../lib/format";
import { Badge, Button, ConfirmDialog, Empty, Kpi, Pnl, SideBadge, Switch } from "./ui";

const refreshKeys = ["sessions", "session", "overview", "positions", "risk", "symbols"];

function useSessionAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (a: { id: number; op: "start" | "pause" | "resume" | "stop"; close?: boolean }) => {
      if (a.op === "start") return api.startSession(a.id);
      if (a.op === "pause") return api.pauseSession(a.id);
      if (a.op === "resume") return api.resumeSession(a.id);
      return api.stopSession(a.id, !!a.close);
    },
    onSuccess: (s, a) => {
      toast.success(`Session '${s.name}' ${a.op === "stop" ? "stopped" : a.op === "start" ? "started" : a.op + "d"}`);
      refreshKeys.forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
    },
    onError: (e: Error) => toast.error(e.message),
  });
}

/** Start / Pause / Resume / Stop buttons with the Stop dialog (leave vs close positions). */
export function SessionControls({ id, name, status, size = "sm" }: { id: number; name: string; status: SessionStatus; size?: "sm" | "md" }) {
  const act = useSessionAction();
  const [stopOpen, setStopOpen] = useState(false);
  const [closeAll, setCloseAll] = useState(false);
  const busy = act.isPending;
  return (
    <div className="flex items-center gap-1.5" data-testid={`controls-${id}`}>
      {(status === "stopped" || status === "interrupted") && (
        <Button size={size} variant="success" icon={<Play className="size-3.5" />} loading={busy} onClick={() => act.mutate({ id, op: "start" })} data-testid="start-session">
          {status === "interrupted" ? "Restart" : "Start"}
        </Button>
      )}
      {status === "running" && (
        <Button size={size} variant="warn" icon={<Pause className="size-3.5" />} loading={busy} onClick={() => act.mutate({ id, op: "pause" })} data-testid="pause-session">
          Pause
        </Button>
      )}
      {status === "paused" && (
        <Button size={size} variant="success" icon={<Play className="size-3.5" />} loading={busy} onClick={() => act.mutate({ id, op: "resume" })} data-testid="resume-session">
          Resume
        </Button>
      )}
      {status !== "stopped" && (
        <Button size={size} variant="danger" icon={<Square className="size-3.5" />} onClick={() => setStopOpen(true)} data-testid="stop-session">
          Stop
        </Button>
      )}
      <ConfirmDialog
        open={stopOpen}
        onOpenChange={(o) => {
          setStopOpen(o);
          if (!o) setCloseAll(false);
        }}
        testId="stop-dialog"
        title={`Stop session '${name}'?`}
        confirmLabel={closeAll ? "Stop & close positions" : "Stop, leave positions"}
        description="The engine stops evaluating this Session. Choose what happens to its open positions."
        onConfirm={() => act.mutateAsync({ id, op: "stop", close: closeAll })}
      >
        <div className="space-y-3 text-sm">
          <Switch checked={closeAll} onChange={setCloseAll} label="Close this session's open positions now" testId="stop-close-positions" />
          <p className="text-xs text-dim">
            {closeAll
              ? "All positions with this Session's magic number will be closed at market."
              : "Positions stay open, protected by their server-side stop-loss / take-profit. They are no longer managed (no trailing, no strategy exits)."}
          </p>
        </div>
      </ConfirmDialog>
    </div>
  );
}

/* --------------------------------------------------------------- positions */
export function PositionsTable({ positions, showSession = true, allowClose = true, compact = false }: { positions: PositionView[]; showSession?: boolean; allowClose?: boolean; compact?: boolean }) {
  const qc = useQueryClient();
  const [closing, setClosing] = useState<PositionView | null>(null);
  if (!positions.length) return <Empty>No open positions</Empty>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full" data-testid="positions-table">
        <thead className="border-b border-line">
          <tr>
            <th className="th">Ticket</th>
            <th className="th">Symbol</th>
            <th className="th">Side</th>
            <th className="th text-right">Volume</th>
            <th className="th text-right">Open</th>
            <th className="th text-right">Current</th>
            {!compact && <th className="th text-right">SL</th>}
            {!compact && <th className="th text-right">TP</th>}
            <th className="th text-right">P&L</th>
            {showSession && <th className="th">Owner</th>}
            {!compact && <th className="th">Opened</th>}
            {allowClose && <th className="th" />}
          </tr>
        </thead>
        <tbody className="divide-y divide-line/60">
          {positions.map((p) => {
            const d = digitsFor(p.price_open);
            return (
              <tr key={p.ticket} className="hover:bg-panel-2/60" data-testid="position-row">
                <td className="td num text-dim">#{p.ticket}</td>
                <td className="td font-medium">{p.symbol}</td>
                <td className="td">
                  <SideBadge side={p.side} />
                </td>
                <td className="td num text-right">{p.volume.toFixed(2)}</td>
                <td className="td num text-right">{price(p.price_open, d)}</td>
                <td className="td num text-right">{price(p.price_current, d)}</td>
                {!compact && <td className="td num text-right text-down/90">{price(p.sl, d)}</td>}
                {!compact && <td className="td num text-right text-up/90">{price(p.tp, d)}</td>}
                <td className="td text-right">
                  <Pnl value={p.profit} />
                </td>
                {showSession && (
                  <td className="td">
                    {p.owned ? (
                      <Link to={`/sessions/${p.session_id}`} className="text-accent hover:underline">
                        {p.session_name}
                      </Link>
                    ) : (
                      <Badge tone="neutral">foreign · magic {p.magic}</Badge>
                    )}
                  </td>
                )}
                {!compact && <td className="td num text-dim">{shortTime(p.time)}</td>}
                {allowClose && (
                  <td className="td text-right">
                    {p.owned && (
                      <Button size="sm" variant="ghost" icon={<XCircle className="size-3.5" />} onClick={() => setClosing(p)} data-testid="close-position">
                        Close
                      </Button>
                    )}
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
      <ConfirmDialog
        open={!!closing}
        onOpenChange={(o) => !o && setClosing(null)}
        title={`Close #${closing?.ticket} ${closing?.symbol}?`}
        description={`Closes ${closing?.volume} lots at market. Current P&L ${closing?.profit.toFixed(2)}.`}
        confirmLabel="Close position"
        onConfirm={async () => {
          try {
            await api.closePosition(closing!.ticket);
            toast.success(`Closed #${closing!.ticket}`);
            ["positions", "overview", "session", "trades"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
          } catch (e) {
            toast.error((e as Error).message);
            throw e;
          }
        }}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ trades */
export function TradesTable({ trades, showSession = false }: { trades: Trade[]; showSession?: boolean }) {
  if (!trades.length) return <Empty>No trades yet</Empty>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full" data-testid="trades-table">
        <thead className="border-b border-line">
          <tr>
            <th className="th">Ticket</th>
            <th className="th">Opened</th>
            <th className="th">Symbol</th>
            <th className="th">Side</th>
            <th className="th text-right">Vol</th>
            <th className="th text-right">Entry</th>
            <th className="th text-right">Exit</th>
            <th className="th">Strategy</th>
            {showSession && <th className="th">Session</th>}
            <th className="th">Result</th>
            <th className="th text-right">P&L</th>
            <th className="th text-right">R</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line/60">
          {trades.map((t) => {
            const d = digitsFor(t.open_price);
            const r = t.profit !== null && t.risk_amount ? t.profit / t.risk_amount : null;
            return (
              <tr key={t.ticket} className="hover:bg-panel-2/60" data-testid="trade-row" title={t.signal_reason}>
                <td className="td num text-dim">#{t.ticket}</td>
                <td className="td num text-dim">{shortTime(t.open_time)}</td>
                <td className="td font-medium">{t.symbol}</td>
                <td className="td">
                  <SideBadge side={t.side} />
                </td>
                <td className="td num text-right">{t.volume.toFixed(2)}</td>
                <td className="td num text-right">{price(t.open_price, d)}</td>
                <td className="td num text-right">{t.close_price ? price(t.close_price, d) : "—"}</td>
                <td className="td text-dim">
                  {strategyTitle[t.strategy] ?? (t.strategy || "—")} <span className="text-faint">{t.timeframe}</span>
                </td>
                {showSession && (
                  <td className="td">
                    {t.session_id ? (
                      <Link className="text-accent hover:underline" to={`/sessions/${t.session_id}`}>
                        {t.session_name ?? `#${t.session_id}`}
                      </Link>
                    ) : (
                      "—"
                    )}
                  </td>
                )}
                <td className="td">
                  {t.status === "open" ? <Badge tone="accent">open</Badge> : <Badge tone={t.close_reason === "tp" ? "up" : t.close_reason === "sl" ? "down" : "neutral"}>{t.close_reason || "closed"}</Badge>}
                  {t.adopted && <Badge className="ml-1">adopted</Badge>}
                </td>
                <td className="td text-right">{t.profit !== null ? <Pnl value={t.profit} /> : <span className="text-faint">—</span>}</td>
                <td className="td num text-right text-dim">{r !== null ? `${r >= 0 ? "+" : ""}${r.toFixed(2)}` : "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* ----------------------------------------------------------------- journal */
const kindTone: Record<string, "up" | "down" | "warn" | "info" | "accent" | "neutral"> = {
  signal: "info",
  order: "up",
  close: "accent",
  modify: "accent",
  risk_reject: "warn",
  order_fail: "down",
  alert: "down",
  state: "neutral",
  info: "neutral",
};

export function JournalList({ entries, max, showSession = true }: { entries: JournalEntry[]; max?: number; showSession?: boolean }) {
  const list = max ? entries.slice(0, max) : entries;
  if (!list.length) return <Empty>Nothing recorded yet</Empty>;
  return (
    <ul className="divide-y divide-line/60" data-testid="journal-list">
      {list.map((j) => {
        const Icon = j.level === "error" ? XCircle : j.level === "warn" ? AlertTriangle : j.kind === "signal" ? CircleDot : Info;
        return (
          <li key={j.id} className="flex gap-3 py-2 text-sm" data-testid="journal-entry">
            <Icon className={cn("mt-0.5 size-4 shrink-0", j.level === "error" ? "text-down" : j.level === "warn" ? "text-warn" : "text-faint")} />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge tone={kindTone[j.kind] ?? "neutral"}>{j.kind.replace("_", " ")}</Badge>
                {j.symbol && <span className="text-xs font-medium text-ink">{j.symbol}</span>}
                {showSession && j.session_id && (
                  <Link to={`/sessions/${j.session_id}`} className="text-xs text-accent hover:underline">
                    session #{j.session_id}
                  </Link>
                )}
                <span className="num ml-auto text-[11px] text-faint">{serverTime(j.ts)}</span>
              </div>
              <p className="mt-0.5 break-words text-dim">{j.message}</p>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/* ------------------------------------------------------------------- stats */
export function StatsGrid({ stats, currency = "" }: { stats: Stats; currency?: string }) {
  const items: [string, ReactNode, string?][] = [
    ["Net profit", <Pnl value={stats.net_profit} />],
    ["Trades", stats.trades],
    ["Win rate", `${stats.win_rate.toFixed(1)}%`],
    ["Profit factor", stats.profit_factor === null ? (stats.wins ? "∞" : "—") : stats.profit_factor.toFixed(2)],
    ["Expectancy", <Pnl value={stats.expectancy} />],
    ["Avg win / loss", `${money(stats.avg_win)} / ${money(stats.avg_loss)}`],
    ["Best / worst", `${money(stats.best)} / ${money(stats.worst)}`],
    ["Max drawdown", money(-stats.max_drawdown, currency)],
  ];
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4" data-testid="stats-grid">
      {items.map(([k, v]) => (
        <Kpi key={k} label={k} value={v} />
      ))}
    </div>
  );
}
