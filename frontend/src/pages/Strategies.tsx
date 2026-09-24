import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { Badge, Card, Empty, ErrorBox, Kpi, Loading, PageHeader, Pnl } from "../components/ui";
import { pnlClass, signed } from "../lib/format";

export default function Strategies() {
  const q = useQuery({ queryKey: ["strategies"], queryFn: api.strategies, refetchInterval: 15_000 });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  return (
    <div className="space-y-5">
      <PageHeader title="Strategies" subtitle="Rules that read closed bars and emit Signals. Parameters are set per Assignment in each Session." />
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
            <div className="grid grid-cols-3 gap-2">
              <Kpi label="Net" value={signed(s.stats.net_profit)} tone={pnlClass(s.stats.net_profit)} />
              <Kpi label="Trades" value={s.stats.trades} />
              <Kpi label="Win %" value={s.stats.trades ? s.stats.win_rate.toFixed(0) : "—"} />
            </div>
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
              <div className="label">Performance by symbol</div>
              {Object.keys(s.by_symbol).length === 0 ? (
                <Empty>No closed trades yet</Empty>
              ) : (
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
                    {Object.entries(s.by_symbol).map(([sym, st]) => (
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
              )}
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
