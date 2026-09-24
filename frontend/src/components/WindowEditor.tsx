import { Plus, Trash2 } from "lucide-react";
import type { TradingWindow } from "../api/types";
import { DAYS } from "../lib/format";
import { Button, Field, Switch } from "./ui";

export function WindowEditor({ value, onChange }: { value: TradingWindow; onChange: (w: TradingWindow) => void }) {
  const set = (patch: Partial<TradingWindow>) => onChange({ ...value, ...patch });
  return (
    <div className="space-y-3" data-testid="window-editor">
      <Switch checked={value.enabled} onChange={(v) => set({ enabled: v })} label="Restrict new entries to a trading window (server time)" testId="window-enabled" />
      {value.enabled && (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Field label="Opens (day)">
              <select className="field" value={value.open_day} onChange={(e) => set({ open_day: +e.target.value })}>
                {DAYS.map((d, i) => (
                  <option key={d} value={i}>
                    {d}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Opens (time)">
              <input type="time" className="field" value={value.open_time} onChange={(e) => set({ open_time: e.target.value })} />
            </Field>
            <Field label="Closes (day)">
              <select className="field" value={value.close_day} onChange={(e) => set({ close_day: +e.target.value })}>
                {DAYS.map((d, i) => (
                  <option key={d} value={i}>
                    {d}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Closes (time)">
              <input type="time" className="field" value={value.close_time} onChange={(e) => set({ close_time: e.target.value })} />
            </Field>
          </div>
          <div>
            <div className="label">Daily blackouts</div>
            <div className="space-y-2">
              {value.blackouts.map((b, i) => (
                <div key={i} className="flex items-center gap-2">
                  <input
                    type="time"
                    className="field max-w-[140px]"
                    value={b.start}
                    onChange={(e) => set({ blackouts: value.blackouts.map((x, j) => (j === i ? { ...x, start: e.target.value } : x)) })}
                  />
                  <span className="text-faint">→</span>
                  <input
                    type="time"
                    className="field max-w-[140px]"
                    value={b.end}
                    onChange={(e) => set({ blackouts: value.blackouts.map((x, j) => (j === i ? { ...x, end: e.target.value } : x)) })}
                  />
                  <Button variant="ghost" size="sm" onClick={() => set({ blackouts: value.blackouts.filter((_, j) => j !== i) })} aria-label="Remove blackout">
                    <Trash2 className="size-3.5" />
                  </Button>
                </div>
              ))}
              <Button variant="ghost" size="sm" icon={<Plus className="size-3.5" />} onClick={() => set({ blackouts: [...value.blackouts, { start: "12:00", end: "13:00" }] })}>
                Add blackout
              </Button>
            </div>
            <p className="mt-1 text-xs text-faint">Default skips the daily rollover (23:55–00:10) when spreads spike.</p>
          </div>
        </>
      )}
    </div>
  );
}
