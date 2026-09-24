import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";
import { Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Bar, PositionView, Trade } from "../api/types";
import { money, shortTime } from "../lib/format";

const C = {
  bg: "#0d1422",
  grid: "#16203a",
  text: "#8b98b0",
  up: "#22c55e",
  down: "#f43f5e",
  accent: "#22d3ee",
  warn: "#f59e0b",
};

/** Candles with entry/exit markers and SL/TP lines for open positions. */
export function CandleChart({
  bars,
  trades = [],
  positions = [],
  digits = 5,
  height = 380,
}: {
  bars: Bar[];
  trades?: Trade[];
  positions?: PositionView[];
  digits?: number;
  height?: number;
}) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markers = useRef<ReturnType<typeof createSeriesMarkers<Time>> | null>(null);
  const fitted = useRef(false);

  useEffect(() => {
    if (!el.current) return;
    const c = createChart(el.current, {
      height,
      layout: { background: { type: ColorType.Solid, color: C.bg }, textColor: C.text, fontFamily: "JetBrains Mono, monospace", fontSize: 11 },
      grid: { vertLines: { color: C.grid }, horzLines: { color: C.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#1c2740" },
      timeScale: { borderColor: "#1c2740", timeVisible: true, secondsVisible: false },
      autoSize: true,
    });
    const s = c.addSeries(CandlestickSeries, {
      upColor: C.up,
      downColor: C.down,
      borderUpColor: C.up,
      borderDownColor: C.down,
      wickUpColor: C.up,
      wickDownColor: C.down,
    });
    chart.current = c;
    series.current = s;
    markers.current = createSeriesMarkers(s, []);
    return () => {
      c.remove();
      chart.current = null;
      series.current = null;
      fitted.current = false;
    };
  }, [height]);

  useEffect(() => {
    const s = series.current;
    if (!s) return;
    s.applyOptions({ priceFormat: { type: "price", precision: digits, minMove: 10 ** -digits } });
    s.setData(bars.map((b) => ({ time: b.time as UTCTimestamp, open: b.open, high: b.high, low: b.low, close: b.close })));
    if (!fitted.current && bars.length) {
      chart.current?.timeScale().fitContent();
      fitted.current = true;
    }
    if (!bars.length) return;
    const first = bars[0].time;
    const last = bars[bars.length - 1].time;
    const snap = (t: number) => {
      // markers must sit on an existing bar time
      let lo = 0;
      let hi = bars.length - 1;
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (bars[mid].time <= t) lo = mid;
        else hi = mid - 1;
      }
      return bars[lo].time as UTCTimestamp;
    };
    const m: SeriesMarker<Time>[] = [];
    for (const t of trades) {
      if (t.open_time >= first && t.open_time <= last + 86400) {
        m.push({
          time: snap(t.open_time),
          position: t.side === "long" ? "belowBar" : "aboveBar",
          shape: t.side === "long" ? "arrowUp" : "arrowDown",
          color: t.side === "long" ? C.up : C.down,
          text: `${t.side === "long" ? "BUY" : "SELL"} ${t.volume}`,
        });
      }
      if (t.close_time && t.close_time >= first && t.close_time <= last + 86400) {
        m.push({
          time: snap(t.close_time),
          position: "inBar",
          shape: "circle",
          color: (t.profit ?? 0) >= 0 ? C.accent : C.warn,
          text: `${t.close_reason.toUpperCase()} ${(t.profit ?? 0) >= 0 ? "+" : ""}${(t.profit ?? 0).toFixed(2)}`,
        });
      }
    }
    m.sort((a, b) => (a.time as number) - (b.time as number));
    markers.current?.setMarkers(m);
  }, [bars, trades, digits]);

  useEffect(() => {
    const s = series.current;
    if (!s) return;
    const lines = positions.flatMap((p) => {
      const out = [
        s.createPriceLine({ price: p.price_open, color: C.accent, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: `#${p.ticket} entry` }),
      ];
      if (p.sl) out.push(s.createPriceLine({ price: p.sl, color: C.down, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "SL" }));
      if (p.tp) out.push(s.createPriceLine({ price: p.tp, color: C.up, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "TP" }));
      return out;
    });
    return () => lines.forEach((l) => s.removePriceLine(l));
  }, [positions]);

  return <div ref={el} data-testid="candle-chart" className="w-full overflow-hidden rounded-lg border border-line" style={{ height }} />;
}

/** Area chart for equity or cumulative P&L. */
export function ValueChart({
  data,
  dataKey,
  height = 220,
  baseline,
  testId,
}: {
  data: { ts: number; [k: string]: number }[];
  dataKey: string;
  height?: number;
  baseline?: number;
  testId?: string;
}) {
  const last = data.length ? data[data.length - 1][dataKey] : 0;
  const ref = baseline ?? (data.length ? data[0][dataKey] : 0);
  const color = last >= ref ? C.up : C.down;
  const id = `g-${dataKey}-${testId ?? "x"}`;
  return (
    <div data-testid={testId} style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.35} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke={C.grid} vertical={false} />
          <XAxis dataKey="ts" tickFormatter={(v) => shortTime(v)} stroke={C.text} fontSize={10} tickLine={false} axisLine={false} minTickGap={40} />
          <YAxis stroke={C.text} fontSize={10} tickLine={false} axisLine={false} width={64} domain={["auto", "auto"]} tickFormatter={(v) => money(v, "", 0)} />
          <Tooltip
            contentStyle={{ background: "#111a2c", border: "1px solid #26334f", borderRadius: 8, fontSize: 12 }}
            labelFormatter={(v) => shortTime(v as number)}
            formatter={(v) => [money(v as number), dataKey]}
          />
          {baseline !== undefined && <ReferenceLine y={baseline} stroke="#26334f" strokeDasharray="4 4" />}
          <Area type="monotone" dataKey={dataKey} stroke={color} strokeWidth={2} fill={`url(#${id})`} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
