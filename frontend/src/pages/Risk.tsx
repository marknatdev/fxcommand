import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Octagon, Pencil, Plus, Save, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api } from "../api/client";
import { useLive } from "../api/live";
import type { RiskProfile } from "../api/types";
import { EquityFloorCard, LiveCapsCard } from "../components/LiveControls";
import { Badge, Button, Card, ConfirmDialog, Dialog, ErrorBox, Field, Kpi, Loading, PageHeader, ProgressBar, Switch } from "../components/ui";
import { money, pnlClass, serverTime, signed } from "../lib/format";

const BLANK: Omit<RiskProfile, "id"> = {
  name: "",
  risk_pct: 1,
  max_spread_points: 30,
  breakeven: false,
  breakeven_at_r: 1,
  trailing: false,
  trailing_atr: 2,
  trailing_start_r: 1,
  allow_min_lot: false,
  min_lot_max_risk_pct: 2,
};

function ProfileDialog({ profile, onClose }: { profile: RiskProfile | "new" | null; onClose: () => void }) {
  const qc = useQueryClient();
  const [form, setForm] = useState<Omit<RiskProfile, "id">>(BLANK);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    setErr(null);
    if (profile === "new") setForm(BLANK);
    else if (profile) {
      const { id: _id, used_by: _u, ...rest } = profile;
      setForm(rest);
    }
  }, [profile]);
  const num = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: Number(e.target.value) });
  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      if (profile === "new") await api.createProfile(form);
      else if (profile) await api.updateProfile(profile.id, form);
      toast.success(`Risk profile '${form.name}' saved`);
      qc.invalidateQueries({ queryKey: ["risk"] });
      onClose();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      open={!!profile}
      onOpenChange={(o) => !o && onClose()}
      title={profile === "new" ? "New risk profile" : `Edit '${profile?.name ?? ""}'`}
      testId="profile-dialog"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} onClick={save} icon={<Save className="size-4" />} data-testid="save-profile">
            Save
          </Button>
        </>
      }
    >
      <div className="grid grid-cols-2 gap-3">
        {err && (
          <div className="col-span-2">
            <ErrorBox error={new Error(err)} />
          </div>
        )}
        <Field label="Name" className="col-span-2">
          <input className="field" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} data-testid="profile-name" />
        </Field>
        <Field label="Risk per trade (% equity)">
          <input type="number" step={0.05} className="field num" value={form.risk_pct} onChange={num("risk_pct")} data-testid="profile-risk" />
        </Field>
        <Field label="Max spread (points)">
          <input type="number" className="field num" value={form.max_spread_points} onChange={num("max_spread_points")} />
        </Field>
        <div className="col-span-2 mt-1">
          <Switch checked={form.breakeven} onChange={(v) => setForm({ ...form, breakeven: v })} label="Move SL to breakeven" />
        </div>
        <Field label="…once profit reaches (R)">
          <input type="number" step={0.1} className="field num" value={form.breakeven_at_r} onChange={num("breakeven_at_r")} disabled={!form.breakeven} />
        </Field>
        <div />
        <div className="col-span-2 mt-1">
          <Switch checked={form.trailing} onChange={(v) => setForm({ ...form, trailing: v })} label="ATR trailing stop" />
        </div>
        <Field label="Trail distance (× ATR)">
          <input type="number" step={0.1} className="field num" value={form.trailing_atr} onChange={num("trailing_atr")} disabled={!form.trailing} />
        </Field>
        <Field label="Start after (R)">
          <input type="number" step={0.1} className="field num" value={form.trailing_start_r} onChange={num("trailing_start_r")} disabled={!form.trailing} />
        </Field>
        <div className="col-span-2 mt-1">
          <Switch
            checked={form.allow_min_lot}
            onChange={(v) => setForm({ ...form, allow_min_lot: v })}
            label="Allow the minimum lot when risk % buys less"
            testId="profile-min-lot"
          />
        </div>
        <Field label="…if its risk is at most (% equity)" hint="The journal shows the real risk taken">
          <input type="number" step={0.1} className="field num" value={form.min_lot_max_risk_pct} onChange={num("min_lot_max_risk_pct")} disabled={!form.allow_min_lot} data-testid="profile-min-lot-pct" />
        </Field>
      </div>
    </Dialog>
  );
}

export default function Risk() {
  const qc = useQueryClient();
  const { snapshot } = useLive();
  const q = useQuery({ queryKey: ["risk"], queryFn: api.risk, refetchInterval: 3000 });
  const [limits, setLimits] = useState<{ max_positions_global: number; daily_loss_pct_global: number } | null>(null);
  const [editing, setEditing] = useState<RiskProfile | "new" | null>(null);
  const [deleting, setDeleting] = useState<RiskProfile | null>(null);
  const [killOpen, setKillOpen] = useState(false);
  useEffect(() => {
    if (q.data && !limits) setLimits(q.data.limits);
  }, [q.data, limits]);

  if (q.isLoading || !limits) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const r = q.data!;
  const ex = r.exposure;
  const lossUsed = ex.daily_loss_cap < 0 ? Math.max(0, -ex.day_pnl) : 0;
  const lossCap = -ex.daily_loss_cap;
  const ccy = snapshot?.account?.currency ?? "";

  return (
    <div className="space-y-5">
      <PageHeader title="Risk" subtitle="Every Signal passes the Risk Gate before it becomes an order. Limits here apply across all Sessions." />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Kpi testId="risk-day-pnl" label="Today P&L (owned)" value={signed(ex.day_pnl)} tone={pnlClass(ex.day_pnl)} sub={`start equity ${money(ex.day_start_equity)} ${ccy}`} />
        <Kpi label="Floating" value={signed(ex.floating)} tone={pnlClass(ex.floating)} />
        <Kpi label="Open risk at stops" value={money(ex.risk_at_stop)} sub="loss if every SL is hit" />
        <Kpi label="Open positions" value={`${ex.open_positions} / ${r.limits.max_positions_global}`} />
      </div>

      <div className="grid gap-5 xl:grid-cols-3">
        <Card title="Global limits" testId="global-limits" className="xl:col-span-2">
          <div className="space-y-5">
            <div>
              <div className="mb-1.5 flex justify-between text-xs text-dim">
                <span>Daily loss used</span>
                <span className="num">
                  {money(lossUsed)} / {money(lossCap)} {ccy}
                </span>
              </div>
              <ProgressBar value={lossUsed} max={lossCap} tone={lossUsed / (lossCap || 1) > 0.7 ? "down" : "warn"} testId="loss-bar" />
            </div>
            <div>
              <div className="mb-1.5 flex justify-between text-xs text-dim">
                <span>Positions</span>
                <span className="num">
                  {ex.open_positions} / {r.limits.max_positions_global}
                </span>
              </div>
              <ProgressBar value={ex.open_positions} max={r.limits.max_positions_global} />
            </div>
            <div className="grid gap-3 md:grid-cols-3">
              <Field label="Max open positions (all sessions)">
                <input type="number" min={1} className="field num" value={limits.max_positions_global} onChange={(e) => setLimits({ ...limits, max_positions_global: +e.target.value })} data-testid="limit-max-positions" />
              </Field>
              <Field label="Daily loss limit (% equity)" hint="Hit ⇒ every Session auto-stops">
                <input type="number" step={0.1} min={0.1} className="field num" value={limits.daily_loss_pct_global} onChange={(e) => setLimits({ ...limits, daily_loss_pct_global: +e.target.value })} data-testid="limit-daily-loss" />
              </Field>
              <div className="flex items-start pt-5">
                <Button
                  variant="primary"
                  icon={<Save className="size-4" />}
                  data-testid="save-limits"
                  onClick={async () => {
                    try {
                      await api.setLimits(limits);
                      toast.success("Global limits saved");
                      qc.invalidateQueries({ queryKey: ["risk"] });
                    } catch (e) {
                      toast.error((e as Error).message);
                    }
                  }}
                >
                  Save limits
                </Button>
              </div>
            </div>
          </div>
        </Card>
        <Card title="Kill switch" testId="kill-card">
          <p className="text-sm text-dim">Stops every Session and closes every position carrying a FXCommand magic number. Foreign positions are never touched.</p>
          {r.kill_switch_at && <p className="mt-2 text-xs text-down">Last used {serverTime(r.kill_switch_at)}</p>}
          <Button className="mt-4 w-full" variant="danger" icon={<Octagon className="size-4" />} onClick={() => setKillOpen(true)} data-testid="risk-kill-switch">
            Activate kill switch
          </Button>
        </Card>
      </div>

      <div className="grid gap-5 xl:grid-cols-2">
        <LiveCapsCard r={r} />
        <EquityFloorCard r={r} />
      </div>

      <Card
        title="Risk profiles"
        testId="risk-profiles"
        bodyClass="p-0"
        actions={
          <Button size="sm" icon={<Plus className="size-3.5" />} onClick={() => setEditing("new")} data-testid="new-profile">
            New profile
          </Button>
        }
      >
        <div className="overflow-x-auto">
        <table className="w-full">
          <thead className="border-b border-line">
            <tr>
              <th className="th">Name</th>
              <th className="th text-right">Risk / trade</th>
              <th className="th text-right">Max spread</th>
              <th className="th">Breakeven</th>
              <th className="th">Trailing</th>
              <th className="th">Min lot</th>
              <th className="th text-right">Used by</th>
              <th className="th" />
            </tr>
          </thead>
          <tbody className="divide-y divide-line/60">
            {r.profiles.map((p) => (
              <tr key={p.id} className="hover:bg-panel-2/50" data-testid="profile-row">
                <td className="td font-medium">{p.name}</td>
                <td className="td num text-right">{p.risk_pct}%</td>
                <td className="td num text-right">{p.max_spread_points} pts</td>
                <td className="td">{p.breakeven ? <Badge tone="up">at {p.breakeven_at_r}R</Badge> : <span className="text-faint">off</span>}</td>
                <td className="td">{p.trailing ? <Badge tone="up">{p.trailing_atr}×ATR after {p.trailing_start_r}R</Badge> : <span className="text-faint">off</span>}</td>
                <td className="td">{p.allow_min_lot ? <Badge tone="warn">≤ {p.min_lot_max_risk_pct}%</Badge> : <span className="text-faint">off</span>}</td>
                <td className="td num text-right">{p.used_by}</td>
                <td className="td text-right">
                  <Button size="sm" variant="ghost" onClick={() => setEditing(p)} aria-label="Edit">
                    <Pencil className="size-3.5" />
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setDeleting(p)} disabled={!!p.used_by} aria-label="Delete">
                    <Trash2 className="size-3.5" />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </Card>

      <ProfileDialog profile={editing} onClose={() => setEditing(null)} />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete risk profile '${deleting?.name}'?`}
        confirmLabel="Delete"
        onConfirm={async () => {
          await api.deleteProfile(deleting!.id);
          qc.invalidateQueries({ queryKey: ["risk"] });
        }}
      />
      <ConfirmDialog
        open={killOpen}
        onOpenChange={setKillOpen}
        title="Activate kill switch?"
        confirmLabel="Stop everything & close positions"
        description="Stops every Session and closes every FXCommand position at market."
        onConfirm={async () => {
          const res = await api.killSwitch();
          toast.error(`Kill switch: stopped ${res.stopped_sessions.length} sessions, closed ${res.closed_positions} positions`);
          qc.invalidateQueries();
        }}
      />
    </div>
  );
}
