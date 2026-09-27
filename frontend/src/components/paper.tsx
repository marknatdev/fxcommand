import { useQuery, useQueryClient } from "@tanstack/react-query";
import { NotebookPen, RotateCcw } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { api } from "../api/client";
import { EvidenceBadgeView, r2 } from "./evidence";
import { Badge, Button, Card, Dialog, Empty, ErrorBox, Field, Kpi, Loading } from "./ui";
import { money, pnlClass, signed } from "../lib/format";

/** The notional Paper Account every Paper Session sizes from (ADR 0008). */
export function PaperAccountCard() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["paper-account"], queryFn: api.paperAccount, refetchInterval: 5000 });
  const [editOpen, setEditOpen] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [balance, setBalance] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const p = q.data!;
  const idle = p.active_sessions.length === 0 && p.open_positions === 0;
  const refresh = () => ["paper-account", "overview", "scorecard"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
  const act = async (fn: () => Promise<unknown>, ok: string, close: () => void) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      toast.success(ok);
      refresh();
      close();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <NotebookPen className="size-4" /> Paper Account
        </span>
      }
      testId="paper-account-card"
      actions={
        <>
          <Badge tone="accent">epoch {p.epoch}</Badge>
          <Button size="sm" onClick={() => { setBalance(String(p.start_balance)); setErr(null); setEditOpen(true); }} disabled={!idle} data-testid="paper-edit">
            Start balance
          </Button>
          <Button size="sm" variant="danger" icon={<RotateCcw className="size-3.5" />} onClick={() => { setConfirm(""); setBalance(""); setErr(null); setResetOpen(true); }} disabled={!idle} data-testid="paper-reset">
            Reset
          </Button>
        </>
      }
      bodyClass="space-y-3"
    >
      <p className="text-xs text-dim">A notional balance every Paper Session sizes from. Real prices, simulated fills; nothing reaches the account, and its money never counts toward the real Equity Floor or limits.</p>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Kpi testId="paper-equity" label="Equity" value={money(p.equity)} sub={`Balance ${money(p.balance)} ${p.currency}`} />
        <Kpi label="Start balance" value={money(p.start_balance)} />
        <Kpi label="Realised" value={signed(p.realized)} tone={pnlClass(p.realized)} />
        <Kpi label="Floating" value={signed(p.open_pnl)} tone={pnlClass(p.open_pnl)} sub={`${p.open_positions} open`} />
        <Kpi label="Today" value={signed(p.day_pnl)} tone={pnlClass(p.day_pnl)} />
      </div>
      {!idle && <p className="text-xs text-faint">Stop the Paper Sessions ({p.active_sessions.join(", ") || "none"}) and close Paper positions to change or reset it.</p>}

      <Dialog
        open={editOpen}
        onOpenChange={setEditOpen}
        title="Paper start balance"
        description="Changes the notional pool for this epoch; today's Paper loss limit restarts from the new equity."
        testId="paper-edit-dialog"
        footer={
          <Button variant="primary" loading={busy} disabled={!(Number(balance) > 0)} onClick={() => act(() => api.setPaperBalance(Number(balance)), "Paper start balance saved", () => setEditOpen(false))} data-testid="paper-edit-save">
            Save
          </Button>
        }
      >
        <Field label="Start balance">
          <input className="field num" type="number" min={1} value={balance} onChange={(e) => setBalance(e.target.value)} data-testid="paper-balance-input" />
        </Field>
        {err && <p className="mt-2 text-sm text-down">{err}</p>}
      </Dialog>

      <Dialog
        open={resetOpen}
        onOpenChange={setResetOpen}
        title="Reset the Paper Account?"
        description="Starts a new epoch. Earlier Paper trades are archived, never deleted; ticket numbers are never reused."
        testId="paper-reset-dialog"
        footer={
          <Button
            variant="danger"
            loading={busy}
            disabled={confirm !== "RESET PAPER"}
            onClick={() => act(() => api.resetPaper(confirm, balance ? Number(balance) : null), "Paper Account reset", () => setResetOpen(false))}
            data-testid="paper-reset-confirm"
          >
            Reset
          </Button>
        }
      >
        <div className="space-y-3">
          <Field label="New start balance (empty = keep)">
            <input className="field num" type="number" min={1} value={balance} onChange={(e) => setBalance(e.target.value)} data-testid="paper-reset-balance" />
          </Field>
          <Field label="Type RESET PAPER to confirm">
            <input className="field" value={confirm} onChange={(e) => setConfirm(e.target.value)} data-testid="paper-reset-input" />
          </Field>
          {err && <p className="text-sm text-down">{err}</p>}
        </div>
      </Dialog>
    </Card>
  );
}

const verdictTone = (v: string) => (v.startsWith("within") || v.startsWith("above") ? "up" : v.startsWith("below") ? "down" : "neutral");

/** Per Arena: the Paper record against the band its Evidence predicts, and the real Account's min-lot risk. */
export function ScorecardCard() {
  const q = useQuery({ queryKey: ["scorecard"], queryFn: api.scorecard, refetchInterval: 30_000 });
  return (
    <Card title="Graduation scorecard" testId="scorecard-card" bodyClass="space-y-3">
      <p className="text-xs text-dim">
        Advisory: does the Paper record look like its backtest? The band is where the mean R of that many trades falls 90% of the time in the Evidence. Min-lot risk is what one
        minimum lot at today&apos;s stop would risk on the real account. Nothing here blocks anything; going live is your decision.
      </p>
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} />
      ) : !q.data!.length ? (
        <Empty>No Arenas yet: add Assignments to a Session.</Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="scorecard-table">
            <thead className="border-b border-line">
              <tr>
                <th className="th">Arena</th>
                <th className="th">Session</th>
                <th className="th">Evidence</th>
                <th className="th text-right">Paper trades</th>
                <th className="th text-right">Paper mean</th>
                <th className="th">Expected band</th>
                <th className="th">Verdict</th>
                <th className="th text-right">Min-lot risk</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line/60">
              {q.data!.map((c) => (
                <tr key={`${c.symbol}|${c.timeframe}`} data-testid="scorecard-row">
                  <td className="td whitespace-nowrap font-medium">
                    {c.symbol} {c.timeframe}
                  </td>
                  <td className="td text-xs">
                    {c.session.name} {c.session.execution === "paper" && <Badge tone="accent">paper</Badge>}
                  </td>
                  <td className="td">
                    <EvidenceBadgeView badge={c.evidence} />
                  </td>
                  <td className="td num text-right">{c.paper.trades}</td>
                  <td className={`td num text-right ${pnlClass(c.paper.mean_r)}`}>{c.paper.trades ? r2(c.paper.mean_r, 3) : "—"}</td>
                  <td className="td num whitespace-nowrap text-xs">{c.band ? `${r2(c.band[0], 3)} … ${r2(c.band[1], 3)}` : "—"}</td>
                  <td className="td">
                    <Badge tone={verdictTone(c.verdict)} testId="scorecard-verdict">
                      {c.verdict}
                    </Badge>
                  </td>
                  <td className={`td num text-right ${c.min_lot_risk_pct !== null && c.min_lot_risk_pct > 5 ? "text-down" : ""}`}>
                    {c.min_lot_risk_pct === null ? "—" : `${c.min_lot_risk_pct.toFixed(1)}%`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
