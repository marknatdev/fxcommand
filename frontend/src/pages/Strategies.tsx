import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { api } from "../api/client";
import { TIMEFRAMES, type Stats, type Timeframe } from "../api/types";
import { EvidencePeriods, evidenceMeta, r2 } from "../components/evidence";
import { Badge, Button, Card, Empty, ErrorBox, Field, Kpi, Loading, PageHeader, Pnl } from "../components/ui";
import { pnlClass, serverTime, signed } from "../lib/format";

function StatsRow({ label, s, testId }: { label: string; s: Stats; testId?: string }) {
  return (
    <div data-testid={testId}>
      <div className="label">{label}</div>
      <div className="grid grid-cols-3 gap-2">
        <Kpi label="Net" value={signed(s.net_profit)} tone={pnlClass(s.net_profit)} />
        <Kpi label="Trades" value={s.trades} />
        <Kpi label="Win %" value={s.trades ? s.win_rate.toFixed(0) : "—"} />
      </div>
    </div>
  );
}

function BySymbol({ rows }: { rows: Record<string, Stats> }) {
  if (Object.keys(rows).length === 0) return <Empty>No closed trades yet</Empty>;
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-[11px] uppercase text-faint">
          <th className="py-1 text-left">Symbol</th>
          <th className="py-1 text-right">Trades</th>
          <th className="py-1 text-right">Win %</th>
          <th className="py-1 text-right">PF</th>
          <th className="py-1 text-right">Net</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-line/60">
        {Object.entries(rows).map(([sym, st]) => (
          <tr key={sym}>
            <td className="py-1.5 font-medium">{sym}</td>
            <td className="num py-1.5 text-right">{st.trades}</td>
            <td className="num py-1.5 text-right">{st.win_rate.toFixed(0)}</td>
            <td className="num py-1.5 text-right">{st.profit_factor ?? "—"}</td>
            <td className="py-1.5 text-right">
              <Pnl value={st.net_profit} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RunEvidence() {
  const qc = useQueryClient();
  const strategies = useQuery({ queryKey: ["strategies"], queryFn: api.strategies });
  const symbols = useQuery({ queryKey: ["symbol-names"], queryFn: api.symbolNames, staleTime: 60_000 });
  const [symbol, setSymbol] = useState("GOLD");
  const [timeframe, setTimeframe] = useState<Timeframe>("H4");
  const [strategy, setStrategy] = useState("trend_breakout");
  const run = useMutation({
    mutationFn: () => api.runEvidence({ symbol, timeframe, strategy }),
    onSuccess: () => {
      toast.success(`Evidence Run queued: ${symbol} ${timeframe}`);
      qc.invalidateQueries({ queryKey: ["evidence"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });
  return (
    <div className="grid items-end gap-3 md:grid-cols-[1fr_0.6fr_1.4fr_auto]" data-testid="run-evidence-form">
      <Field label="Symbol">
        <input className="field" list="evidence-symbols" value={symbol} onChange={(e) => setSymbol(e.target.value.trim())} data-testid="evidence-symbol" />
        <datalist id="evidence-symbols">
          {(symbols.data ?? []).map((n) => (
            <option key={n} value={n} />
          ))}
        </datalist>
      </Field>
      <Field label="Timeframe">
        <select className="field" value={timeframe} onChange={(e) => setTimeframe(e.target.value as Timeframe)} data-testid="evidence-timeframe">
          {TIMEFRAMES.map((t) => (
            <option key={t}>{t}</option>
          ))}
        </select>
      </Field>
      <Field label="Strategy (default parameters)">
        <select className="field" value={strategy} onChange={(e) => setStrategy(e.target.value)} data-testid="evidence-strategy">
          {(strategies.data ?? []).map((s) => (
            <option key={s.key} value={s.key}>
              {s.title}
            </option>
          ))}
        </select>
      </Field>
      <Button variant="primary" icon={<Play className="size-3.5" />} loading={run.isPending} disabled={!symbol} onClick={() => run.mutate()} data-testid="evidence-run">
        Run evidence
      </Button>
    </div>
  );
}

function EvidenceTable() {
  const q = useQuery({
    queryKey: ["evidence"],
    queryFn: () => api.evidence(),
    refetchInterval: (query) => (query.state.data?.some((e) => e.status === "queued" || e.status === "running") ? 3000 : 30_000),
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!q.data!.length) return <Empty>No Evidence yet. Run one above, or from an Assignment in the session editor.</Empty>;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm" data-testid="evidence-table">
        <thead className="border-b border-line">
          <tr>
            <th className="th">Arena</th>
            <th className="th">Candidate</th>
            <th className="th">Settings</th>
            <th className="th">Status</th>
            <th className="th text-right">Trades</th>
            <th className="th text-right">Mean</th>
            <th className="th text-right">SQN</th>
            <th className="th text-right">Max DD</th>
            <th className="th">By period</th>
            <th className="th">History</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line/60">
          {q.data!.map((e) => (
            <tr key={e.id} data-testid="evidence-row" className="align-top">
              <td className="td whitespace-nowrap font-medium">
                {e.symbol} {e.timeframe}
              </td>
              <td className="td min-w-48">{e.label}</td>
              <td className="td text-xs text-dim">
                {e.weekend_close ? `Weekend Close ${e.weekend_close}` : "no Weekend Close"}
                {e.exit_rules.breakeven ? " · breakeven" : ""}
                {e.exit_rules.trailing ? " · trailing" : ""}
              </td>
              <td className="td">
                <Badge tone={e.status === "done" ? "up" : e.status === "failed" ? "down" : e.status === "incomplete" ? "warn" : "info"} testId="evidence-status">
                  {e.status}
                </Badge>
                {e.note && <div className="mt-1 max-w-56 text-[11px] text-faint">{e.note}</div>}
              </td>
              <td className="td num text-right">{e.trades}</td>
              <td className={`td num text-right ${pnlClass(e.mean_r)}`}>{r2(e.mean_r, 3)}</td>
              <td className="td num text-right">{e.sqn.toFixed(2)}</td>
              <td className="td num text-right">{e.max_dd_r.toFixed(1)}R</td>
              <td className="td min-w-64">
                <EvidencePeriods e={e} />
              </td>
              <td className="td whitespace-nowrap text-[11px] text-dim">
                {e.bars ? evidenceMeta(e) : "—"}
                <div className="text-faint">run {serverTime(e.run_ts)}</div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function Strategies() {
  const q = useQuery({ queryKey: ["strategies"], queryFn: api.strategies, refetchInterval: 15_000 });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  return (
    <div className="space-y-5">
      <PageHeader title="Strategies" subtitle="Rules that read closed bars and emit Signals. Parameters are set per Assignment in each Session." />
      <Card title="Evidence" testId="evidence-card" bodyClass="space-y-4">
        <p className="text-xs text-dim">
          A read-only backtest of a Strategy on the feed&apos;s full history at twice today&apos;s typical spread, with swap. Mean R per trade after costs, and by period: an edge should hold in every one.
        </p>
        <RunEvidence />
        <EvidenceTable />
      </Card>
      <div className="grid gap-5 xl:grid-cols-3">
        {q.data!.map((s) => (
          <Card
            key={s.key}
            testId={`strategy-${s.key}`}
            title={s.title}
            actions={<Badge tone={s.assignments ? "accent" : "neutral"}>{s.assignments} assignments</Badge>}
            bodyClass="space-y-4"
          >
            <p className="text-sm text-dim">{s.description}</p>
            <StatsRow label="Real account" s={s.stats} testId="stats-real" />
            <StatsRow label="Paper Account (this epoch)" s={s.stats_paper} testId="stats-paper" />
            <div>
              <div className="label">Parameters (defaults)</div>
              <table className="w-full text-sm">
                <tbody className="divide-y divide-line/60">
                  {s.params.map((p) => (
                    <tr key={p.name}>
                      <td className="py-1.5 text-dim">{p.label}</td>
                      <td className="num py-1.5 text-right">{String(p.default)}</td>
                      <td className="num py-1.5 pl-3 text-right text-[11px] text-faint">{p.type === "bool" ? "on/off" : `${p.min ?? ""}–${p.max ?? ""}`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div>
              <div className="label">Real account by symbol</div>
              <BySymbol rows={s.by_symbol} />
            </div>
            {Object.keys(s.by_symbol_paper).length > 0 && (
              <div>
                <div className="label">Paper Account by symbol</div>
                <BySymbol rows={s.by_symbol_paper} />
              </div>
            )}
          </Card>
        ))}
      </div>
    </div>
  );
}
