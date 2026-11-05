import { useEffect, useMemo, useRef, useState } from "react";

export interface ProgressPoint {
  ts: number; // seconds
  done: number;
  planned: number;
}

const H = 200;
const M = { top: 12, right: 84, bottom: 24, left: 32 };

function fmtAxisTime(ts: number, span: number) {
  const d = new Date(ts * 1000);
  return span > 2 * 86400
    ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
    : d.toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        ...(span < 3600 ? { second: "2-digit" } : {}),
      });
}

function niceStep(max: number) {
  for (const s of [1, 2, 5, 10, 20, 50, 100, 200, 500]) if (max / s <= 4) return s;
  return 1000;
}

/** Tasks done vs. planned over time: done is the series, planned is a reference line. */
export default function ProgressChart({ points, now }: { points: ProgressPoint[]; now: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const [table, setTable] = useState(false);
  const svgRef = useRef<SVGSVGElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  // Draw in CSS pixels so text and strokes keep their size at any width.
  const [W, setW] = useState(640);
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, [points.length > 0]);

  const geo = useMemo(() => {
    if (!points.length) return null;
    // Carry the latest state to "now" so the lines reach the right edge.
    const series = [...points, { ...points[points.length - 1], ts: Math.max(now, points[points.length - 1].ts) }];
    const t0 = series[0].ts;
    const end = series[series.length - 1].ts;
    const t1 = end > t0 ? end : t0 + 60;
    const yMax = Math.max(1, ...series.map((p) => p.planned), ...series.map((p) => p.done));
    const step = niceStep(yMax);
    const top = Math.ceil(yMax / step) * step;
    const x = (t: number) => M.left + ((t - t0) / (t1 - t0)) * (W - M.left - M.right);
    const y = (v: number) => M.top + (1 - v / top) * (H - M.top - M.bottom);
    const stepPath = (key: "done" | "planned") =>
      series
        .map((p, i) => (i === 0 ? `M${x(p.ts)},${y(p[key])}` : `H${x(p.ts)}V${y(p[key])}`))
        .join("");
    const ticks = [];
    for (let v = 0; v <= top; v += step) ticks.push(v);
    return { series, t0, t1, x, y, ticks, done: stepPath("done"), planned: stepPath("planned") };
  }, [points, now, W]);

  if (!geo) return <p className="muted small">No tasks yet.</p>;
  const last = geo.series[geo.series.length - 1];
  // The state in effect at the hovered time (step-after).
  const at = hover == null ? null : [...geo.series].reverse().find((p) => p.ts <= hover) ?? geo.series[0];

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const box = svgRef.current?.getBoundingClientRect();
    if (!box) return;
    const px = ((e.clientX - box.left) / box.width) * W;
    const frac = Math.min(1, Math.max(0, (px - M.left) / (W - M.left - M.right)));
    setHover(geo.t0 + frac * (geo.t1 - geo.t0));
  };

  return (
    <div className="progress-chart" ref={wrapRef}>
      <div className="row chart-head">
        <div className="legend small">
          <span className="legend-item">
            <span className="swatch swatch-done" /> Done
          </span>
          <span className="legend-item">
            <span className="swatch swatch-planned" /> Planned
          </span>
        </div>
        <button type="button" className="btn small" onClick={() => setTable((t) => !t)}>
          {table ? "Chart" : "Table"}
        </button>
      </div>
      {table ? (
        <table className="data-table small">
          <thead>
            <tr>
              <th>Time</th>
              <th>Done</th>
              <th>Planned</th>
            </tr>
          </thead>
          <tbody>
            {points.map((p) => (
              <tr key={p.ts}>
                <td>{new Date(p.ts * 1000).toLocaleString()}</td>
                <td>{p.done}</td>
                <td>{p.planned}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="chart-wrap">
          <svg
            ref={svgRef}
            width={W}
            height={H}
            viewBox={`0 0 ${W} ${H}`}
            role="img"
            aria-label={`Progress: ${last.done} of ${last.planned} tasks done`}
            onPointerMove={onMove}
            onPointerLeave={() => setHover(null)}
          >
            {geo.ticks.map((v) => (
              <g key={v}>
                <line className="grid" x1={M.left} x2={W - M.right} y1={geo.y(v)} y2={geo.y(v)} />
                <text className="axis" x={M.left - 6} y={geo.y(v)} dy="0.32em" textAnchor="end">
                  {v}
                </text>
              </g>
            ))}
            <text className="axis" x={M.left} y={H - 6}>
              {fmtAxisTime(geo.t0, geo.t1 - geo.t0)}
            </text>
            <text className="axis" x={W - M.right} y={H - 6} textAnchor="end">
              {fmtAxisTime(geo.t1, geo.t1 - geo.t0)}
            </text>
            <path className="line-planned" d={geo.planned} />
            <path className="line-done" d={geo.done} />
            <text className="direct-label" x={W - M.right + 6} y={geo.y(last.planned)} dy="0.32em">
              {last.planned} planned
            </text>
            <text
              className="direct-label strong"
              x={W - M.right + 6}
              y={geo.y(last.done)}
              dy={last.planned - last.done < 1 ? "1.4em" : "0.32em"}
            >
              {last.done} done
            </text>
            {hover != null && at && (
              <g>
                <line className="crosshair" x1={geo.x(hover)} x2={geo.x(hover)} y1={M.top} y2={H - M.bottom} />
                <circle className="marker-done" cx={geo.x(hover)} cy={geo.y(at.done)} r={4} />
              </g>
            )}
          </svg>
          {hover != null && at && (
            <div
              className="chart-tooltip small"
              style={{ left: `${(geo.x(hover) / W) * 100}%` }}
            >
              <div className="muted">{new Date(hover * 1000).toLocaleString()}</div>
              <div>
                <span className="swatch swatch-done" /> Done <b>{at.done}</b>
              </div>
              <div>
                <span className="swatch swatch-planned" /> Planned <b>{at.planned}</b>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
