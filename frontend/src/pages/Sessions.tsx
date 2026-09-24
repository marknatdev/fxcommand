import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useLive } from "../api/live";
import { SessionControls } from "../components/domain";
import { Badge, Button, Card, Empty, ErrorBox, Loading, PageHeader, Pnl, StatusBadge } from "../components/ui";
import { strategyTitle } from "../lib/format";

export default function Sessions() {
  const nav = useNavigate();
  const { snapshot } = useLive();
  const q = useQuery({ queryKey: ["sessions"], queryFn: api.sessions, refetchInterval: 5000 });
  const live = new Map(snapshot?.sessions.map((s) => [s.id, s]) ?? []);

  return (
    <div>
      <PageHeader
        title="Sessions"
        subtitle="Each Session trades a set of Symbols, each bound to one Strategy, Timeframe and Risk Profile."
        actions={
          <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => nav("/sessions/new")} data-testid="new-session">
            New session
          </Button>
        }
      />
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : !q.data!.length ? (
        <Card>
          <Empty>
            No sessions yet.
            <Button variant="primary" size="sm" onClick={() => nav("/sessions/new")}>
              Create your first session
            </Button>
          </Empty>
        </Card>
      ) : (
        <Card bodyClass="p-0">
          <div className="overflow-x-auto">
            <table className="w-full" data-testid="sessions-table">
              <thead className="border-b border-line">
                <tr>
                  <th className="th">Session</th>
                  <th className="th">Status</th>
                  <th className="th">Assignments</th>
                  <th className="th text-right">Positions</th>
                  <th className="th text-right">Today</th>
                  <th className="th text-right">Net</th>
                  <th className="th text-right">Trades</th>
                  <th className="th text-right">Win %</th>
                  <th className="th">Controls</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line/60">
                {q.data!.map((s) => {
                  const l = live.get(s.id);
                  const status = l?.status ?? s.status;
                  return (
                    <tr key={s.id} className="hover:bg-panel-2/50" data-testid="session-row">
                      <td className="td">
                        <Link to={`/sessions/${s.id}`} className="font-medium text-ink hover:text-accent" data-testid="session-link">
                          {s.name}
                        </Link>
                        <div className="num text-[11px] text-faint">
                          magic {s.magic} {s.execution === "paper" && <Badge tone="accent" testId="session-paper">PAPER</Badge>}
                        </div>
                      </td>
                      <td className="td">
                        <StatusBadge status={status} />
                        {status !== "running" && s.stop_reason && <div className="mt-1 max-w-[220px] truncate text-[11px] text-faint" title={s.stop_reason}>{s.stop_reason}</div>}
                      </td>
                      <td className="td">
                        <div className="flex max-w-[420px] flex-wrap gap-1">
                          {s.assignments.map((a) => (
                            <Badge key={a.id} tone={a.enabled ? "accent" : "neutral"} className="font-mono">
                              {a.symbol} · {a.timeframe} · {strategyTitle[a.strategy] ?? a.strategy}
                            </Badge>
                          ))}
                        </div>
                      </td>
                      <td className="td num text-right">{l?.open_positions ?? s.open_positions}</td>
                      <td className="td text-right">
                        <Pnl value={l?.day_pnl ?? s.day_pnl} />
                      </td>
                      <td className="td text-right">
                        <Pnl value={s.stats.net_profit} />
                      </td>
                      <td className="td num text-right">{s.stats.trades}</td>
                      <td className="td num text-right">{s.stats.trades ? `${s.stats.win_rate.toFixed(0)}%` : "—"}</td>
                      <td className="td">
                        <SessionControls id={s.id} name={s.name} status={status} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}
