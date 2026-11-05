import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { control, post } from "../api";
import { ActionButton, ErrorNote, EventLog, StateBadge, StopButton, fmtDuration, fmtTime } from "../components";
import { useEventStream, usePoll } from "../hooks";
import type { Project, Task, TestCase, TestRun } from "../types";

type Tab = "log" | "tests" | "diff" | "details";

const STEP_LABEL: Record<string, string> = { tester: "Tester", builder: "Builder", acceptance: "Acceptance" };

export function pct(n: number | null | undefined) {
  return n == null ? "—" : `${Math.round(n)}%`;
}

export function CaseList({ cases, onlyTask = false }: { cases: TestCase[]; onlyTask?: boolean }) {
  const shown = onlyTask ? cases.filter((c) => c.task_test) : cases;
  const order = { failed: 0, error: 1, skipped: 2, passed: 3 };
  const sorted = shown.slice().sort((a, b) => order[a.status] - order[b.status] || a.name.localeCompare(b.name));
  if (!sorted.length) return <p className="muted small">No test cases reported.</p>;
  return (
    <table className="cases small">
      <thead>
        <tr><th>Result</th><th>Test</th><th>File</th><th>Time</th></tr>
      </thead>
      <tbody>
        {sorted.map((c) => (
          <tr key={c.id} className={`case-${c.status}`}>
            <td>
              <span className={`badge test-${c.status}`}>{c.status === "passed" ? "✓ passed" : c.status === "skipped" ? "skipped" : "✗ " + c.status}</span>
            </td>
            <td>
              <span className="mono">{c.name}</span>
              {c.task_test && !onlyTask && <span className="badge kind-badge">this task</span>}
              {c.message && <div className="case-message mono">{c.message}</div>}
            </td>
            <td className="mono muted">{c.file || c.classname}</td>
            <td className="muted">{c.duration_s ? `${c.duration_s.toFixed(2)}s` : ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function RunSummary({ run }: { run: TestRun }) {
  const failing = run.failed + run.errors;
  return (
    <div className="test-summary">
      <div className="stat">
        <div className={`stat-value ${failing ? "bad" : "ok-text"}`}>{run.passed} / {run.passed + failing}</div>
        <div className="stat-label">tests passing</div>
      </div>
      <div className="stat">
        <div className="stat-value">{pct(run.coverage)}</div>
        <div className="stat-label">line coverage</div>
      </div>
      {run.step === "builder" && (
        <div className="stat">
          <div className="stat-value">{pct(run.changed_coverage)}</div>
          <div className="stat-label">of changed lines covered{run.changed_lines ? ` (${run.changed_lines})` : ""}</div>
        </div>
      )}
    </div>
  );
}

function TestsView({ taskId, live }: { taskId: number; live: boolean }) {
  const { data, error } = usePoll<{ runs: TestRun[] }>(`/api/tasks/${taskId}/tests`, live ? 3000 : 30000);
  if (error) return <ErrorNote error={error} />;
  if (!data) return <div className="muted">Loading…</div>;
  if (!data.runs.length) return <div className="muted">No test results yet. The Tester writes this task's tests first.</div>;
  const latest = data.runs[0];
  const files = Object.entries(latest.files);
  return (
    <div className="tests-view">
      <h3>
        Latest run: {STEP_LABEL[latest.step]} step <span className="muted small">{fmtTime(latest.created_at)}</span>
      </h3>
      {latest.problem && <div className="error-note">{latest.problem}</div>}
      <RunSummary run={latest} />
      <h3>This task's tests</h3>
      <CaseList cases={latest.cases ?? []} onlyTask />
      <details>
        <summary className="small">Whole suite ({(latest.cases ?? []).length} tests)</summary>
        <CaseList cases={latest.cases ?? []} />
      </details>
      {files.length > 0 && (
        <details>
          <summary className="small">Coverage by file</summary>
          <table className="cases small">
            <thead><tr><th>File</th><th>Covered</th><th>Lines</th><th></th></tr></thead>
            <tbody>
              {files.map(([f, v]) => (
                <tr key={f}>
                  <td className="mono">{f}</td>
                  <td>{v.covered}</td>
                  <td>{v.total}</td>
                  <td>{pct(v.total ? (100 * v.covered) / v.total : null)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
      <h3>History</h3>
      <table className="cases small">
        <thead><tr><th>When</th><th>Step</th><th>Passed</th><th>Failed</th><th>Coverage</th><th>Changed lines</th></tr></thead>
        <tbody>
          {data.runs.map((r) => (
            <tr key={r.id}>
              <td>{fmtTime(r.created_at)}</td>
              <td>{STEP_LABEL[r.step]}{r.step === "tester" && <span className="muted"> (expected to fail)</span>}</td>
              <td>{r.passed}</td>
              <td className={r.failed + r.errors ? "warn" : ""}>{r.failed + r.errors}</td>
              <td>{pct(r.coverage)}</td>
              <td>{r.step === "builder" ? pct(r.changed_coverage) : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Where the task is in Tester -> Builder. */
function Pipeline({ t }: { t: Task }) {
  if (!t.criteria.length) return null;
  const last = t.runs?.[t.runs.length - 1];
  const building = t.tests_ready;
  const step = (name: "tester" | "builder", label: string) => {
    const done = name === "tester" ? t.tests_ready : t.state === "done";
    const now = last?.step === name && (t.state === "running" || t.state === "validating");
    return <span className={`pipe-step ${done ? "done" : ""} ${now ? "now" : ""}`}>{label}</span>;
  };
  return (
    <div className="pipeline small">
      {step("tester", t.kind === "acceptance" ? "Tester writes the acceptance tests" : "Tester writes tests")}
      {t.kind !== "acceptance" && (
        <>
          <span className="muted">→</span>
          {step("builder", building ? "Builder makes them pass" : "Builder")}
        </>
      )}
      {t.coverage_rounds > 0 && <span className="muted">· sent back once for coverage</span>}
    </div>
  );
}

function DiffView({ taskId, live }: { taskId: number; live: boolean }) {
  const { data, error } = usePoll<{ diff: string; files: string[] }>(`/api/tasks/${taskId}/diff`, live ? 3000 : 60000);
  if (error) return <ErrorNote error={error} />;
  if (!data) return <div className="muted">Loading…</div>;
  if (!data.diff) return <div className="muted">No changes yet.</div>;
  return (
    <div>
      <div className="small muted">{data.files.length} file(s): {data.files.join(", ")}</div>
      <pre className="diff">
        {data.diff.split("\n").map((l, i) => (
          <div
            key={i}
            className={
              l.startsWith("+++") || l.startsWith("---")
                ? "d-file"
                : l.startsWith("+")
                  ? "d-add"
                  : l.startsWith("-")
                    ? "d-del"
                    : l.startsWith("@@")
                      ? "d-hunk"
                      : ""
            }
          >
            {l || " "}
          </div>
        ))}
      </pre>
    </div>
  );
}

function Details({ t }: { t: Task }) {
  return (
    <div className="details">
      {t.description && (
        <>
          <h3>Goal</h3>
          <p className="pre">{t.description}</p>
        </>
      )}
      {t.criteria.length > 0 && (
        <>
          <h3>Done when</h3>
          <ul>{t.criteria.map((x, i) => <li key={i}>{x}</li>)}</ul>
        </>
      )}
      {t.req_refs.length > 0 && (
        <>
          <h3>Implements</h3>
          <p>{t.req_refs.join(", ")}</p>
        </>
      )}
      {t.validation_cmd && (
        <>
          <h3>Check (scripted task)</h3>
          <pre className="mono block">{t.validation_cmd}</pre>
        </>
      )}
      {t.allowed_globs.length > 0 && (
        <>
          <h3>Allowed files</h3>
          <ul className="mono">{t.allowed_globs.map((g) => <li key={g}>{g}</li>)}</ul>
        </>
      )}
      {t.deps.length > 0 && (
        <>
          <h3>Depends on</h3>
          <p>{t.deps.map((d) => <Link key={d} to={`/tasks/${d}`}>#{d} </Link>)}</p>
        </>
      )}
      <h3>Runs</h3>
      <table className="runs">
        <thead>
          <tr><th>#</th><th>Agent</th><th>State</th><th>Started</th><th>Duration</th><th>Model</th><th>Calls</th><th>Tokens</th><th>Reason</th></tr>
        </thead>
        <tbody>
          {(t.runs ?? []).slice().reverse().map((r) => (
            <tr key={r.id}>
              <td>{r.id}</td>
              <td>{r.step ? STEP_LABEL[r.step] : "—"}</td>
              <td>{r.state}</td>
              <td>{fmtTime(r.started_at)}</td>
              <td>{fmtDuration(r.started_at, r.ended_at)}</td>
              <td className="mono small">{r.model || "—"}</td>
              <td className="small">{r.model_requests}</td>
              <td className="small">{(r.tokens_in + r.tokens_out).toLocaleString()}</td>
              <td className="small">{r.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Meter({ label, value, max, fmt = (n: number) => n.toLocaleString() }: {
  label: string;
  value: number;
  max: number;
  fmt?: (n: number) => string;
}) {
  const pct = Math.min(100, max ? (100 * value) / max : 0);
  return (
    <div className="meter">
      <div className="meter-label">
        <span>{label}</span>
        <span className="muted">
          {fmt(value)} / {fmt(max)}
        </span>
      </div>
      <div className="meter-bar">
        <div className={`meter-fill ${pct >= 90 ? "hot" : pct >= 70 ? "warm" : ""}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

const LEVELS = ["retrying", "split requested", "on the escalation model"];

function Watchdog({ t }: { t: Task }) {
  const { data: project } = usePoll<Project>(`/api/projects/${t.project_id}`, 10000);
  const run = t.runs?.[t.runs.length - 1];
  const w = project?.watchdog_effective;
  if (!run || !w) return null;
  return (
    <section className="card watchdog">
      <div className="row">
        <h2>Watchdog</h2>
        <span className="small muted">
          {LEVELS[t.escalation_level] ?? ""}
          {t.level_failures > 0 && ` · ${t.level_failures} failure(s) at this level`}
        </span>
      </div>
      <div className="meters">
        <Meter label="Model calls" value={run.model_requests} max={w.max_requests} />
        <Meter label="Tokens" value={run.tokens_in + run.tokens_out} max={w.max_tokens} />
        <Meter label="Lines changed" value={run.diff_lines} max={w.max_diff_lines} />
        <Meter
          label="Time"
          value={Math.round((run.ended_at ?? Date.now() / 1000) - run.started_at)}
          max={t.timeout_s}
          fmt={(n) => `${Math.round(n / 60)}m`}
        />
      </div>
      {run.watchdog && <div className="error-note">Stopped by the watchdog: {run.watchdog}</div>}
    </section>
  );
}

export default function TaskPage() {
  const id = Number(useParams().id);
  const { data: t, error, reload } = usePoll<Task>(`/api/tasks/${id}`, 1500);
  const { events, connected } = useEventStream({ task_id: id });
  const [tab, setTab] = useState<Tab>("log");

  if (!t) return <ErrorNote error={error} />;
  const active = t.state === "running" || t.state === "validating";
  const canRetry = ["failed", "stopped_by_user", "paused"].includes(t.state);
  const run = t.runs?.[t.runs.length - 1];

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="crumbs">
            <Link to="/">Projects</Link> / <Link to={`/projects/${t.project_id}`}>project {t.project_id}</Link> /
          </div>
          <h1>
            #{t.id} {t.title} <StateBadge state={t.state} pending={t.stop_pending && active} />
          </h1>
          <div className="muted small">
            attempt {t.attempts}
            {run && active && <> · running for {fmtDuration(run.started_at, null)}</>}
            {t.hold && <span className="warn"> · on hold (will park after current step)</span>}
          </div>
          {t.reason && t.state !== "done" && <div className="reason">{t.reason}</div>}
          <Pipeline t={t} />
          {t.parent_id && (
            <div className="small muted">
              split from <Link to={`/tasks/${t.parent_id}`}>#{t.parent_id}</Link>
            </div>
          )}
        </div>
        <div className="actions">
          {(active || t.state === "queued") &&
            (t.hold ? (
              <ActionButton onClick={async () => { await control("task", "resume", id); reload(); }}>Resume</ActionButton>
            ) : (
              <ActionButton onClick={async () => { await control("task", "pause", id); reload(); }}>Pause</ActionButton>
            ))}
          {canRetry && (
            <>
              <ActionButton kind="primary" onClick={async () => { await post(`/api/tasks/${id}/retry`, { reset: false }); reload(); }}>
                {t.state === "paused" ? "Resume" : "Retry (keep work)"}
              </ActionButton>
              <ActionButton
                onClick={async () => {
                  if (!confirm("Discard this task's changes and start over from the milestone branch?")) return;
                  await post(`/api/tasks/${id}/retry`, { reset: true });
                  reload();
                }}
              >
                Retry from scratch
              </ActionButton>
            </>
          )}
          {t.state !== "done" && t.state !== "discarded" && !active && (
            <ActionButton kind="danger" onClick={async () => { await post(`/api/tasks/${id}/discard`); reload(); }}>
              Discard
            </ActionButton>
          )}
          <StopButton
            size="big"
            disabled={!(active || t.state === "queued")}
            onStop={async () => {
              await control("task", "stop", id);
              reload();
            }}
          />
        </div>
      </div>

      <Watchdog t={t} />
      <div className="tabs">
        {(["log", "tests", "diff", "details"] as Tab[]).map((x) => (
          <button key={x} className={tab === x ? "active" : ""} onClick={() => setTab(x)}>
            {{ log: "Live log", tests: "Tests", diff: "Diff", details: "Details" }[x]}
          </button>
        ))}
        <span className={`dot ${connected ? "ok" : "bad"}`}>{connected ? "live" : "reconnecting"}</span>
      </div>
      <section className="card">
        {tab === "log" && <EventLog events={events} empty="Waiting for the agent…" />}
        {tab === "tests" && <TestsView taskId={id} live={active} />}
        {tab === "diff" && <DiffView taskId={id} live={active} />}
        {tab === "details" && <Details t={t} />}
      </section>
    </div>
  );
}
