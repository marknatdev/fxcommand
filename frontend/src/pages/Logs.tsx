import { useQuery } from "@tanstack/react-query";
import { Pause, Play, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useLiveEvent, type LogLine } from "../api/live";
import { Button, Card, PageHeader } from "../components/ui";
import { cn } from "../lib/format";

const LEVELS = ["", "INFO", "WARNING", "ERROR"];
const levelClass: Record<string, string> = { INFO: "text-info", WARNING: "text-warn", ERROR: "text-down", CRITICAL: "text-down" };
const RANK: Record<string, number> = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };

export default function Logs() {
  const [level, setLevel] = useState("");
  const [paused, setPaused] = useState(false);
  const [lines, setLines] = useState<LogLine[]>([]);
  const [text, setText] = useState("");
  const box = useRef<HTMLDivElement>(null);
  const initial = useQuery({ queryKey: ["logs", level], queryFn: () => api.logs({ level: level || undefined, limit: 1000 }) });
  useEffect(() => {
    if (initial.data) setLines(initial.data);
  }, [initial.data]);
  useLiveEvent<LogLine>("log", (l) => {
    if (paused) return;
    if (level && (RANK[l.level] ?? 0) < RANK[level]) return;
    setLines((prev) => [...prev.slice(-1999), l]);
  });
  useEffect(() => {
    if (!paused && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines, paused]);
  const shown = text ? lines.filter((l) => l.message.toLowerCase().includes(text.toLowerCase())) : lines;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Logs"
        subtitle="Technical diagnostics from the engine, broker and API. For trading decisions, see the Journal."
        actions={
          <>
            <input className="field w-56" placeholder="Search…" value={text} onChange={(e) => setText(e.target.value)} data-testid="log-search" />
            <select className="field w-36" value={level} onChange={(e) => setLevel(e.target.value)} data-testid="log-level">
              {LEVELS.map((l) => (
                <option key={l} value={l}>
                  {l || "All levels"}
                </option>
              ))}
            </select>
            <Button icon={paused ? <Play className="size-4" /> : <Pause className="size-4" />} onClick={() => setPaused(!paused)} data-testid="log-pause">
              {paused ? "Resume" : "Pause"}
            </Button>
            <Button variant="ghost" icon={<Trash2 className="size-4" />} onClick={() => setLines([])}>
              Clear
            </Button>
          </>
        }
      />
      <Card bodyClass="p-0">
        <div ref={box} className="h-[70vh] overflow-y-auto bg-bg/60 p-3 font-mono text-xs leading-relaxed" data-testid="log-box">
          {shown.map((l) => (
            <div key={l.seq} className="flex gap-3 whitespace-pre-wrap break-all" data-testid="log-line">
              <span className="shrink-0 text-faint">{new Date(l.time * 1000).toLocaleTimeString()}</span>
              <span className={cn("w-16 shrink-0", levelClass[l.level] ?? "text-dim")}>{l.level}</span>
              <span className="w-40 shrink-0 truncate text-faint">{l.logger}</span>
              <span className="text-ink/90">{l.message}</span>
            </div>
          ))}
          {!shown.length && <div className="py-10 text-center text-faint">No log lines</div>}
        </div>
      </Card>
    </div>
  );
}
