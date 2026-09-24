import { useQueryClient } from "@tanstack/react-query";
import { Lock, RotateCcw, Save } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api } from "../api/client";
import type { RiskOverview } from "../api/types";
import { money, serverTime } from "../lib/format";
import { Badge, Button, Card, Dialog, Field } from "./ui";

/** Live Caps: hard limits above every Risk Profile, applied only to Broker Sessions on a live account. */
export function LiveCapsCard({ r }: { r: RiskOverview }) {
  const qc = useQueryClient();
  const [caps, setCaps] = useState(r.live_caps);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [login, setLogin] = useState("");
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => setCaps(r.live_caps), [r.live_caps.max_risk_pct, r.live_caps.max_volume]); // eslint-disable-line react-hooks/exhaustive-deps
  const save = async (confirm_login?: number) => {
    try {
      await api.setLiveCaps({ ...caps, confirm_login });
      toast.success("Live Caps saved");
      setConfirmOpen(false);
      qc.invalidateQueries({ queryKey: ["risk"] });
    } catch (e) {
      setErr((e as Error).message);
      if (!confirm_login) toast.error((e as Error).message);
    }
  };
  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          <Lock className="size-4" /> Live Caps
        </span>
      }
      testId="live-caps"
    >
      <p className="mb-3 text-sm text-dim">
        Applied only to Broker-mode Sessions on a <b>live</b> account, above any Risk Profile. {r.account_live ? <Badge tone="down">in force</Badge> : <Badge>not in force (demo)</Badge>}
      </p>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Max risk per trade (%)">
          <input type="number" step={0.1} min={0.1} className="field num" value={caps.max_risk_pct} onChange={(e) => setCaps({ ...caps, max_risk_pct: +e.target.value })} data-testid="cap-risk" />
        </Field>
        <Field label="Max volume per order (lots)">
          <input type="number" step={0.01} min={0.01} className="field num" value={caps.max_volume} onChange={(e) => setCaps({ ...caps, max_volume: +e.target.value })} data-testid="cap-volume" />
        </Field>
        <div className="flex items-start pt-5">
          <Button
            variant="primary"
            icon={<Save className="size-4" />}
            data-testid="save-caps"
            onClick={() => {
              setErr(null);
              setLogin("");
              if (r.account_live) setConfirmOpen(true);
              else save();
            }}
          >
            Save caps
          </Button>
        </div>
      </div>
      <Dialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Change Live Caps on a LIVE account?"
        description="Type the account number to confirm."
        testId="caps-confirm-dialog"
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => save(Number(login))} data-testid="caps-confirm">
              Save caps
            </Button>
          </>
        }
      >
        <Field label={`Account number (${r.login ?? ""})`}>
          <input className="field num" value={login} onChange={(e) => setLogin(e.target.value)} autoFocus data-testid="caps-confirm-input" />
        </Field>
        {err && <p className="mt-2 text-sm text-down">{err}</p>}
      </Dialog>
    </Card>
  );
}

/** Equity Floor: below it the Kill Switch fires and live Sessions cannot start until reset. */
export function EquityFloorCard({ r }: { r: RiskOverview }) {
  const qc = useQueryClient();
  const f = r.equity_floor;
  const [open, setOpen] = useState(false);
  const [login, setLogin] = useState("");
  const [pct, setPct] = useState(80);
  const [err, setErr] = useState<string | null>(null);
  return (
    <Card title="Equity Floor" testId="equity-floor">
      {f ? (
        <div className="space-y-2 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <span className="num text-lg font-semibold" data-testid="floor-value">
              {money(f.floor)}
            </span>
            <span className="text-dim">({f.pct}% of equity when set, {serverTime(f.set_at)})</span>
            {f.breached_at ? <Badge tone="down" testId="floor-breached">BREACHED {serverTime(f.breached_at)}</Badge> : <Badge tone="up">armed</Badge>}
          </div>
          {r.equity !== null && <p className="text-xs text-dim">Current equity {money(r.equity)} — headroom {money(r.equity - f.floor)}</p>}
          <p className="text-xs text-dim">If equity falls below the floor the Kill Switch fires; live Sessions cannot start until you reset it.</p>
        </div>
      ) : (
        <p className="text-sm text-dim">{r.account_live ? "Set automatically (80% of equity) when live trading is enabled." : "Only used on live accounts."}</p>
      )}
      {r.account_live && (
        <Button
          className="mt-3"
          variant={f?.breached_at ? "danger" : "secondary"}
          icon={<RotateCcw className="size-4" />}
          data-testid="reset-floor"
          onClick={() => {
            setErr(null);
            setLogin("");
            setOpen(true);
          }}
        >
          Reset floor
        </Button>
      )}
      <Dialog
        open={open}
        onOpenChange={setOpen}
        title="Reset the Equity Floor?"
        description="The new floor is a percentage of the current equity. Type the account number to confirm."
        testId="floor-dialog"
        footer={
          <>
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              data-testid="floor-confirm"
              onClick={async () => {
                try {
                  await api.resetFloor(Number(login), pct);
                  toast.warning("Equity Floor reset");
                  setOpen(false);
                  qc.invalidateQueries({ queryKey: ["risk"] });
                } catch (e) {
                  setErr((e as Error).message);
                }
              }}
            >
              Reset floor
            </Button>
          </>
        }
      >
        <div className="grid gap-3">
          <Field label="New floor (% of current equity)">
            <input type="number" min={1} max={99} className="field num" value={pct} onChange={(e) => setPct(+e.target.value)} data-testid="floor-pct" />
          </Field>
          <Field label={`Account number (${r.login ?? ""})`}>
            <input className="field num" value={login} onChange={(e) => setLogin(e.target.value)} data-testid="floor-login" />
          </Field>
          {err && <p className="text-sm text-down">{err}</p>}
        </div>
      </Dialog>
    </Card>
  );
}
