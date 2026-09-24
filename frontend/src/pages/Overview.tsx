import { useQuery } from "@tanstack/react-query";
import { Activity, Bell, TrendingUp, Wallet } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useLive } from "../api/live";
import { ValueChart } from "../components/charts";
import { JournalList, PositionsTable, SessionControls } from "../components/domain";
import { Card, Empty, ErrorBox, Kpi, Loading, PageHeader, Pnl, StatusBadge } from "../components/ui";
import { cn, money, pnlClass, signed } from "../lib/format";

export default function Overview() {
  const { snapshot } = useLive();
  const q = useQuery({ queryKey: ["overview"], queryFn: api.overview, refetchInterval: 10_000 });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const o = q.data!;
  // live snapshot wins over the polled one for fast-moving numbers
  const snap = snapshot ?? o;
  const acct = snap.account;
  const ccy = acct?.currency ?? "";
  const sessions = snap.sessions;
  const running = sessions.filter((s) => s.status === "running").length;
  const paused = sessions.filter((s) => s.status === "paused").length;
  const owned = snap.positions.filter((p) => p.owned);
  const floating = owned.reduce((a, p) => a + p.profit, 0);
  const dayPct = snap.day_start_equity ? (snap.day_pnl / snap.day_start_equity) * 100 : 0;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Overview"
        subtitle={acct ? `${acct.name} · ${acct.login} @ ${acct.server} · leverage 1:${acct.leverage}` : "Waiting for broker…"}
      />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
        <Kpi testId="kpi-equity" label="Equity" value={money(acct?.equity)} sub={`Balance ${money(acct?.balance)} ${ccy}`} icon={<Wallet className="size-3.5" />} />
        <Kpi
          testId="kpi-day-pnl"
          label="Today P&L"
          value={signed(snap.day_pnl)}
          tone={pnlClass(snap.day_pnl)}
          sub={<span className={pnlClass(dayPct)}>{signed(dayPct)}% of start equity</span>}
          icon={<TrendingUp className="size-3.5" />}
        />
        <Kpi testId="kpi-floating" label="Floating" value={signed(floating)} tone={pnlClass(floating)} sub={`${owned.length} owned positions`} />
        <Kpi testId="kpi-sessions" label="Sessions" value={`${running} running`} sub={`${paused} paused · ${sessions.length} total`} icon={<Activity className="size-3.5" />} />
        <Kpi label="Win rate (all time)" value={`${o.all_time.win_rate.toFixed(1)}%`} sub={`${o.all_time.trades} closed trades`} />
        <Kpi label="Net (all time)" value={signed(o.all_time.net_profit)} tone={pnlClass(o.all_time.net_profit)} sub={`PF ${o.all_time.profit_factor ?? "—"} · DD ${money(o.all_time.max_drawdown)}`} />
      </div>

      <div className="grid gap-5 xl:grid-cols-3">
        <Card title="Equity curve" className="xl:col-span-2" testId="equity-card">
          {o.equity_curve.length > 1 ? (
            <ValueChart data={o.equity_curve} dataKey="equity" height={260} testId="equity-chart" />
          ) : (
            <Empty>Equity points are recorded every few minutes of server time.</Empty>
          )}
        </Card>
        <Card
          title={
            <span className="flex items-center gap-2">
              <Bell className="size-4 text-warn" /> Alerts
            </span>
          }
          testId="alerts-card"
          bodyClass="max-h-[300px] overflow-y-auto py-1"
        >
          <JournalList entries={o.alerts} max={20} />
        </Card>
      </div>

      <Card
        title="Sessions"
        testId="overview-sessions"
        actions={
          <Link to="/sessions" className="text-xs text-accent hover:underline">
            Manage sessions →
          </Link>
        }
        bodyClass="p-0"
      >
        {sessions.length === 0 ? (
          <Empty>
            No sessions yet.{" "}
            <Link to="/sessions/new" className="text-accent hover:underline">
              Create one
            </Link>
          </Empty>
        ) : (
          <div className="grid divide-y divide-line md:grid-cols-2 md:divide-y-0 xl:grid-cols-3">
            {sessions.map((s) => (
              <div key={s.id} className="flex flex-col gap-2 border-line p-4 md:border-b md:odd:border-r xl:border-r" data-testid="overview-session">
                <div className="flex items-center justify-between gap-2">
                  <Link to={`/sessions/${s.id}`} className="truncate font-medium hover:text-accent">
                    {s.name}
                  </Link>
                  <StatusBadge status={s.status} />
                </div>
                <div className="flex items-center gap-4 text-xs text-dim">
                  <span>
                    Today <Pnl value={s.day_pnl} />
                  </span>
                  <span>
                    Positions <span className="num text-ink">{s.open_positions}</span>
                  </span>
                  <span className="num text-faint">magic {s.magic}</span>
                </div>
                {s.stop_reason && s.status !== "running" && <p className={cn("text-xs", s.stop_reason.includes("AUTO") ? "text-down" : "text-faint")}>{s.stop_reason}</p>}
                <SessionControls id={s.id} name={s.name} status={s.status} />
              </div>
            ))}
          </div>
        )}
      </Card>

      <div className="grid gap-5 xl:grid-cols-3">
        <Card title="Open positions" className="xl:col-span-2" testId="overview-positions" bodyClass="p-0">
          <PositionsTable positions={snap.positions} compact />
        </Card>
        <Card title="Recent activity" testId="recent-card" bodyClass="max-h-[360px] overflow-y-auto py-1">
          <JournalList entries={o.recent} max={15} />
        </Card>
      </div>
    </div>
  );
}
