import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { control, del, patch, post } from "../api";
import { ActionButton, ErrorNote, EventLog, Field, StateBadge, StopButton, lines } from "../components";
import { useEventStream, usePoll } from "../hooks";
import type { CoveragePoint, Node, PlanRevision, Project, ProjectPhase, RoleInfo, SystemInfo, Task, TaskState, TestRun } from "../types";
import CoverageChart from "../CoverageChart";
import ProgressChart, { type ProgressPoint } from "../ProgressChart";
import { CaseList, RunSummary } from "./TaskPage";
import PlanReview from "./PlanReview";
import SpecWorkspace from "./SpecWorkspace";

const COLUMNS: { title: string; states: TaskState[] }[] = [
  { title: "Queued", states: ["queued"] },
  { title: "Running", states: ["running", "validating"] },
  { title: "Needs attention", states: ["failed", "stopped_by_user", "paused", "blocked", "awaiting_merge"] },
  { title: "Done", states: ["done", "split"] },
];

function TaskCard({ t, all }: { t: Task; all: Task[] }) {
  const blockedBy = t.deps.filter((d) => all.find((x) => x.id === d)?.state !== "done");
  return (
    <Link to={`/tasks/${t.id}`} className={`task-card state-border-${t.state}`}>
      <div className="row">
        <span className="muted small">#{t.id}</span>
        <StateBadge state={t.state} />
      </div>
      <div className="task-title">{t.title}</div>
      {t.hold && t.state === "queued" && <div className="small warn">on hold</div>}
      {t.state === "queued" && blockedBy.length > 0 && (
        <div className="small muted">waits for {blockedBy.map((d) => `#${d}`).join(", ")}</div>
      )}
      {t.reason && t.state !== "done" && <div className="small reason">{t.reason}</div>}
      {t.attempts > 1 && <div className="small muted">attempt {t.attempts}</div>}
      {t.escalation_level === 2 && <div className="small warn">on the escalation model</div>}
      {t.origin === "replan" && <div className="small muted">added by replanning</div>}
      {t.state === "split" && <div className="small muted">split into smaller tasks</div>}
    </Link>
  );
}

function AddTask({ project, tasks, onDone }: { project: Project; tasks: Task[]; onDone: () => void }) {
  const { data: sys } = usePoll<SystemInfo>("/api/system", 10000);
  const [f, setF] = useState({
    title: "",
    description: "",
    criteria: "",
    validation_cmd: "",
    deps: [] as number[],
    executor_kind: "",
    executor_script: "",
    timeout_s: 1800,
  });
  const [error, setError] = useState("");
  const set = (k: string, v: unknown) => setF((x) => ({ ...x, [k]: v }));
  const kind = f.executor_kind || project.executor_kind;
  const scripted = kind === "shell";

  return (
    <form
      className="card form"
      onSubmit={async (e) => {
        e.preventDefault();
        setError("");
        try {
          await post(`/api/projects/${project.id}/tasks`, {
            title: f.title,
            description: f.description,
            deps: f.deps,
            executor_kind: f.executor_kind,
            timeout_s: f.timeout_s,
            ...(scripted
              ? { executor_script: f.executor_script, validation_cmd: f.validation_cmd }
              : { criteria: lines(f.criteria) }),
          });
          onDone();
        } catch (err) {
          setError((err as Error).message);
        }
      }}
    >
      <h2>Add task</h2>
      <p className="small muted">
        {scripted
          ? "A scripted task: the shell executor runs your script, then your check."
          : "Describe the work in plain language. The Tester Agent writes its tests from the criteria, then the Builder Agent makes them pass."}
      </p>
      <div className="grid2">
        <Field label="Title">
          <input required value={f.title} onChange={(e) => set("title", e.target.value)} />
        </Field>
        <Field label="Goal">
          <textarea rows={3} value={f.description} onChange={(e) => set("description", e.target.value)} />
        </Field>
        {!scripted && (
          <Field label="Done when" hint="Plain-language criteria, one per line">
            <textarea required rows={4} value={f.criteria} placeholder={"Searching shows matching todos\nAn empty search shows everything"} onChange={(e) => set("criteria", e.target.value)} />
          </Field>
        )}
        <Field label="Depends on">
          <select multiple value={f.deps.map(String)} onChange={(e) => set("deps", Array.from(e.target.selectedOptions, (o) => Number(o.value)))}>
            {tasks.filter((t) => t.state !== "discarded").map((t) => (
              <option key={t.id} value={t.id}>
                #{t.id} {t.title}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Executor" hint={`Default: ${project.executor_kind}`}>
          <select value={f.executor_kind} onChange={(e) => set("executor_kind", e.target.value)}>
            <option value="">project default</option>
            {sys?.executors.map((x) => <option key={x}>{x}</option>)}
          </select>
        </Field>
        <Field label="Timeout per step (s)">
          <input type="number" min={10} value={f.timeout_s} onChange={(e) => set("timeout_s", Number(e.target.value))} />
        </Field>
        {scripted && (
          <>
            <Field label="Shell script" hint="Runs instead of an AI agent">
              <textarea rows={4} className="mono" value={f.executor_script} onChange={(e) => set("executor_script", e.target.value)} />
            </Field>
            <Field label="Check command" hint="Exit 0 = done">
              <input required className="mono" value={f.validation_cmd} onChange={(e) => set("validation_cmd", e.target.value)} />
            </Field>
          </>
        )}
      </div>
      <ErrorNote error={error} />
      <button className="btn primary">Queue task</button>
    </form>
  );
}

function RoleFields({
  label,
  nodes,
  binding,
  onChange,
  listId,
}: {
  label: string;
  nodes: Node[] | null;
  binding: { node_id: string; model: string };
  onChange: (b: { node_id: string; model: string }) => void;
  listId: string;
}) {
  const node = nodes?.find((n) => String(n.id) === binding.node_id);
  return (
    <>
      <Field label={`${label} node`}>
        <select value={binding.node_id} onChange={(e) => onChange({ ...binding, node_id: e.target.value })}>
          <option value="">— none —</option>
          {nodes?.map((n) => (
            <option key={n.id} value={n.id}>
              {n.name} {n.provider === "anthropic" ? "(Claude)" : ""}
            </option>
          ))}
        </select>
      </Field>
      <Field label={`${label} model`}>
        <input list={listId} value={binding.model} onChange={(e) => onChange({ ...binding, model: e.target.value })} />
        <datalist id={listId}>{node?.models.map((m) => <option key={m} value={m} />)}</datalist>
      </Field>
    </>
  );
}

function Settings({ project, onSaved }: { project: Project; onSaved: () => void }) {
  const { data: nodes } = usePoll<Node[]>("/api/nodes", 10000);
  // Older projects bound "planner" and "executor": each agent falls back to those.
  const b = (...keys: string[]) => {
    const x = keys.map((k) => project.role_bindings[k]).find((v) => v?.node_id) ?? { node_id: null, model: "" };
    return { node_id: x.node_id ? String(x.node_id) : "", model: x.model };
  };
  const [projectPlanner, setProjectPlanner] = useState(b("project_planner", "planner"));
  const [taskPlanner, setTaskPlanner] = useState(b("task_planner", "planner"));
  const [tester, setTester] = useState(b("tester", "executor"));
  const [builder, setBuilder] = useState(b("builder", "executor"));
  const [escalation, setEscalation] = useState(b("escalation"));
  const wd = project.watchdog_effective;
  const ge = project.gates_effective ?? {};
  const [w, setW] = useState({
    escalation: wd?.escalation ?? true,
    replan: wd?.replan ?? true,
    max_requests: wd?.max_requests ?? 60,
    max_tokens: wd?.max_tokens ?? 400000,
    max_diff_lines: wd?.max_diff_lines ?? 800,
    max_retries: wd?.max_retries ?? 2,
    repeat_limit: wd?.repeat_limit ?? 3,
    growth_cap_pct: wd?.growth_cap_pct ?? 50,
  });
  const [g, setG] = useState({
    task_merge: ge.task_merge ?? false,
    replan_approval: ge.replan_approval ?? false,
    milestone: ge.milestone ?? true,
  });
  const setWd = (k: string, v: unknown) => setW((x) => ({ ...x, [k]: v }));
  const [f, setF] = useState({
    test_cmd: project.test_cmd,
    executor_kind: project.executor_kind,
    network: project.network ?? "isolated",
    push_after_merge: project.push_after_merge,
    max_task_files: project.max_task_files,
    context_budget_pct: project.context_budget_pct,
    plan_approval: project.gates?.plan_approval !== false,
    spend_cap_usd: project.spend_cap_usd,
    min_task_coverage: project.min_task_coverage,
    test_paths: project.test_paths.join(", "),
  });
  const [error, setError] = useState("");
  const set = (k: string, v: unknown) => setF((x) => ({ ...x, [k]: v }));
  const toBinding = (x: { node_id: string; model: string }) => ({
    node_id: x.node_id ? Number(x.node_id) : null,
    model: x.model,
  });
  return (
    <form
      className="card form"
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          await patch(`/api/projects/${project.id}`, {
            test_cmd: f.test_cmd,
            executor_kind: f.executor_kind,
            network: f.network,
            push_after_merge: f.push_after_merge,
            max_task_files: f.max_task_files,
            context_budget_pct: f.context_budget_pct,
            spend_cap_usd: f.spend_cap_usd,
            min_task_coverage: f.min_task_coverage,
            test_paths: f.test_paths.split(",").map((x) => x.trim()).filter(Boolean),
            gates: { ...project.gates, plan_approval: f.plan_approval, ...g },
            watchdog: { ...project.watchdog, ...w },
            role_bindings: {
              project_planner: toBinding(projectPlanner),
              task_planner: toBinding(taskPlanner),
              tester: toBinding(tester),
              builder: toBinding(builder),
              escalation: toBinding(escalation),
            },
          });
          onSaved();
        } catch (err) {
          setError((err as Error).message);
        }
      }}
    >
      <h2>Settings</h2>
      <h3>Agents</h3>
      <p className="small muted">Which model each agent uses. Each agent can only do what its role allows.</p>
      <div className="grid2">
        <RoleFields label="Project Planner" nodes={nodes} binding={projectPlanner} onChange={setProjectPlanner} listId="pp-models" />
        <RoleFields label="Task Planner" nodes={nodes} binding={taskPlanner} onChange={setTaskPlanner} listId="tp-models" />
        <RoleFields label="Tester" nodes={nodes} binding={tester} onChange={setTester} listId="tester-models" />
        <RoleFields label="Builder" nodes={nodes} binding={builder} onChange={setBuilder} listId="builder-models" />
        <RoleFields label="Escalation" nodes={nodes} binding={escalation} onChange={setEscalation} listId="escalation-models" />
        <Field label="Coding agent" hint="Runs the Tester and Builder">
          <select value={f.executor_kind} onChange={(e) => set("executor_kind", e.target.value)}>
            <option>pi</option>
            <option>opencode</option>
            <option>aider</option>
            <option>shell</option>
          </select>
        </Field>
        <Field label="Executor network" hint="Isolated: agents reach only their model, through the gateway">
          <select value={f.network} onChange={(e) => set("network", e.target.value)}>
            <option value="isolated">isolated</option>
            <option value="open">open (LAN and internet)</option>
          </select>
        </Field>
      </div>
      <h3>Tasks</h3>
      <div className="grid2">
        <Field label="Max files per task">
          <input type="number" min={1} max={20} value={f.max_task_files} onChange={(e) => set("max_task_files", Number(e.target.value))} />
        </Field>
        <Field label="Context budget (% of executor window)">
          <input type="number" min={5} max={90} value={f.context_budget_pct} onChange={(e) => set("context_budget_pct", Number(e.target.value))} />
        </Field>
      </div>
      <h3>Tests</h3>
      <div className="grid2">
        <Field label="Minimum coverage of changed lines (%)" hint="Below it, the task goes back to the Tester once, then asks you. 0 = only report">
          <input type="number" min={0} max={100} value={f.min_task_coverage} onChange={(e) => set("min_task_coverage", Number(e.target.value))} />
        </Field>
        <Field label="Test paths" hint="Comma-separated. Only the Tester may change files here">
          <input className="mono" value={f.test_paths} onChange={(e) => set("test_paths", e.target.value)} />
        </Field>
        <Field label="Test command override" hint="Empty: the Tester's tests/run.sh">
          <input className="mono" value={f.test_cmd} onChange={(e) => set("test_cmd", e.target.value)} />
        </Field>
      </div>
      <h3>Spend</h3>
      <div className="grid2">
        <Field label="Spend cap (USD)" hint={`Paid (cloud) model calls stop here and ask you. 0 = no cap. Spent so far: $${project.spend_usd.toFixed(2)}`}>
          <input type="number" min={0} step={0.5} value={f.spend_cap_usd} onChange={(e) => set("spend_cap_usd", Number(e.target.value))} />
        </Field>
      </div>
      <h3>Watchdog (per task attempt)</h3>
      <div className="grid2">
        <Field label="Max model calls">
          <input type="number" min={1} value={w.max_requests} onChange={(e) => setWd("max_requests", Number(e.target.value))} />
        </Field>
        <Field label="Max tokens (in + out)">
          <input type="number" min={1000} step={1000} value={w.max_tokens} onChange={(e) => setWd("max_tokens", Number(e.target.value))} />
        </Field>
        <Field label="Max lines changed">
          <input type="number" min={1} value={w.max_diff_lines} onChange={(e) => setWd("max_diff_lines", Number(e.target.value))} />
        </Field>
        <Field label="Identical responses before stopping" hint="Also: identical failures before skipping retries">
          <input type="number" min={2} value={w.repeat_limit} onChange={(e) => setWd("repeat_limit", Number(e.target.value))} />
        </Field>
      </div>
      <h3>Autonomy</h3>
      <div className="grid2">
        <Field label="Escalate failures automatically" hint="retry → split → escalation model → ask you">
          <input type="checkbox" checked={w.escalation} onChange={(e) => setWd("escalation", e.target.checked)} />
        </Field>
        <Field label="Retries per level">
          <input type="number" min={0} max={10} value={w.max_retries} onChange={(e) => setWd("max_retries", Number(e.target.value))} />
        </Field>
        <Field label="Replan after each task">
          <input type="checkbox" checked={w.replan} onChange={(e) => setWd("replan", e.target.checked)} />
        </Field>
        <Field label="Tasks replanning may add (% of plan)" hint="Beyond this, revisions wait for you">
          <input type="number" min={0} max={500} value={w.growth_cap_pct} onChange={(e) => setWd("growth_cap_pct", Number(e.target.value))} />
        </Field>
      </div>
      <h3>Checkpoints</h3>
      <div className="grid2">
        <Field label="Review the task breakdown before it runs">
          <input type="checkbox" checked={f.plan_approval} onChange={(e) => set("plan_approval", e.target.checked)} />
        </Field>
        <Field label="Approve every plan revision">
          <input type="checkbox" checked={g.replan_approval} onChange={(e) => setG({ ...g, replan_approval: e.target.checked })} />
        </Field>
        <Field label="Approve each task's merge">
          <input type="checkbox" checked={g.task_merge} onChange={(e) => setG({ ...g, task_merge: e.target.checked })} />
        </Field>
        <Field label="Approve before pushing the finished milestone">
          <input type="checkbox" checked={g.milestone} onChange={(e) => setG({ ...g, milestone: e.target.checked })} />
        </Field>
        <Field label="Push milestone branch after each merge">
          <input type="checkbox" checked={f.push_after_merge} onChange={(e) => set("push_after_merge", e.target.checked)} />
        </Field>
      </div>
      <ErrorNote error={error} />
      <button className="btn primary">Save</button>
    </form>
  );
}

function Progress({ project }: { project: Project }) {
  const { data } = usePoll<ProgressPoint[]>(`/api/projects/${project.id}/progress`, 10000);
  const capped = project.spend_cap_usd > 0;
  return (
    <section className="card">
      <div className="row">
        <h2>Progress</h2>
        {(project.spend_usd > 0 || capped) && (
          <span className="small">
            Spend <b>${project.spend_usd.toFixed(2)}</b>
            {capped && <span className="muted"> of ${project.spend_cap_usd.toFixed(2)} cap</span>}
          </span>
        )}
      </div>
      <ProgressChart points={data ?? []} now={Date.now() / 1000} />
    </section>
  );
}

function Tests({ project }: { project: Project }) {
  const { data } = usePoll<{ suite: TestRun | null; acceptance: TestRun | null }>(`/api/projects/${project.id}/tests`, 10000);
  const { data: trend } = usePoll<CoveragePoint[]>(`/api/projects/${project.id}/coverage-trend`, 10000);
  const [all, setAll] = useState(false);
  const suite = data?.suite;
  const acceptance = data?.acceptance;
  const slowest = (suite?.cases ?? []).slice().sort((a, b) => b.duration_s - a.duration_s).slice(0, 5);
  return (
    <section className="card">
      <div className="row">
        <h2>Tests</h2>
        <span className="small muted">
          minimum {project.min_task_coverage}% of each task's changed lines covered
        </span>
      </div>
      {!suite ? (
        <p className="muted small">No suite results yet. The Tester sets up the test runner in the first task.</p>
      ) : (
        <>
          <div className="small muted">
            Latest suite: after task <Link to={`/tasks/${suite.task_id}`}>#{suite.task_id}</Link>, commit{" "}
            <span className="mono">{suite.commit.slice(0, 8)}</span>
          </div>
          <RunSummary run={suite} />
          <CoverageChart points={trend ?? []} />
          <div className="row">
            <h3>{all ? "All tests" : "Failing tests"}</h3>
            <button type="button" className="btn small" onClick={() => setAll((x) => !x)}>
              {all ? "Show failing only" : `Show all ${suite.cases?.length ?? 0}`}
            </button>
          </div>
          {all ? (
            <CaseList cases={suite.cases ?? []} />
          ) : suite.failed + suite.errors === 0 ? (
            <p className="small ok-text">Every test passes.</p>
          ) : (
            <CaseList cases={(suite.cases ?? []).filter((c) => c.status === "failed" || c.status === "error")} />
          )}
          {slowest.some((c) => c.duration_s > 0) && (
            <>
              <h3>Slowest tests</h3>
              <ul className="small">
                {slowest.map((c) => (
                  <li key={c.id}>
                    <span className="mono">{c.name}</span> <span className="muted">{c.duration_s.toFixed(2)}s</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
      {acceptance && (
        <>
          <h3>
            Acceptance tests{" "}
            <span className={`badge ${acceptance.failed + acceptance.errors ? "test-failed" : "test-passed"}`}>
              {acceptance.passed} of {acceptance.passed + acceptance.failed + acceptance.errors} passing
            </span>
          </h3>
          <CaseList cases={acceptance.cases ?? []} />
        </>
      )}
    </section>
  );
}

function Roles() {
  const { data } = usePoll<RoleInfo[]>("/api/roles", 600000);
  if (!data) return null;
  return (
    <section className="card">
      <h2>Agents and what they may do</h2>
      <p className="small muted">Fixed by Foreman. The Orchestrator refuses anything outside an agent's role and logs it.</p>
      <div className="roles">
        {data.map((r) => (
          <div key={r.key} className="role">
            <b>{r.name}</b>
            <div className="small muted">{r.summary}</div>
            <ul className="small may">
              {r.may.map((m) => <li key={m}>✓ {m}</li>)}
            </ul>
            <ul className="small may-not">
              {r.may_not.map((m) => <li key={m}>✗ {m}</li>)}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}

function Milestone({ project }: { project: Project }) {
  const checked = !!project.acceptance_commit;
  return (
    <section className={`card milestone ${project.phase === "done" ? "milestone-done" : ""}`}>
      <div className="row">
        <div>
          <b>Definition of done:</b>{" "}
          <code>{project.acceptance_cmd || "the acceptance tests (tests/acceptance/run.sh)"}</code>
        </div>
        <div className="small">
          {project.phase === "done" ? (
            <span className="badge state-done">milestone complete</span>
          ) : checked ? (
            <span className={`badge ${project.acceptance_passed ? "state-done" : "state-failed"}`}>
              last check {project.acceptance_passed ? "passed" : `failed (${project.acceptance_failures}×)`}
            </span>
          ) : (
            <span className="muted">runs when every task is done</span>
          )}
        </div>
      </div>
      {project.milestone_pr_url && (
        <div className="small">
          Pull request: <a href={project.milestone_pr_url}>{project.milestone_pr_url}</a>
        </div>
      )}
    </section>
  );
}

function Revisions({ projectId }: { projectId: number }) {
  const { data } = usePoll<PlanRevision[]>(`/api/projects/${projectId}/revisions`, 5000);
  const shown = (data ?? []).filter((r) => r.state !== "empty");
  if (!data || data.length === 0) return null;
  return (
    <section className="card">
      <h2>
        Plan revisions <span className="muted small">{data.length} review(s), {shown.length} with changes</span>
      </h2>
      {shown.length === 0 && <div className="muted small">The replanner has found the plan on track so far.</div>}
      {shown.map((r) => (
        <div key={r.id} className="revision">
          <div className="row">
            <b>
              v{r.version}: {r.summary || "changes"}
            </b>
            <span className={`badge revision-${r.state}`}>{r.state}</span>
          </div>
          <div className="small muted">after: {r.trigger}</div>
          <ul className="small">
            {r.ops.map((o, i) => (
              <li key={i}>
                <span className={`op op-${o.op}`}>{o.op}</span> <b>{o.key}</b>
                {o.op === "add" && o.task?.title ? ` (${o.task.title})` : ""} — {o.reason}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
}

const PHASES: { phase: ProjectPhase; label: string }[] = [
  { phase: "specify", label: "1. Specify" },
  { phase: "plan_review", label: "2. Review plan" },
  { phase: "execute", label: "3. Execute" },
  { phase: "done", label: "✓ Done" },
];

function PhaseStepper({ phase }: { phase: ProjectPhase }) {
  const current = phase === "decomposing" ? "plan_review" : phase;
  const idx = PHASES.findIndex((p) => p.phase === current);
  return (
    <div className="stepper">
      {PHASES.map((p, i) => (
        <span key={p.phase} className={`step ${i < idx ? "done" : i === idx ? "current" : ""}`}>
          {p.label}
          {p.phase === "plan_review" && phase === "decomposing" && " (planning…)"}
        </span>
      ))}
    </div>
  );
}

export default function ProjectPage() {
  const id = Number(useParams().id);
  const navigate = useNavigate();
  const { data: project, error, reload } = usePoll<Project>(`/api/projects/${id}`, 2000);
  const { data: tasks, reload: reloadTasks } = usePoll<Task[]>(`/api/projects/${id}/tasks`, 2000);
  const { events, connected } = useEventStream({ project_id: id });
  const [panel, setPanel] = useState<"" | "add" | "settings">("");

  if (!project) return <ErrorNote error={error} />;
  const all = tasks ?? [];
  const active = all.some((t) => t.state === "running" || t.state === "validating");
  const activity = events.filter((e) => e.kind !== "log" && e.kind !== "test");

  return (
    <div>
      <div className="page-head">
        <div>
          <div className="crumbs">
            <Link to="/">Projects</Link> /
          </div>
          <h1>
            {project.name} {project.paused && <span className="badge state-paused">paused</span>}
          </h1>
          <div className="muted mono small">
            {project.repo_url} → {project.milestone_branch}
          </div>
          <PhaseStepper phase={project.phase} />
        </div>
        <div className="actions">
          {(project.phase === "execute" || project.phase === "done") && (
            <button className="btn" onClick={() => setPanel(panel === "add" ? "" : "add")}>
              {panel === "add" ? "Cancel" : "Add task"}
            </button>
          )}
          <button className="btn" onClick={() => setPanel(panel === "settings" ? "" : "settings")}>
            Settings
          </button>
          {project.paused ? (
            <ActionButton kind="primary" onClick={async () => { await control("project", "resume", id); reload(); }}>
              Resume
            </ActionButton>
          ) : (
            <ActionButton onClick={async () => { await control("project", "pause", id); reload(); }}>
              Pause
            </ActionButton>
          )}
          <StopButton
            size="big"
            label="STOP PROJECT"
            onStop={async () => {
              await control("project", "stop", id);
              reload();
              reloadTasks();
            }}
          />
          <ActionButton
            kind="danger"
            disabled={active}
            onClick={async () => {
              if (!confirm(`Delete project "${project.name}" and its local clone?`)) return;
              await del(`/api/projects/${id}`);
              navigate("/");
            }}
          >
            Delete
          </ActionButton>
        </div>
      </div>

      {panel === "add" && (
        <AddTask project={project} tasks={all} onDone={() => { setPanel(""); reloadTasks(); }} />
      )}
      {panel === "settings" && (
        <>
          <Settings project={project} onSaved={() => { setPanel(""); reload(); }} />
          <Roles />
        </>
      )}

      {(project.phase === "specify" || project.phase === "decomposing") && <SpecWorkspace project={project} />}
      {project.phase === "plan_review" && <PlanReview project={project} onChange={() => { reload(); reloadTasks(); }} />}
      {(project.phase === "execute" || project.phase === "done") && (
      <>
      <Milestone project={project} />
      <Progress project={project} />
      <div className="board">
        {COLUMNS.map((col) => {
          const items = all.filter((t) => col.states.includes(t.state));
          return (
            <section key={col.title} className="column">
              <h3>
                {col.title} <span className="muted">{items.length}</span>
              </h3>
              {items.map((t) => <TaskCard key={t.id} t={t} all={all} />)}
            </section>
          );
        })}
      </div>

      <section className="card">
        <h2>
          Activity <span className={`dot ${connected ? "ok" : "bad"}`}>{connected ? "live" : "reconnecting"}</span>
        </h2>
        <EventLog events={activity} />
      </section>
      <Tests project={project} />
      <Revisions projectId={project.id} />
      </>
      )}
    </div>
  );
}
