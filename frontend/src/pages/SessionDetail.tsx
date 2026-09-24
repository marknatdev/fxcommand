import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Pencil } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { useLive } from "../api/live";
import { TIMEFRAMES, type Timeframe } from "../api/types";
import { CandleChart, ValueChart } from "../components/charts";
import { JournalList, PositionsTable, SessionControls, StatsGrid, TradesTable } from "../components/domain";
import { ArenaCard } from "./Learning";
import { Badge, Button, Card, Empty, ErrorBox, Kpi, Loading, PageHeader, StatusBadge, Tabs, TabPanel } from "../components/ui";
import { digitsFor, pnlClass, serverTime, signed, strategyTitle } from "../lib/format";

export default function SessionDetail() {
  const id = Number(useParams().id);
  const nav = useNavigate();
  const { snapshot } = useLive();
  const q = useQuery({ queryKey: ["session", id], queryFn: () => api.session(id), refetchInterval: 4000 });
  const [symbol, setSymbol] = useState<string | null>(null);
  const [tf, setTf] = useState<Timeframe | null>(null);
  const [tab, setTab] = useState("positions");
  const learning = useQuery({ queryKey: ["learning"], queryFn: api.learning, enabled: tab === "learning", refetchInterval: 5000 });

  const s = q.data;
  const chartSymbol = symbol ?? s?.assignments[0]?.symbol ?? null;
  const assignment = s?.assignments.find((a) => a.symbol === chartSymbol);
  const chartTf = tf ?? assignment?.timeframe ?? "M15";
  const bars = useQuery({
    queryKey: ["bars", chartSymbol, chartTf],
    queryFn: () => api.bars(chartSymbol!, chartTf, 250),
    enabled: !!chartSymbol,
    refetchInterval: 5000,
  });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  if (!s) return null;
  const live = snapshot?.sessions.find((x) => x.id === id);
  const status = live?.status ?? s.status;
  const positions = snapshot ? snapshot.positions.filter((p) => p.magic === s.magic) : s.positions ?? [];
  const sessionTrades = (bars.data?.trades ?? []).filter((t) => t.session_id === s.id);

  return (
    <div className="space-y-5">
      <Link to="/sessions" className="inline-flex items-center gap-1 text-xs text-dim hover:text-ink">
        <ArrowLeft className="size-3.5" /> Sessions
      </Link>
      <PageHeader
        title={s.name}
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            <StatusBadge status={status} />
            <span className="num">magic {s.magic}</span>·<span>{s.window_text}</span>
            {s.auto_resume && <Badge tone="info">auto-resume</Badge>}
          </span>
        }
        actions={
          <>
            <Button icon={<Pencil className="size-3.5" />} onClick={() => nav(`/sessions/${s.id}/edit`)} disabled={status === "running" || status === "paused"} data-testid="edit-session">
              Edit
            </Button>
            <SessionControls id={s.id} name={s.name} status={status} size="md" />
          </>
        }
      />
      {s.stop_reason && status !== "running" && (
        <div className={`rounded-lg border px-4 py-2 text-sm ${s.stop_reason.includes("AUTO") ? "border-down/40 bg-down/10 text-down" : "border-line bg-panel text-dim"}`} data-testid="stop-reason">
          {s.stop_reason}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Kpi testId="session-day-pnl" label="Today P&L" value={signed(live?.day_pnl ?? s.day_pnl)} tone={pnlClass(live?.day_pnl ?? s.day_pnl)} sub={`limit −${s.daily_loss_pct}% of start equity`} />
        <Kpi label="Open positions" value={positions.length} sub={`max ${s.max_positions}`} />
        <Kpi label="Net profit" value={signed(s.stats.net_profit)} tone={pnlClass(s.stats.net_profit)} sub={`${s.stats.trades} closed trades`} />
        <Kpi label="Win rate" value={s.stats.trades ? `${s.stats.win_rate.toFixed(1)}%` : "—"} sub={`PF ${s.stats.profit_factor ?? "—"}`} />
        <Kpi label="Started" value={<span className="text-sm">{serverTime(s.started_at)}</span>} sub={s.stopped_at ? `stopped ${serverTime(s.stopped_at)}` : undefined} />
      </div>

      <Card title="Assignments" bodyClass="p-0" testId="assignments-card">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead className="border-b border-line">
              <tr>
                <th className="th">Symbol</th>
                <th className="th">TF</th>
                <th className="th">Strategy</th>
                <th className="th">Risk profile</th>
                <th className="th">Engine state</th>
                <th className="th">Last signal</th>
                <th className="th text-right">ATR</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line/60">
              {s.assignments.map((a) => (
                <tr key={a.id} className="cursor-pointer hover:bg-panel-2/50" onClick={() => setSymbol(a.symbol)} data-testid="assignment-state-row">
                  <td className="td font-medium">
                    {a.symbol} {!a.enabled && <Badge>disabled</Badge>}
                  </td>
                  <td className="td num">{a.timeframe}</td>
                  <td className="td">{strategyTitle[a.strategy] ?? a.strategy}</td>
                  <td className="td text-dim">{a.risk_profile_name}</td>
                  <td className="td max-w-[280px] truncate text-dim" title={a.state?.status}>
                    {a.state?.status ?? "idle"}
                  </td>
                  <td className="td max-w-[320px] truncate" title={a.state?.last_signal?.reason}>
                    {a.state?.last_signal ? (
                      <>
                        <Badge tone={a.state.last_signal.action === "long" ? "up" : a.state.last_signal.action === "short" ? "down" : "neutral"}>{a.state.last_signal.action}</Badge>{" "}
                        <span className="text-dim">{a.state.last_signal.reason}</span>
                      </>
                    ) : (
                      <span className="text-faint">—</span>
                    )}
                  </td>
                  <td className="td num text-right text-dim">{a.state?.atr ? a.state.atr.toFixed(digitsFor(a.state.atr * 1000)) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card
        title={`Chart · ${chartSymbol ?? ""}`}
        testId="session-chart"
        actions={
          <div className="flex gap-2">
            <select className="field h-7 w-auto py-0 text-xs" value={chartSymbol ?? ""} onChange={(e) => setSymbol(e.target.value)} data-testid="chart-symbol">
              {s.assignments.map((a) => (
                <option key={a.id}>{a.symbol}</option>
              ))}
            </select>
            <select className="field h-7 w-auto py-0 text-xs" value={chartTf} onChange={(e) => setTf(e.target.value as Timeframe)} data-testid="chart-timeframe">
              {TIMEFRAMES.map((t) => (
                <option key={t}>{t}</option>
              ))}
            </select>
          </div>
        }
      >
        {bars.data?.bars.length ? (
          <CandleChart
            bars={bars.data.bars}
            trades={sessionTrades}
            positions={positions.filter((p) => p.symbol === chartSymbol)}
            digits={digitsFor(bars.data.bars[bars.data.bars.length - 1].close)}
          />
        ) : bars.isLoading ? (
          <Loading />
        ) : (
          <Empty>No bars</Empty>
        )}
      </Card>

      <Tabs
        value={tab}
        onChange={setTab}
        tabs={[
          { value: "positions", label: `Positions (${positions.length})` },
          { value: "trades", label: `Trades (${s.trades?.length ?? 0})` },
          { value: "journal", label: "Journal" },
          { value: "performance", label: "Performance" },
          { value: "learning", label: "Learning" },
        ]}
      >
        <TabPanel value="positions">
          <Card bodyClass="p-0">
            <PositionsTable positions={positions} showSession={false} />
          </Card>
        </TabPanel>
        <TabPanel value="trades">
          <Card bodyClass="p-0">
            <TradesTable trades={s.trades ?? []} />
          </Card>
        </TabPanel>
        <TabPanel value="journal">
          <Card bodyClass="py-1" testId="session-journal">
            <JournalList entries={s.journal ?? []} showSession={false} />
          </Card>
        </TabPanel>
        <TabPanel value="learning">
          <div className="space-y-4" data-testid="session-learning">
            {learning.isLoading ? (
              <Loading />
            ) : (
              (learning.data?.arenas ?? [])
                .filter((x) => x.session.id === s.id)
                .map((x) => <ArenaCard key={`${x.symbol}-${x.timeframe}`} arena={x} liveAccount={learning.data!.live_account} />)
            )}
            {learning.data && !learning.data.arenas.some((x) => x.session.id === s.id) && (
              <Card>
                <Empty>Another session is the active trader of these Arenas, or this session has not started yet.</Empty>
              </Card>
            )}
          </div>
        </TabPanel>
        <TabPanel value="performance">
          <div className="space-y-4">
            <StatsGrid stats={s.stats} />
            <Card title="Cumulative P&L">
              {(s.equity?.length ?? 0) > 1 ? <ValueChart data={s.equity!} dataKey="value" baseline={0} /> : <Empty>Needs at least two closed trades</Empty>}
            </Card>
          </div>
        </TabPanel>
      </Tabs>
    </div>
  );
}
