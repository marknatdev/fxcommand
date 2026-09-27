import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, Play } from "lucide-react";
import { toast } from "sonner";
import { api } from "../api/client";
import type { CostCheckRow, Evidence, EvidenceBadge as Badge_, EvidenceRequest } from "../api/types";
import { cn, serverTime } from "../lib/format";
import { Badge, Button } from "./ui";

export const r2 = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(digits)}R`);

/** "+0.07R · 149 trades" — an Evidence result in one line. */
export function evidenceLine(e: Evidence) {
  if (e.status === "failed") return `failed: ${e.note}`;
  return `${r2(e.mean_r, 3)} · ${e.trades} trades${e.status === "incomplete" ? " (incomplete)" : ""}`;
}

export function EvidencePeriods({ e }: { e: Evidence }) {
  if (!e.periods?.length) return null;
  return (
    <span className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-dim">
      {e.periods.map((p) => (
        <span key={p.label} className="whitespace-nowrap">
          {p.label}: <span className={cn("num", p.mean > 0 ? "text-up" : p.mean < 0 ? "text-down" : "")}>{r2(p.mean, 3)}</span>
          <span className="text-faint"> ({p.n})</span>
        </span>
      ))}
    </span>
  );
}

/** The Evidence badge for one Assignment's settings: match / running / other settings / none. */
export function EvidenceBadgeView({ badge, testId }: { badge: Badge_ | undefined; testId?: string }) {
  if (!badge) return <Badge testId={testId}>evidence…</Badge>;
  if (badge.status === "running") return <Badge tone="info" testId={testId}>evidence running…</Badge>;
  if (badge.status === "none") return <Badge testId={testId}>no evidence</Badge>;
  if (badge.status === "mismatch") return <Badge tone="warn" testId={testId}>evidence does not match these settings</Badge>;
  const e = badge.evidence!;
  return (
    <Badge tone={e.status === "failed" ? "down" : e.mean_r > 0 ? "up" : "down"} testId={testId}>
      {evidenceLine(e)}
    </Badge>
  );
}

/** Loads the badge for settings (saved or not) and offers to run the Evidence when it is missing. */
export function EvidenceFor({ req, testId, weekendWarning }: { req: EvidenceRequest | null; testId?: string; weekendWarning?: boolean }) {
  const qc = useQueryClient();
  const key = ["evidence-match", req];
  const q = useQuery({
    queryKey: key,
    queryFn: () => api.evidenceMatch(req!),
    enabled: !!req?.symbol,
    refetchInterval: (query) => (query.state.data?.status === "running" ? 3000 : false),
  });
  const run = useMutation({
    mutationFn: (body: EvidenceRequest) => api.runEvidence(body),
    onSuccess: () => {
      toast.success("Evidence Run queued");
      qc.invalidateQueries({ queryKey: ["evidence-match"] });
      qc.invalidateQueries({ queryKey: ["evidence"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });
  if (!req?.symbol) return null;
  const b = q.data;
  const other = b?.without_weekend_close;
  return (
    <div className="flex flex-col gap-1" data-testid={testId}>
      <div className="flex flex-wrap items-center gap-2">
        <FlaskConical className="size-3.5 text-faint" />
        <EvidenceBadgeView badge={b} testId="evidence-badge" />
        {b && b.status !== "match" && b.status !== "running" && (
          <Button size="sm" variant="ghost" icon={<Play className="size-3" />} loading={run.isPending} onClick={() => run.mutate(req)} data-testid="run-evidence">
            Run evidence
          </Button>
        )}
      </div>
      {b?.status === "match" && b.evidence && <EvidencePeriods e={b.evidence} />}
      {weekendWarning && (
        <p className="text-xs text-warn" data-testid="weekend-evidence">
          Weekend Close cuts this trend edge.{" "}
          {other?.status === "match" && other.evidence ? (
            <>
              Without it: <span className="num">{evidenceLine(other.evidence)}</span>
            </>
          ) : (
            <>
              No Evidence without Weekend Close yet{" "}
              <button className="text-accent hover:underline" onClick={() => run.mutate({ ...req, weekend_close: false })} data-testid="run-evidence-without-weekend">
                run it
              </button>
              .
            </>
          )}
        </p>
      )}
    </div>
  );
}

/** One Assignment's Cost Check: cost in R of the typical stop, swap beside it, and whether it may start. */
export function CostCheckLine({ row, testId }: { row: CostCheckRow | undefined; testId?: string }) {
  if (!row) return <span className="text-xs text-faint">cost check…</span>;
  if (row.error) return <span className="text-xs text-warn" data-testid={testId}>cost check: {row.error}</span>;
  const tone = row.blocked ? (row.override ? "warn" : "down") : "up";
  return (
    <span className="flex flex-wrap items-center gap-2 text-xs" data-testid={testId} data-blocked={row.blocked ? "true" : "false"}>
      <Badge tone={tone}>
        cost {Number.isFinite(row.cost_r) ? `${row.cost_r!.toFixed(2)}R` : "—"} {row.blocked ? (row.override ? "· overridden" : "· blocked") : "· ok"}
      </Badge>
      {row.swap_r ? <span className="text-dim">swap {r2(row.swap_r)} per trade (shown, not blocking)</span> : null}
      {row.blocked && <span className="text-dim">{row.reason}</span>}
    </span>
  );
}

export function evidenceMeta(e: Evidence) {
  return `${e.bars.toLocaleString()} bars ${serverTime(e.first_ts, true).slice(0, 10)} → ${serverTime(e.last_ts, true).slice(0, 10)}`;
}
