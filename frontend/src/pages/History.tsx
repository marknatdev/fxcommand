import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { ValueChart } from "../components/charts";
import { JournalList, StatsGrid, TradesTable } from "../components/domain";
import { Card, Empty, ErrorBox, Loading, PageHeader, Switch, TabPanel, Tabs } from "../components/ui";

const KINDS = ["signal", "risk_reject", "order", "order_fail", "close", "modify", "state", "alert", "info"];

function TradesTab() {
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: api.sessions });
  const strategies = useQuery({ queryKey: ["strategies"], queryFn: api.strategies });
  const [f, setF] = useState<{ session_id?: number; symbol?: string; strategy?: string; status?: string }>({});
  const q = useQuery({ queryKey: ["trades", f], queryFn: () => api.trades({ ...f, limit: 2000 }), refetchInterval: 8000 });
  const symbols = Array.from(new Set((sessions.data ?? []).flatMap((s) => s.assignments.map((a) => a.symbol)))).sort();
  return (
    <div className="space-y-4">
      <Card bodyClass="flex flex-wrap gap-3">
        <select className="field w-48" value={f.session_id ?? ""} onChange={(e) => setF({ ...f, session_id: e.target.value ? +e.target.value : undefined })} data-testid="filter-session">
          <option value="">All sessions</option>
          {(sessions.data ?? []).map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        <select className="field w-40" value={f.symbol ?? ""} onChange={(e) => setF({ ...f, symbol: e.target.value || undefined })} data-testid="filter-symbol">
          <option value="">All symbols</option>
          {symbols.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        <select className="field w-48" value={f.strategy ?? ""} onChange={(e) => setF({ ...f, strategy: e.target.value || undefined })}>
          <option value="">All strategies</option>
          {(strategies.data ?? []).map((s) => (
            <option key={s.key} value={s.key}>
              {s.title}
            </option>
          ))}
        </select>
        <select className="field w-36" value={f.status ?? ""} onChange={(e) => setF({ ...f, status: e.target.value || undefined })} data-testid="filter-status">
          <option value="">Open & closed</option>
          <option value="open">Open</option>
          <option value="closed">Closed</option>
        </select>
      </Card>
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : (
        <>
          <StatsGrid stats={q.data!.stats} />
          <Card title="Cumulative P&L (closed trades)">
            {q.data!.curve.length > 1 ? <ValueChart data={q.data!.curve} dataKey="value" baseline={0} testId="history-curve" /> : <Empty>Needs at least two closed trades</Empty>}
          </Card>
          <Card bodyClass="p-0">
            <TradesTable trades={q.data!.trades} showSession />
          </Card>
        </>
      )}
    </div>
  );
}

function JournalTab() {
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: api.sessions });
  const [sessionId, setSessionId] = useState<number | undefined>();
  const [kinds, setKinds] = useState<string[]>([]);
  const [alerts, setAlerts] = useState(false);
  const q = useQuery({
    queryKey: ["journal", sessionId, kinds, alerts],
    queryFn: () => api.journal({ session_id: sessionId, kind: kinds, alerts, limit: 500 }),
    refetchInterval: 5000,
  });
  return (
    <div className="space-y-4">
      <Card bodyClass="flex flex-wrap items-center gap-3">
        <select className="field w-48" value={sessionId ?? ""} onChange={(e) => setSessionId(e.target.value ? +e.target.value : undefined)} data-testid="journal-session">
          <option value="">All sessions</option>
          {(sessions.data ?? []).map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        <div className="flex flex-wrap gap-1">
          {KINDS.map((k) => (
            <button
              key={k}
              data-testid={`kind-${k}`}
              onClick={() => setKinds(kinds.includes(k) ? kinds.filter((x) => x !== k) : [...kinds, k])}
              className={`rounded-full border px-2.5 py-0.5 text-xs ${kinds.includes(k) ? "border-accent bg-accent/15 text-accent" : "border-line-2 text-dim hover:text-ink"}`}
            >
              {k.replace("_", " ")}
            </button>
          ))}
        </div>
        <div className="ml-auto">
          <Switch checked={alerts} onChange={setAlerts} label="Alerts only" testId="alerts-only" />
        </div>
      </Card>
      <Card bodyClass="py-1" testId="journal-card">
        {q.isLoading ? <Loading /> : q.error ? <ErrorBox error={q.error} /> : <JournalList entries={q.data!} />}
      </Card>
    </div>
  );
}

export default function History() {
  const [tab, setTab] = useState("trades");
  return (
    <div>
      <PageHeader title="History & Journal" subtitle="Closed trades with statistics, and the Journal: every Signal, Risk Gate decision, order and state change — and why." />
      <Tabs value={tab} onChange={setTab} tabs={[{ value: "trades", label: "Trades" }, { value: "journal", label: "Journal" }]}>
        <TabPanel value="trades">
          <TradesTab />
        </TabPanel>
        <TabPanel value="journal">
          <JournalTab />
        </TabPanel>
      </Tabs>
    </div>
  );
}
