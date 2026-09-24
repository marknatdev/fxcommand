import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { toast } from "sonner";
import type { JournalEntry, LogLine, Snapshot } from "./types";

type Listener = (data: unknown) => void;

interface LiveCtx {
  snapshot: Snapshot | null;
  snapshotAt: number; // wall seconds the last snapshot arrived (a stalled engine sends none)
  wsConnected: boolean;
  on: (type: string, fn: Listener) => () => void;
}

const Ctx = createContext<LiveCtx>({ snapshot: null, snapshotAt: 0, wsConnected: false, on: () => () => {} });

// Queries whose data changes when the engine records something.
const LIVE_KEYS = ["overview", "sessions", "session", "trades", "journal", "risk", "positions", "strategies", "symbols", "learning", "arena"];

export function LiveProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [snapshotAt, setSnapshotAt] = useState(0);
  const [wsConnected, setWsConnected] = useState(false);
  const listeners = useRef(new Map<string, Set<Listener>>());
  const invalidateTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry = 0;
    let closed = false;
    let timer: number | undefined;

    const invalidate = () => {
      window.clearTimeout(invalidateTimer.current);
      invalidateTimer.current = window.setTimeout(() => {
        LIVE_KEYS.forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
      }, 350);
    };

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => {
        retry = 0;
        setWsConnected(true);
      };
      ws.onclose = () => {
        setWsConnected(false);
        if (!closed) timer = window.setTimeout(connect, Math.min(5000, 500 * 2 ** retry++));
      };
      ws.onmessage = (msg) => {
        const ev = JSON.parse(msg.data) as { type: string; data: unknown };
        if (ev.type === "snapshot") {
          setSnapshot(ev.data as Snapshot);
          setSnapshotAt(Date.now() / 1000);
        }
        if (ev.type === "journal") invalidate();
        if (ev.type === "alert") {
          const j = ev.data as JournalEntry;
          (j.level === "error" ? toast.error : toast.warning)(j.message, { duration: 8000 });
        }
        listeners.current.get(ev.type)?.forEach((fn) => fn(ev.data));
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      ws?.close();
    };
  }, [qc]);

  const on = useCallback((type: string, fn: Listener) => {
    const set = listeners.current.get(type) ?? new Set<Listener>();
    set.add(fn);
    listeners.current.set(type, set);
    return () => {
      set.delete(fn);
    };
  }, []);

  return <Ctx.Provider value={{ snapshot, snapshotAt, wsConnected, on }}>{children}</Ctx.Provider>;
}

export const useLive = () => useContext(Ctx);

export function useLiveEvent<T = unknown>(type: string, fn: (data: T) => void) {
  const { on } = useLive();
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => on(type, (d) => ref.current(d as T)), [on, type]);
}

export type { LogLine };
