import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { useLive } from "../api/live";
import { PositionsTable } from "../components/domain";
import { Card, ErrorBox, Kpi, Loading, PageHeader, Switch } from "../components/ui";
import { pnlClass, signed } from "../lib/format";

export default function Positions() {
  const { snapshot } = useLive();
  const q = useQuery({ queryKey: ["positions"], queryFn: api.positions, refetchInterval: snapshot ? false : 3000 });
  const [ownedOnly, setOwnedOnly] = useState(false);
  if (!snapshot && q.isLoading) return <Loading />;
  if (!snapshot && q.error) return <ErrorBox error={q.error} />;
  const all = snapshot?.positions ?? q.data ?? [];
  const shown = ownedOnly ? all.filter((p) => p.owned) : all;
  const owned = all.filter((p) => p.owned);
  const pnl = (xs: typeof all) => xs.reduce((a, p) => a + p.profit, 0);
  return (
    <div className="space-y-5">
      <PageHeader
        title="Positions & Orders"
        subtitle="Live positions on the account. The engine sends market orders only; there are no pending orders."
        actions={<Switch checked={ownedOnly} onChange={setOwnedOnly} label="FXCommand positions only" testId="owned-only" />}
      />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Kpi label="Owned positions" value={owned.length} />
        <Kpi label="Owned floating" value={signed(pnl(owned))} tone={pnlClass(pnl(owned))} />
        <Kpi label="Foreign positions" value={all.length - owned.length} sub="manual / other EAs — never touched" />
        <Kpi label="Total floating" value={signed(pnl(all))} tone={pnlClass(pnl(all))} />
      </div>
      <Card bodyClass="p-0" testId="positions-card">
        <PositionsTable positions={shown} />
      </Card>
    </div>
  );
}
