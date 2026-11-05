import { useEffect, useMemo, useRef, useState } from "react";
import type { CoveragePoint } from "./types";

const H = 180;
const M = { top: 12, right: 64, bottom: 24, left: 40 };
const TICKS = [0, 25, 50, 75, 100];

/** Project line coverage after each verified task. One series on a 0-100% axis;
 * the test count is in the tooltip and table (never a second axis). */
export default function CoverageChart({ points }: { points: CoveragePoint[] }) {
  const data = useMemo(() => points.filter((p) => p.coverage != null), [points]);
  const [hover, setHover] = useState<number | null>(null);
  const [table, setTable] = useState(false);
  const svgRef = useRef<SVGSVGElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(640);
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, [data.length > 0]);

  const geo = useMemo(() => {
    if (!data.length) return null;
    const n = data.length;
    // Evenly spaced by verified task: the story is "after each task", not wall-clock gaps.
    const x = (i: number) => (n === 1 ? (M.left + W - M.right) / 2 : M.left + (i / (n - 1)) * (W - M.left - M.right));
    const y = (v: number) => M.top + (1 - v / 100) * (H - M.top - M.bottom);
    const path = data.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p.coverage ?? 0)}`).join("");
    return { x, y, path };
  }, [data, W]);

  if (!geo) return <p className="muted small">No coverage yet: it appears after the first task's tests run.</p>;
  const last = data[data.length - 1];
  const at = hover == null ? null : data[hover];

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const box = svgRef.current?.getBoundingClientRect();
    if (!box || data.length === 0) return;
    const px = ((e.clientX - box.left) / box.width) * W;
    const frac = Math.min(1, Math.max(0, (px - M.left) / (W - M.left - M.right)));
    setHover(Math.round(frac * (data.length - 1)));
  };

  return (
    <div className="progress-chart" ref={wrapRef}>
      <div className="row chart-head">
        <span className="small muted">Line coverage after each task</span>
        <button type="button" className="btn small" onClick={() => setTable((t) => !t)}>
          {table ? "Chart" : "Table"}
        </button>
      </div>
      {table ? (
        <table className="data-table small">
          <thead>
            <tr>
              <th>Time</th>
              <th>Task</th>
              <th>Coverage</th>
              <th>Tests</th>
            </tr>
          </thead>
          <tbody>
            {data.map((p, i) => (
              <tr key={i}>
                <td>{new Date(p.ts * 1000).toLocaleString()}</td>
                <td>{p.task_id ? `#${p.task_id}` : ""}</td>
                <td>{p.coverage!.toFixed(1)}%</td>
                <td>
                  {p.passed} of {p.tests} passed
                </td>
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
            aria-label={`Coverage: ${last.coverage!.toFixed(0)}% after ${data.length} task(s)`}
            onPointerMove={onMove}
            onPointerLeave={() => setHover(null)}
          >
            {TICKS.map((v) => (
              <g key={v}>
                <line className="grid" x1={M.left} x2={W - M.right} y1={geo.y(v)} y2={geo.y(v)} />
                <text className="axis" x={M.left - 6} y={geo.y(v)} dy="0.32em" textAnchor="end">
                  {v}%
                </text>
              </g>
            ))}
            <path className="line-done" d={geo.path} />
            {data.length === 1 && <circle className="marker-done" cx={geo.x(0)} cy={geo.y(last.coverage!)} r={4} />}
            <text className="direct-label strong" x={W - M.right + 6} y={geo.y(last.coverage!)} dy="0.32em">
              {last.coverage!.toFixed(0)}%
            </text>
            {at && hover != null && (
              <g>
                <line className="crosshair" x1={geo.x(hover)} x2={geo.x(hover)} y1={M.top} y2={H - M.bottom} />
                <circle className="marker-done" cx={geo.x(hover)} cy={geo.y(at.coverage!)} r={4} />
              </g>
            )}
          </svg>
          {at && hover != null && (
            <div className="chart-tooltip small" style={{ left: `${(geo.x(hover) / W) * 100}%` }}>
              <div className="muted">
                {new Date(at.ts * 1000).toLocaleString()}
                {at.task_id ? ` · task #${at.task_id}` : ""}
              </div>
              <div>
                Coverage <b>{at.coverage!.toFixed(1)}%</b>
              </div>
              <div>
                Tests <b>{at.passed}</b> of {at.tests} passed
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
