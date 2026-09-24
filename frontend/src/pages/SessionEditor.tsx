import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Plus, Save, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";
import { api } from "../api/client";
import { TIMEFRAMES, type AssignmentInput, type SessionInput, type StrategyInfo, type Timeframe } from "../api/types";
import { WindowEditor } from "../components/WindowEditor";
import { Button, Card, ConfirmDialog, ErrorBox, Field, Loading, PageHeader, Switch } from "../components/ui";

const DEFAULT_WINDOW = { enabled: true, open_day: 0, open_time: "00:10", close_day: 4, close_time: "23:00", blackouts: [{ start: "23:55", end: "00:10" }] };

function blankAssignment(symbol = "", strategies: StrategyInfo[] = []): AssignmentInput {
  const s = strategies[0];
  return {
    symbol,
    timeframe: "M15",
    strategy: s?.key ?? "ema_cross",
    params: s ? Object.fromEntries(s.params.map((p) => [p.name, p.default])) : {},
    risk_profile_id: null,
    reverse_on_opposite: true,
    enabled: true,
  };
}

function ParamsEditor({ strategy, params, onChange }: { strategy?: StrategyInfo; params: AssignmentInput["params"]; onChange: (p: AssignmentInput["params"]) => void }) {
  if (!strategy) return null;
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4" data-testid="params-editor">
      {strategy.params.map((p) =>
        p.type === "bool" ? (
          <div key={p.name} className="flex items-end pb-1.5">
            <Switch checked={Boolean(params[p.name] ?? p.default)} onChange={(v) => onChange({ ...params, [p.name]: v })} label={p.label} />
          </div>
        ) : (
          <Field key={p.name} label={p.label}>
            <input
              type="number"
              className="field num"
              data-testid={`param-${p.name}`}
              value={String(params[p.name] ?? p.default)}
              min={p.min ?? undefined}
              max={p.max ?? undefined}
              step={p.step ?? (p.type === "int" ? 1 : 0.1)}
              onChange={(e) => onChange({ ...params, [p.name]: e.target.value === "" ? (p.default as number) : Number(e.target.value) })}
            />
          </Field>
        ),
      )}
    </div>
  );
}

export default function SessionEditor() {
  const { id } = useParams();
  const editing = id !== undefined;
  const nav = useNavigate();
  const qc = useQueryClient();
  const symbols = useQuery({ queryKey: ["symbol-names"], queryFn: api.symbolNames, staleTime: 60_000 });
  const strategies = useQuery({ queryKey: ["strategies"], queryFn: api.strategies });
  const risk = useQuery({ queryKey: ["risk"], queryFn: api.risk });
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const existing = useQuery({ queryKey: ["session", Number(id)], queryFn: () => api.session(Number(id)), enabled: editing });

  const [form, setForm] = useState<SessionInput | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);

  useEffect(() => {
    if (form) return;
    if (editing && existing.data) {
      const s = existing.data;
      setForm({
        name: s.name,
        notes: s.notes,
        auto_resume: s.auto_resume,
        max_positions: s.max_positions,
        daily_loss_pct: s.daily_loss_pct,
        window: s.window,
        assignments: s.assignments.map((a) => ({
          symbol: a.symbol,
          timeframe: a.timeframe,
          strategy: a.strategy,
          params: a.params,
          risk_profile_id: a.risk_profile_id,
          reverse_on_opposite: a.reverse_on_opposite,
          enabled: a.enabled,
        })),
      });
    } else if (!editing && strategies.data && settings.data) {
      setForm({
        name: "",
        notes: "",
        auto_resume: false,
        max_positions: 5,
        daily_loss_pct: 3,
        window: settings.data.app.default_window ?? DEFAULT_WINDOW,
        assignments: [blankAssignment("", strategies.data)],
      });
      setOpen(0);
    }
  }, [editing, existing.data, strategies.data, settings.data, form]);

  if (!form || symbols.isLoading || strategies.isLoading || risk.isLoading) return <Loading />;
  const stratMap = new Map((strategies.data ?? []).map((s) => [s.key, s]));
  const used = new Set(form.assignments.map((a) => a.symbol));
  const known = new Set(symbols.data ?? []);
  const setA = (i: number, patch: Partial<AssignmentInput>) =>
    setForm({ ...form, assignments: form.assignments.map((a, j) => (j === i ? { ...a, ...patch } : a)) });

  const save = async () => {
    setError(null);
    if (!form.name.trim()) return setError("Give the session a name.");
    if (form.assignments.some((a) => !a.symbol)) return setError("Every assignment needs a symbol.");
    if (new Set(form.assignments.map((a) => a.symbol)).size !== form.assignments.length)
      return setError("A Symbol may appear only once per Session.");
    setSaving(true);
    try {
      const s = editing ? await api.updateSession(Number(id), form) : await api.createSession(form);
      toast.success(`Session '${s.name}' saved`);
      ["sessions", "session", "overview", "symbols"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
      nav(`/sessions/${s.id}`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const active = existing.data && (existing.data.status === "running" || existing.data.status === "paused");

  return (
    <div className="space-y-5">
      <PageHeader
        title={editing ? `Edit session` : "New session"}
        subtitle={editing ? `${existing.data?.name} · magic ${existing.data?.magic}` : "A magic number is allocated automatically and never reused."}
        actions={
          <>
            {editing && (
              <Button variant="danger" icon={<Trash2 className="size-4" />} onClick={() => setDeleteOpen(true)} disabled={!!active} data-testid="delete-session">
                Delete
              </Button>
            )}
            <Button variant="ghost" onClick={() => nav(-1)}>
              Cancel
            </Button>
            <Button variant="primary" icon={<Save className="size-4" />} loading={saving} onClick={save} disabled={!!active} data-testid="save-session">
              Save session
            </Button>
          </>
        }
      />
      {active && <ErrorBox error={new Error("This session is active. Stop it before editing.")} />}
      {error && (
        <div data-testid="form-error">
          <ErrorBox error={new Error(error)} />
        </div>
      )}

      <Card title="Session">
        <div className="grid gap-4 md:grid-cols-4">
          <Field label="Name" className="md:col-span-2">
            <input className="field" data-testid="session-name" value={form.name} placeholder="e.g. London majors" onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
          <Field label="Max open positions">
            <input type="number" min={1} max={100} className="field num" data-testid="session-max-positions" value={form.max_positions} onChange={(e) => setForm({ ...form, max_positions: +e.target.value })} />
          </Field>
          <Field label="Daily loss limit (% equity)">
            <input type="number" min={0.1} step={0.1} className="field num" data-testid="session-daily-loss" value={form.daily_loss_pct} onChange={(e) => setForm({ ...form, daily_loss_pct: +e.target.value })} />
          </Field>
          <Field label="Notes" className="md:col-span-3">
            <input className="field" value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
          </Field>
          <div className="flex items-end pb-1.5">
            <Switch checked={form.auto_resume} onChange={(v) => setForm({ ...form, auto_resume: v })} label="Auto-resume on app restart" testId="session-auto-resume" />
          </div>
        </div>
      </Card>

      <Card
        title={`Assignments (${form.assignments.length})`}
        actions={
          <Button
            size="sm"
            icon={<Plus className="size-3.5" />}
            data-testid="add-assignment"
            onClick={() => {
              const next = (symbols.data ?? []).find((s) => !used.has(s)) ?? "";
              setForm({ ...form, assignments: [...form.assignments, blankAssignment(next, strategies.data)] });
              setOpen(form.assignments.length);
            }}
          >
            Add symbol
          </Button>
        }
        bodyClass="space-y-3"
      >
        <p className="text-xs text-faint">A Symbol can appear only once per Session, and only one running Session can trade a given Symbol.</p>
        {form.assignments.map((a, i) => {
          const strat = stratMap.get(a.strategy);
          const expanded = open === i;
          return (
            <div key={i} className="rounded-lg border border-line-2 bg-bg/40" data-testid="assignment-row">
              <div className="grid items-end gap-3 p-3 md:grid-cols-[1.2fr_0.7fr_1.3fr_1.1fr_auto]">
                <Field label="Symbol">
<>
                    <input
                      className="field"
                      data-testid="assignment-symbol"
                      list="symbol-names"
                      placeholder="Type or pick…"
                      value={a.symbol}
                      onChange={(e) => setA(i, { symbol: e.target.value.trim() })}
                    />
                    {a.symbol && !known.has(a.symbol) && <p className="mt-1 text-xs text-warn">Not offered by the broker</p>}
                    {a.symbol && form.assignments.some((x, j) => j !== i && x.symbol === a.symbol) && (
                      <p className="mt-1 text-xs text-down">Already in this session</p>
                    )}
                  </>
                </Field>
                <Field label="Timeframe">
                  <select className="field" data-testid="assignment-timeframe" value={a.timeframe} onChange={(e) => setA(i, { timeframe: e.target.value as Timeframe })}>
                    {TIMEFRAMES.map((t) => (
                      <option key={t}>{t}</option>
                    ))}
                  </select>
                </Field>
                <Field label="Strategy">
                  <select
                    className="field"
                    data-testid="assignment-strategy"
                    value={a.strategy}
                    onChange={(e) => {
                      const s = stratMap.get(e.target.value);
                      setA(i, { strategy: e.target.value, params: s ? Object.fromEntries(s.params.map((p) => [p.name, p.default])) : {} });
                    }}
                  >
                    {(strategies.data ?? []).map((s) => (
                      <option key={s.key} value={s.key}>
                        {s.title}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Risk profile">
                  <select
                    className="field"
                    data-testid="assignment-risk"
                    value={a.risk_profile_id ?? risk.data?.profiles[0]?.id ?? ""}
                    onChange={(e) => setA(i, { risk_profile_id: Number(e.target.value) })}
                  >
                    {(risk.data?.profiles ?? []).map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name} ({p.risk_pct}%)
                      </option>
                    ))}
                  </select>
                </Field>
                <div className="flex items-center gap-1 pb-0.5">
                  <Button variant="ghost" size="sm" onClick={() => setOpen(expanded ? null : i)} data-testid="toggle-params">
                    {expanded ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />} Params
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label="Remove assignment"
                    disabled={form.assignments.length === 1}
                    onClick={() => setForm({ ...form, assignments: form.assignments.filter((_, j) => j !== i) })}
                  >
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
              </div>
              {expanded && (
                <div className="space-y-3 border-t border-line p-3">
                  <p className="text-xs text-dim">{strat?.description}</p>
                  <ParamsEditor strategy={strat} params={a.params} onChange={(p) => setA(i, { params: p })} />
                  <div className="flex flex-wrap gap-5">
                    <Switch checked={a.reverse_on_opposite} onChange={(v) => setA(i, { reverse_on_opposite: v })} label="Reverse on opposite signal (else close only)" />
                    <Switch checked={a.enabled} onChange={(v) => setA(i, { enabled: v })} label="Enabled" />
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </Card>

      <datalist id="symbol-names">
        {(symbols.data ?? []).filter((n) => !used.has(n)).map((n) => (
          <option key={n} value={n} />
        ))}
      </datalist>

      <Card title="Trading window">
        <WindowEditor value={form.window} onChange={(w) => setForm({ ...form, window: w })} />
      </Card>

      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title={`Delete session '${existing.data?.name}'?`}
        description="Configuration is removed. Its trades and journal stay in History. The magic number is never reused."
        confirmLabel="Delete session"
        onConfirm={async () => {
          try {
            await api.deleteSession(Number(id));
            toast.success("Session deleted");
            qc.invalidateQueries({ queryKey: ["sessions"] });
            nav("/sessions");
          } catch (e) {
            toast.error((e as Error).message);
            throw e;
          }
        }}
      />
    </div>
  );
}
