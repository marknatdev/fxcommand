import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Brain, Check, ChevronDown, ChevronRight, FlaskConical, Play, RotateCcw, Trophy, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { toast } from "sonner";
import { api } from "../api/client";
import type { ArenaT, ChallengerT, FilterT, RStatsT } from "../api/types";
import { Badge, Button, Card, ConfirmDialog, Empty, ErrorBox, Loading, PageHeader, StatusBadge, Switch } from "../components/ui";
import { cn, shortTime } from "../lib/format";

const COLORS = ["#22d3ee", "#a78bfa", "#f59e0b", "#22c55e", "#f43f5e"];

const r2 = (v: number | undefined | null) => (v === undefined || v === null || Number.isNaN(v) ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}`);

function StatCells({ s }: { s: Partial<RStatsT> | undefined }) {
  return (
    <>
      <td className="td num text-right">{s?.n ?? 0}</td>
      <td className={cn("td num text-right", (s?.mean ?? 0) > 0 ? "text-up" : (s?.mean ?? 0) < 0 ? "text-down" : "text-dim")}>{s?.n ? `${r2(s.mean)}R` : "—"}</td>
      <td className="td num text-right">{s?.n ? r2(s.sqn) : "—"}</td>
      <td className="td num text-right text-dim">{s?.n ? `${(s.max_dd ?? 0).toFixed(1)}R` : "—"}</td>
    </>
  );
}

const filterTone: Record<FilterT["mode"], "neutral" | "info" | "up" | "down"> = { observe: "neutral", no_edge: "info", active: "up", disabled: "down" };

export function FilterBadge({ f }: { f: FilterT }) {
  return (
    <span className="flex min-w-0 max-w-full items-center gap-2 text-xs text-dim" data-testid="filter-status">
      <Badge tone={filterTone[f.mode]} className="shrink-0">
        Signal Filter: {f.mode.replace("_", " ")}
      </Badge>
      <span className="min-w-0 truncate" title={f.note}>
        {f.note}
      </span>
    </span>
  );
}

function Guardrails({ ch }: { ch: ChallengerT }) {
  return (
    <div className="flex w-[250px] flex-wrap gap-1 whitespace-normal" data-testid="guardrails">
      {ch.checks.map((c) => (
        <span
          key={c.name}
          title={c.detail}
          className={cn(
            "inline-flex items-center gap-0.5 rounded border px-1.5 py-0.5 text-[10px] font-medium",
            c.ok ? "border-up/30 bg-up/10 text-up" : "border-line-2 bg-line/40 text-faint",
          )}
        >
          {c.ok ? <Check className="size-3" /> : <X className="size-3" />}
          {c.name.replace("_", " ")}
        </span>
      ))}
    </div>
  );
}

function PromoteDialog({ arena, ch, onClose }: { arena: ArenaT; ch: ChallengerT | null; onClose: () => void }) {
  const qc = useQueryClient();
  const [force, setForce] = useState(false);
  if (!ch) return null;
  return (
    <ConfirmDialog
      open={!!ch}
      onOpenChange={(o) => {
        if (!o) {
          setForce(false);
          onClose();
        }
      }}
      testId="promote-dialog"
      variant={ch.promotable ? "success" : "danger"}
      title={`Promote to Champion of ${arena.symbol} ${arena.timeframe}?`}
      confirmLabel={ch.promotable ? "Promote (when flat)" : "Promote anyway"}
      description={`${ch.candidate.label} replaces ${arena.champion.label} in '${arena.session.name}' at the first bar close where it holds no position. The current Champion keeps shadow-trading so you can roll back.`}
      confirmDisabled={!ch.promotable && !force}
      onConfirm={async () => {
        try {
          await api.promote(ch.id, arena.session.id, !ch.promotable);
          toast.success("Promotion queued — applies when the assignment is flat");
          qc.invalidateQueries({ queryKey: ["learning"] });
        } catch (e) {
          toast.error((e as Error).message);
          throw e;
        }
      }}
    >
      <ul className="space-y-1.5 text-sm">
        {ch.checks.map((c) => (
          <li key={c.name} className="flex items-start gap-2">
            {c.ok ? <Check className="mt-0.5 size-4 shrink-0 text-up" /> : <X className="mt-0.5 size-4 shrink-0 text-down" />}
            <span className={c.ok ? "text-dim" : "text-ink"}>{c.detail}</span>
          </li>
        ))}
      </ul>
      {!ch.promotable && (
        <div className="mt-4 rounded-lg border border-down/40 bg-down/10 p-3">
          <Switch checked={force} onChange={setForce} label="Override: promote without passing every Guardrail" testId="promote-override" />
        </div>
      )}
    </ConfirmDialog>
  );
}

function ArenaDetails({ arena }: { arena: ArenaT }) {
  const q = useQuery({ queryKey: ["arena", arena.symbol, arena.timeframe], queryFn: () => api.arena(arena.symbol, arena.timeframe), refetchInterval: 10_000 });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const d = q.data!;
  const keys = [d.champion.key, ...d.challengers.map((c) => c.candidate.key)];
  const labels: Record<string, string> = { [d.champion.key]: `Champion · ${d.champion.label}` };
  d.challengers.forEach((c) => (labels[c.candidate.key] = c.candidate.label));
  // merge curves onto one time axis
  const byTs = new Map<number, Record<string, number>>();
  keys.forEach((k) => (d.curves[k] ?? []).forEach((p) => byTs.set(p.ts, { ...(byTs.get(p.ts) ?? {}), [k]: p.r })));
  const rows = [...byTs.entries()].sort((a, b) => a[0] - b[0]).map(([ts, v]) => ({ ts, ...v }));
  return (
    <div className="space-y-4 border-t border-line pt-4" data-testid="arena-details">
      <div>
        <div className="label">Shadow Trading — cumulative R</div>
        {rows.length > 1 ? (
          <div style={{ height: 240 }} data-testid="shadow-curves">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={rows} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid stroke="#16203a" vertical={false} />
                <XAxis dataKey="ts" tickFormatter={(v) => shortTime(v)} stroke="#8b98b0" fontSize={10} minTickGap={40} />
                <YAxis stroke="#8b98b0" fontSize={10} width={40} />
                <Tooltip contentStyle={{ background: "#111a2c", border: "1px solid #26334f", fontSize: 12 }} labelFormatter={(v) => shortTime(v as number)} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                {keys.map((k, i) => (
                  <Line key={k} type="stepAfter" dataKey={k} name={labels[k]} stroke={COLORS[i % COLORS.length]} dot={false} strokeWidth={i === 0 ? 2.5 : 1.5} connectNulls isAnimationActive={false} />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <Empty>Shadow Trades appear here as bars close.</Empty>
        )}
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <div>
          <div className="label">Champion versions</div>
          <table className="w-full text-sm" data-testid="version-history">
            <tbody className="divide-y divide-line/60">
              {d.history.map((v) => (
                <tr key={v.id}>
                  <td className="py-1.5 num text-dim">v{v.version}</td>
                  <td className="py-1.5">
                    <Badge tone={v.kind.includes("rollback") ? "warn" : v.kind.includes("promotion") ? "up" : "neutral"}>{v.kind.replace("_", " ")}</Badge>
                  </td>
                  <td className="py-1.5">{v.label}</td>
                  <td className="py-1.5 num text-right text-faint">{shortTime(v.server_ts)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div>
          <div className="label">Optimizer Runs</div>
          <table className="w-full text-sm" data-testid="runs-table">
            <tbody className="divide-y divide-line/60">
              {d.runs.map((r) => (
                <tr key={r.id} title={r.note}>
                  <td className="py-1.5 num text-dim">#{r.id}</td>
                  <td className="py-1.5">
                    <Badge tone={r.status === "done" ? "up" : r.status === "failed" ? "down" : r.status === "insufficient" ? "warn" : "info"}>{r.status}</Badge>
                  </td>
                  <td className="py-1.5 text-dim">{r.trigger.replace("_", " ")}</td>
                  <td className="max-w-[320px] truncate py-1.5 text-xs text-faint">{r.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      {!!d.runs[0]?.result?.finalists?.length && (
        <div>
          <div className="label">Last run — finalists (ranked in-sample, judged out-of-sample)</div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm" data-testid="finalists">
              <thead>
                <tr className="text-[11px] uppercase text-faint">
                  <th className="py-1 text-left">Candidate</th>
                  <th className="py-1 text-right">In-sample SQN</th>
                  <th className="py-1 text-right">Out-of-sample SQN</th>
                  <th className="py-1 text-right">OOS trades</th>
                  <th className="py-1 text-right">Folds +</th>
                  <th className="py-1 text-right">Robust</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line/60">
                {d.runs[0].result!.finalists!.map((f) => (
                  <tr key={f.candidate.key}>
                    <td className="max-w-[340px] truncate py-1.5" title={f.candidate.label}>
                      {f.candidate.label}
                    </td>
                    <td className="py-1.5 num text-right">{r2(f.walk_forward.in_sample.sqn)}</td>
                    <td className={cn("py-1.5 num text-right", f.walk_forward.oos.sqn > 0 ? "text-up" : "text-down")}>{r2(f.walk_forward.oos.sqn)}</td>
                    <td className="py-1.5 num text-right">{f.walk_forward.oos.n}</td>
                    <td className="py-1.5 num text-right">{f.walk_forward.positive_folds}/3</td>
                    <td className="py-1.5 text-right">{f.robust ? <Check className="ml-auto size-3.5 text-up" /> : <X className="ml-auto size-3.5 text-faint" />}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-1 text-xs text-faint">
            Champion out-of-sample SQN {r2(d.runs[0].result!.champion?.walk_forward.oos.sqn)}. A finalist becomes a Challenger only with a positive, robust
            out-of-sample edge in at least 2 of 3 folds.
          </p>
        </div>
      )}
      {!!d.signals.length && (
        <div>
          <div className="label">Recent live Signals (filter decisions)</div>
          <div className="flex flex-wrap gap-1.5">
            {d.signals.slice(0, 24).map((s) => (
              <span
                key={s.id}
                title={`${shortTime(s.ts)} ${s.side} P(win) ${s.p_win?.toFixed(2) ?? "—"} (${s.filter_mode})${s.r !== null ? ` → ${r2(s.r)}R` : ""}`}
                className={cn(
                  "rounded border px-1.5 py-0.5 text-[10px] num",
                  s.decision === "blocked" ? "border-warn/40 text-warn" : s.decision === "rejected" ? "border-line-2 text-faint" : (s.r ?? 0) > 0 ? "border-up/40 text-up" : "border-line-2 text-dim",
                )}
              >
                {s.side[0].toUpperCase()} {s.p_win !== null ? s.p_win.toFixed(2) : "—"} {s.decision === "blocked" ? "blocked" : s.r !== null ? `${r2(s.r)}R` : ""}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function ArenaCard({ arena, liveAccount }: { arena: ArenaT; liveAccount: boolean }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [promoting, setPromoting] = useState<ChallengerT | null>(null);
  const [rollbackOpen, setRollbackOpen] = useState(false);
  const refresh = () => ["learning", "arena"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
  const run = arena.last_run;
  return (
    <Card
      testId={`arena-${arena.symbol}-${arena.timeframe}`}
      title={
        <span className="flex flex-wrap items-center gap-2">
          <FlaskConical className="size-4 text-accent" />
          {arena.symbol} · {arena.timeframe}
          <Link to={`/sessions/${arena.session.id}`} className="text-xs font-normal text-accent hover:underline">
            {arena.session.name}
          </Link>
          <StatusBadge status={arena.session.status} />
        </span>
      }
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Switch
            checked={arena.auto_promote}
            disabled={!arena.auto_promote_allowed}
            testId="auto-promote"
            label={<span className="text-xs text-dim">Auto-promote{!arena.auto_promote_allowed && " (never on live)"}</span>}
            onChange={async (v) => {
              try {
                await api.setAutoPromote(arena.session.id, arena.symbol, v);
                refresh();
              } catch (e) {
                toast.error((e as Error).message);
              }
            }}
          />
          <Button
            size="sm"
            icon={<Play className="size-3.5" />}
            data-testid="optimize"
            onClick={async () => {
              const r = await api.optimize(arena.symbol, arena.timeframe);
              toast.success(`Optimizer Run #${r.run_id} queued for ${arena.symbol} ${arena.timeframe}`);
              refresh();
            }}
          >
            Optimize
          </Button>
          <Button size="sm" variant="ghost" icon={<RotateCcw className="size-3.5" />} disabled={arena.versions < 2} onClick={() => setRollbackOpen(true)} data-testid="rollback">
            Rollback
          </Button>
        </div>
      }
      bodyClass="space-y-4"
    >
      {arena.pending && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn" data-testid="pending-change">
          <span>
            Pending {arena.pending.kind.replace("_", " ")} — applies at the next bar close where {arena.symbol} is flat. {arena.pending.reason}
          </span>
          <Button
            size="sm"
            variant="ghost"
            onClick={async () => {
              await api.cancelPending(arena.pending!.id);
              refresh();
            }}
          >
            Cancel
          </Button>
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full" data-testid="leaderboard">
          <thead className="border-b border-line">
            <tr>
              <th className="th">Candidate</th>
              <th className="th text-right">Shadow trades</th>
              <th className="th text-right">Mean</th>
              <th className="th text-right">SQN</th>
              <th className="th text-right">Max DD</th>
              <th className="th text-right">Walk-fwd SQN</th>
              <th className="th">Guardrails</th>
              <th className="th" />
            </tr>
          </thead>
          <tbody className="divide-y divide-line/60">
            <tr className="bg-accent/5" data-testid="champion-row">
              <td className="td">
                <div className="flex items-center gap-1.5 font-medium">
                  <Trophy className="size-3.5 text-warn" /> {arena.champion.label}
                </div>
                <div className="text-[11px] text-faint">Champion · live since v{arena.versions}</div>
              </td>
              <StatCells s={arena.champion.shadow} />
              <td className="td num text-right text-faint">—</td>
              <td className="td" />
              <td className="td" />
            </tr>
            {arena.challengers.map((c) => (
              <tr key={c.id} data-testid="challenger-row">
                <td className="td">
                  <div className="max-w-[320px] truncate font-medium" title={c.candidate.label}>
                    {c.candidate.label}
                  </div>
                  <div className="text-[11px] text-faint">
                    {c.note || "challenger"} · since {shortTime(c.started_ts)} · champion {r2(c.champion_shadow.mean)}R on the same bars
                  </div>
                </td>
                <StatCells s={c.shadow} />
                <td className="td num text-right">{c.oos?.n ? r2(c.oos.sqn) : "—"}</td>
                <td className="td">
                  <Guardrails ch={c} />
                </td>
                <td className="td text-right">
                  <Button size="sm" variant={c.promotable ? "success" : "secondary"} onClick={() => setPromoting(c)} data-testid="promote">
                    Promote
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!arena.challengers.length && (
          <p className="px-3 py-3 text-sm text-faint" data-testid="no-challengers">
            No Challengers yet — an Optimizer Run looks for Candidates whose edge survives out-of-sample and small parameter changes. On a random market it may rightly find none.
          </p>
        )}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <FilterBadge f={arena.filter} />
        <span className="text-xs text-faint" data-testid="last-run">
          {run ? (
            <>
              Last Optimizer Run #{run.id}: <b className="text-dim">{run.status}</b> ({run.trigger.replace("_", " ")}) {run.note && `— ${run.note}`}
            </>
          ) : (
            "No Optimizer Run yet"
          )}
        </span>
      </div>
      <button className="flex items-center gap-1 text-xs text-accent hover:underline" onClick={() => setOpen(!open)} data-testid="arena-details-toggle">
        {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />} Curves, history & runs
      </button>
      {open && <ArenaDetails arena={arena} />}
      <PromoteDialog arena={arena} ch={promoting} onClose={() => setPromoting(null)} />
      <ConfirmDialog
        open={rollbackOpen}
        onOpenChange={setRollbackOpen}
        testId="rollback-dialog"
        variant="warn"
        title={`Roll back ${arena.symbol} ${arena.timeframe}?`}
        description="The previous Champion Version takes over at the next bar close where the assignment is flat."
        confirmLabel="Roll back"
        onConfirm={async () => {
          try {
            await api.rollback(arena.session.id, arena.symbol);
            toast.success("Rollback queued");
            refresh();
          } catch (e) {
            toast.error((e as Error).message);
            throw e;
          }
        }}
      />
      {liveAccount && <p className="text-xs text-faint">Live account: Promotions always need you. Guardrails still apply.</p>}
    </Card>
  );
}

export default function Learning() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["learning"], queryFn: api.learning, refetchInterval: 4000 });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} />;
  const o = q.data!;
  return (
    <div className="space-y-5">
      <PageHeader
        title="Learning"
        subtitle="Strategies improve by competition: Challengers shadow-trade the same bars as each Champion and replace it only when they prove better under the Guardrails."
        actions={
          <>
            {!o.enabled && <Badge tone="warn">learning disabled in Settings</Badge>}
            {o.queue.length > 0 && <Badge tone="info" testId="learning-queue">{o.queue.length} optimizer run(s) queued</Badge>}
            <Button
              variant="primary"
              icon={<Brain className="size-4" />}
              data-testid="learn-now"
              onClick={async () => {
                const r = await api.learnNow();
                toast.success(r.queued.length ? `Queued ${r.queued.length} Optimizer Run(s)` : "No running sessions to learn from");
                qc.invalidateQueries({ queryKey: ["learning"] });
              }}
            >
              Learn now
            </Button>
          </>
        }
      />
      {o.live_account && (
        <div className="rounded-lg border border-down/40 bg-down/10 px-4 py-2 text-sm text-down">Live account: automatic Promotions are disabled. Promote manually after reviewing the Guardrails.</div>
      )}
      {o.arenas.length === 0 ? (
        <Card>
          <Empty>
            Learning starts when a Session trades. Every Symbol + Timeframe it trades becomes an Arena.
            <Link to="/sessions/new" className="text-accent hover:underline">
              Create a session
            </Link>
          </Empty>
        </Card>
      ) : (
        o.arenas.map((a) => <ArenaCard key={`${a.symbol}-${a.timeframe}`} arena={a} liveAccount={o.live_account} />)
      )}
    </div>
  );
}
