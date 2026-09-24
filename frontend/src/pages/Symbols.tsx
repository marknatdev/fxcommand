import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useLive } from "../api/live";
import { TIMEFRAMES, type Timeframe } from "../api/types";
import { CandleChart } from "../components/charts";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader } from "../components/ui";
import { cn, strategyTitle } from "../lib/format";

export default function Symbols() {
  const { snapshot } = useLive();
  const [selected, setSelected] = useState<string | null>(null);
  const [tf, setTf] = useState<Timeframe>("M15");
  const [filter, setFilter] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    const t = window.setTimeout(() => setQuery(filter.trim()), 250);
    return () => window.clearTimeout(t);
  }, [filter]);
  const q = useQuery({ queryKey: ["symbols", query], queryFn: () => api.symbols(query, 60), refetchInterval: 2000, placeholderData: (prev) => prev });
  const sym = selected ?? q.data?.[0]?.name ?? null;
  const bars = useQuery({ queryKey: ["bars", sym, tf], queryFn: () => api.bars(sym!, tf, 300), enabled: !!sym, refetchInterval: 5000 });
  const info = q.data?.find((s) => s.name === sym);

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const rows = q.data!;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Symbols"
        subtitle={
          snapshot?.mode === "mt5"
            ? "Symbols in the terminal's Market Watch (first 60 matches; symbols used by a session first). Filter to find others."
            : "Simulated market symbols."
        }
        actions={<input className="field w-56" placeholder="Filter symbols…" value={filter} onChange={(e) => setFilter(e.target.value)} data-testid="symbol-filter" />}
      />
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1.2fr)]">
        <Card bodyClass="p-0" testId="symbols-table">
          <div className="max-h-[640px] overflow-auto">
            <table className="w-full">
              <thead className="sticky top-0 border-b border-line bg-panel">
                <tr>
                  <th className="th">Symbol</th>
                  <th className="th text-right">Bid</th>
                  <th className="th text-right">Ask</th>
                  <th className="th text-right">Spread</th>
                  <th className="th">Traded by</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line/60">
                {rows.map((s) => (
                  <tr
                    key={s.name}
                    onClick={() => setSelected(s.name)}
                    data-testid="symbol-row"
                    className={cn("cursor-pointer hover:bg-panel-2/60", s.name === sym && "bg-accent/5")}
                  >
                    <td className="td">
                      <div className="font-medium">{s.name}</div>
                      <div className="max-w-[150px] truncate text-[11px] text-faint">{s.description}</div>
                    </td>
                    <td className="td num text-right">{s.bid.toFixed(s.digits)}</td>
                    <td className="td num text-right">{s.ask.toFixed(s.digits)}</td>
                    <td className="td num text-right text-dim">{s.spread_points}</td>
                    <td className="td">
                      <div className="flex flex-wrap gap-1">
                        {s.used_by.length === 0 && <span className="text-xs text-faint">—</span>}
                        {s.used_by.map((u) => (
                          <span key={u.session_id} title={`${u.session_name} · ${u.timeframe} · ${u.status}`}>
                            <Badge tone={u.active ? "up" : "neutral"} className="max-w-[130px] truncate">
                              {u.session_name}
                            </Badge>
                          </span>
                        ))}
                        {s.open_positions > 0 && <Badge tone="accent">{s.open_positions} open</Badge>}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!rows.length && <Empty>No symbols match</Empty>}
          </div>
        </Card>
        <div className="space-y-5">
          <Card
            title={sym ? `${sym} · ${info?.description ?? ""}` : "Chart"}
            testId="symbol-chart"
            actions={
              <div className="flex gap-1">
                {TIMEFRAMES.map((t) => (
                  <button key={t} onClick={() => setTf(t)} className={cn("rounded px-2 py-0.5 text-xs", t === tf ? "bg-accent/15 text-accent" : "text-dim hover:text-ink")} data-testid={`tf-${t}`}>
                    {t}
                  </button>
                ))}
              </div>
            }
          >
            {bars.data?.bars.length ? (
              <CandleChart
                bars={bars.data.bars}
                trades={bars.data.trades}
                positions={(snapshot?.positions ?? []).filter((p) => p.symbol === sym && p.owned)}
                digits={info?.digits ?? 5}
                height={420}
              />
            ) : bars.isLoading ? (
              <Loading />
            ) : (
              <Empty>No bars for this symbol</Empty>
            )}
          </Card>
          {info && (
            <Card title="Contract specification">
              <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm md:grid-cols-3">
                {(
                  [
                    ["Digits", info.digits],
                    ["Point", info.point],
                    ["Contract size", info.contract_size.toLocaleString()],
                    ["Tick value (1 lot)", info.trade_tick_value.toFixed(4)],
                    ["Volume min / step", `${info.volume_min} / ${info.volume_step}`],
                    ["Volume max", info.volume_max],
                    ["Stops level", `${info.stops_level} pts`],
                    ["Spread", `${info.spread_points} pts`],
                  ] as [string, string | number][]
                ).map(([k, v]) => (
                  <div key={k}>
                    <dt className="text-[11px] uppercase tracking-wide text-faint">{k}</dt>
                    <dd className="num">{v}</dd>
                  </div>
                ))}
              </dl>
              {info.used_by.length > 0 && (
                <div className="mt-4 border-t border-line pt-3 text-sm">
                  {info.used_by.map((u) => (
                    <div key={u.session_id} className="text-dim">
                      <Link to={`/sessions/${u.session_id}`} className="text-accent hover:underline">
                        {u.session_name}
                      </Link>{" "}
                      trades it on {u.timeframe} with {strategyTitle[u.strategy] ?? u.strategy} ({u.status})
                    </div>
                  ))}
                </div>
              )}
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
