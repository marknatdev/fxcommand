import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Bug, FastForward, Save, Zap } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { api } from "../api/client";
import type { AppSettings } from "../api/types";
import { NotifyCard } from "../components/NotifyCard";
import { WindowEditor } from "../components/WindowEditor";
import { Button, Card, ErrorBox, Field, Loading, PageHeader, Switch } from "../components/ui";
import { serverTime } from "../lib/format";

function SimControls() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["sim"], queryFn: api.simState, refetchInterval: 2000 });
  const [bars, setBars] = useState(60);
  const [symbol, setSymbol] = useState("EURUSD");
  const [pct, setPct] = useState(-1);
  const [fault, setFault] = useState("requote");
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<unknown>, msg: string) => {
    setBusy(true);
    try {
      await fn();
      toast.success(msg);
      qc.invalidateQueries();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  if (!q.data) return null;
  return (
    <Card title="Simulator controls" testId="sim-controls">
      <p className="mb-4 text-sm text-dim">
        Simulated server time <span className="num text-ink" data-testid="sim-time">{serverTime(q.data.server_time)}</span> · {q.data.closed_trades} closed trades · balance{" "}
        <span className="num">{q.data.balance.toFixed(2)}</span>
      </p>
      <div className="grid gap-4 md:grid-cols-2">
        <div className="flex items-end gap-2">
          <Field label="Advance market (M1 bars)" className="flex-1">
            <input type="number" min={1} max={5000} className="field num" value={bars} onChange={(e) => setBars(+e.target.value)} data-testid="sim-bars" />
          </Field>
          <Button icon={<FastForward className="size-4" />} loading={busy} onClick={() => run(() => api.simAdvance(bars), `Advanced ${bars} bars`)} data-testid="sim-advance">
            Advance
          </Button>
        </div>
        <div className="flex items-end gap-2">
          <Field label="Price shock" className="flex-1">
            <div className="flex gap-2">
              <select className="field" value={symbol} onChange={(e) => setSymbol(e.target.value)} data-testid="sim-shock-symbol">
                {Object.keys(q.data.prices).map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </select>
              <input type="number" step={0.1} className="field num w-28" value={pct} onChange={(e) => setPct(+e.target.value)} data-testid="sim-shock-pct" />
              <span className="self-center text-dim">%</span>
            </div>
          </Field>
          <Button variant="warn" icon={<Zap className="size-4" />} loading={busy} onClick={() => run(() => api.simShock(symbol, pct / 100), `Shocked ${symbol} ${pct}%`)} data-testid="sim-shock">
            Shock
          </Button>
        </div>
        <div className="flex items-end gap-2">
          <Field label="Broker fault (applies to the next order / close)" className="flex-1">
            <select className="field" value={fault} onChange={(e) => setFault(e.target.value)} data-testid="sim-fault-kind">
              {["requote", "timeout_filled", "timeout_none", "partial", "price_zero", "close_fail", "close_timeout_done"].map((f) => (
                <option key={f}>{f}</option>
              ))}
            </select>
          </Field>
          <Button icon={<Bug className="size-4" />} loading={busy} onClick={() => run(() => api.simFault(fault), `Queued fault ${fault}`)} data-testid="sim-fault">
            Inject
          </Button>
        </div>
        <div className="flex flex-wrap items-end gap-3 text-sm">
          <Switch
            checked={!q.data.is_demo}
            onChange={(v) => run(() => api.simAccount({ is_demo: !v }), v ? "Simulated account is now LIVE" : "Simulated account is now demo")}
            label="Simulate a LIVE account"
            testId="sim-live"
          />
          <Button size="sm" variant="ghost" loading={busy} onClick={() => run(() => api.simAccount({ login: q.data!.login + 1 }), "Simulated an account switch")} data-testid="sim-switch-account">
            Switch account ({q.data.login})
          </Button>
        </div>
      </div>
      {q.data.faults.length > 0 && <p className="mt-3 text-xs text-warn" data-testid="sim-faults">Queued faults: {q.data.faults.join(", ")}</p>}
    </Card>
  );
}

export default function Settings() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const [form, setForm] = useState<AppSettings | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (q.data && !form) setForm(q.data.app);
  }, [q.data, form]);
  if (q.isLoading || !form) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const sim = q.data!.mode === "sim";
  const save = async () => {
    setSaving(true);
    try {
      const r = await api.saveSettings(form);
      setForm(r.app);
      toast.success("Settings saved");
      qc.invalidateQueries({ queryKey: ["settings"] });
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="space-y-5">
      <PageHeader
        title="Settings"
        subtitle="Application-wide configuration. Changes apply immediately to the running engine."
        actions={
          <Button variant="primary" icon={<Save className="size-4" />} loading={saving} onClick={save} data-testid="save-settings">
            Save settings
          </Button>
        }
      />
      <Card title="Engine">
        <div className="grid gap-4 md:grid-cols-3">
          <Field label="Engine poll interval (seconds)" hint="How often the engine checks for new closed bars and manages positions.">
            <input type="number" step={0.1} min={0.2} className="field num" value={form.poll_interval} onChange={(e) => setForm({ ...form, poll_interval: +e.target.value })} data-testid="setting-poll" />
          </Field>
          <Field label="Equity snapshot every (server seconds)" hint="Spacing of points on the equity curve.">
            <input type="number" min={60} className="field num" value={form.equity_snapshot_seconds} onChange={(e) => setForm({ ...form, equity_snapshot_seconds: +e.target.value })} />
          </Field>
          <div className="flex items-end pb-1.5">
            <Switch checked={form.close_on_auto_stop} onChange={(v) => setForm({ ...form, close_on_auto_stop: v })} label="Close positions when a daily-loss limit auto-stops a Session" testId="setting-close-on-auto-stop" />
          </div>
        </div>
      </Card>
      <NotifyCard notify={q.data!.notify} />
      <Card title="Learning">
        <div className="grid gap-4 md:grid-cols-3">
          <div className="flex items-end pb-1.5">
            <Switch checked={form.learning_enabled} onChange={(v) => setForm({ ...form, learning_enabled: v })} label="Shadow Trading, Signal Filter and Optimizer Runs" testId="setting-learning" />
          </div>
          <Field label="Candidates per Optimizer Run" hint="More finds more, costs CPU (≈ 30 ms each on 5,000 bars).">
            <input type="number" min={10} max={2000} className="field num" value={form.learning_candidates} onChange={(e) => setForm({ ...form, learning_candidates: +e.target.value })} data-testid="setting-learning-candidates" />
          </Field>
          <Field label="History bars per Optimizer Run" hint="Walk-forward uses the first 60% to rank, the last 40% to judge.">
            <input type="number" min={500} max={50000} className="field num" value={form.learning_bars} onChange={(e) => setForm({ ...form, learning_bars: +e.target.value })} />
          </Field>
        </div>
      </Card>
      {sim && (
        <Card title="Simulated market">
          <Field label="Clock speed (simulated minutes per real minute, 0 = paused)" hint="60 ⇒ one M1 bar per second." className="max-w-md">
            <input type="number" min={0} className="field num" value={form.sim_speed} onChange={(e) => setForm({ ...form, sim_speed: +e.target.value })} data-testid="setting-sim-speed" />
          </Field>
        </Card>
      )}
      <Card title="Default trading window for new sessions">
        <WindowEditor value={form.default_window} onChange={(w) => setForm({ ...form, default_window: w })} />
      </Card>
      {sim && <SimControls />}
    </div>
  );
}
