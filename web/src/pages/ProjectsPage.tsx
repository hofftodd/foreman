import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { post } from "../api";
import { ErrorNote, Field } from "../components";
import { usePoll } from "../hooks";
import type { GitHubTokenStatus, Node, Project, SystemInfo } from "../types";

const PHASE_LABEL: Record<Project["phase"], string> = {
  specify: "specifying",
  decomposing: "planning",
  plan_review: "plan review",
  execute: "executing",
  done: "done",
};

function planned(p: Project) {
  const c = p.task_counts ?? {};
  const total = Object.entries(c).reduce((n, [k, v]) => (k === "discarded" || k === "split" ? n : n + (v ?? 0)), 0);
  return { done: c.done ?? 0, total };
}

function Portfolio({ projects, nodes }: { projects: Project[]; nodes: Node[] }) {
  const running = projects.filter((p) => p.active_task).length;
  const waiting = projects.reduce((n, p) => n + (p.pending_gates ?? 0), 0);
  const spend = projects.reduce((n, p) => n + p.spend_usd, 0);
  const busy = nodes.reduce((n, x) => n + x.active, 0);
  const slots = nodes.reduce((n, x) => n + x.capacity, 0);
  const tiles = [
    { label: "projects working", value: `${running} / ${projects.length}` },
    { label: "need you", value: String(waiting), to: "/inbox" },
    { label: "node slots busy", value: `${busy} / ${slots}`, to: "/nodes" },
    { label: "cloud spend", value: `$${spend.toFixed(2)}` },
  ];
  return (
    <div className="stats">
      {tiles.map((t) => {
        const body = (
          <>
            <div className="stat-value">{t.value}</div>
            <div className="stat-label">{t.label}</div>
          </>
        );
        return t.to ? (
          <Link key={t.label} to={t.to} className="stat">
            {body}
          </Link>
        ) : (
          <div key={t.label} className="stat">
            {body}
          </div>
        );
      })}
    </div>
  );
}

function Counts({ p }: { p: Project }) {
  const c = p.task_counts ?? {};
  const active = (c.running ?? 0) + (c.validating ?? 0);
  return (
    <div className="counts">
      <span>{c.done ?? 0} done</span>
      <span className={active ? "hot" : ""}>{active} running</span>
      <span>{c.queued ?? 0} queued</span>
      {(c.failed ?? 0) > 0 && <span className="warn">{c.failed} failed</span>}
      {(c.stopped_by_user ?? 0) > 0 && <span className="warn">{c.stopped_by_user} stopped</span>}
      {(c.blocked ?? 0) > 0 && <span className="warn">{c.blocked} blocked</span>}
    </div>
  );
}

function ProgressBar({ p }: { p: Project }) {
  const { done, total } = planned(p);
  if (!total) return null;
  return (
    <div title={`${done} of ${total} tasks done`}>
      <div className="progress">
        <div className="progress-fill" style={{ width: `${(done / total) * 100}%` }} />
      </div>
      <div className="small muted">
        {done} of {total} tasks done
        {p.coverage != null && ` · ${Math.round(p.coverage)}% test coverage`}
        {p.spend_usd > 0 && ` · $${p.spend_usd.toFixed(2)} spent`}
      </div>
    </div>
  );
}

// GitHub's rules for repository names, applied to a project name as the default.
const repoSlug = (name: string) =>
  name
    .trim()
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "");

interface CreatedRepo {
  full_name: string;
  clone_url: string;
  html_url: string;
}

function AddProject({ onDone }: { onDone: (p: Project) => void }) {
  const { data: nodes } = usePoll<Node[]>("/api/nodes", 10000);
  const { data: sys } = usePoll<SystemInfo>("/api/system", 10000);
  const [form, setForm] = useState({
    name: "",
    repo_url: "",
    milestone_branch: "foreman/m1",
    test_cmd: "",
    executor_kind: "pi",
    node_id: "",
    model: "",
    planner_node_id: "",
    planner_model: "",
    push_after_merge: false,
    skip_spec: false,
    repo_mode: "existing" as "existing" | "new",
    new_owner: "",
    new_name: "",
    new_private: true,
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const creating = form.repo_mode === "new";
  const { data: gh } = usePoll<GitHubTokenStatus>("/api/settings/github", 30000);
  const { data: owners, error: ownersError } = usePoll<string[]>(
    creating && gh?.configured ? "/api/github/owners" : null,
    60000,
  );
  const owner = form.new_owner || owners?.[0] || "";
  const repoName = form.new_name || repoSlug(form.name);
  const set = (k: string, v: unknown) => setForm((f) => ({ ...f, [k]: v }));
  const node = nodes?.find((n) => String(n.id) === form.node_id);
  const plannerNode = nodes?.find((n) => String(n.id) === form.planner_node_id);

  return (
    <form
      className="card form"
      onSubmit={async (e) => {
        e.preventDefault();
        setError("");
        try {
          let repoUrl = form.repo_url;
          if (creating) {
            setBusy("Creating repository…");
            const repo = await post<CreatedRepo>("/api/github/repos", {
              owner,
              name: repoName,
              private: form.new_private,
            });
            repoUrl = repo.clone_url;
            // The repository exists now: a retry after a failure below must not create it again.
            setForm((f) => ({ ...f, repo_mode: "existing", repo_url: repo.clone_url }));
          }
          setBusy("Cloning…");
          const p = await post<Project>("/api/projects", {
            name: form.name,
            repo_url: repoUrl,
            milestone_branch: form.milestone_branch,
            test_cmd: form.test_cmd,
            executor_kind: form.executor_kind,
            push_after_merge: form.push_after_merge,
            skip_spec: form.skip_spec,
            role_bindings: {
              ...(form.node_id ? { executor: { node_id: Number(form.node_id), model: form.model } } : {}),
              ...(form.planner_node_id
                ? { planner: { node_id: Number(form.planner_node_id), model: form.planner_model } }
                : {}),
            },
          });
          onDone(p);
        } catch (err) {
          setError((err as Error).message);
        } finally {
          setBusy("");
        }
      }}
    >
      <h2>Add project</h2>
      <div className="grid2">
        <Field label="Name">
          <input required value={form.name} onChange={(e) => set("name", e.target.value)} />
        </Field>
        <Field label="Repository">
          <select value={form.repo_mode} onChange={(e) => set("repo_mode", e.target.value)}>
            <option value="existing">Use an existing repository</option>
            <option value="new">Create a new GitHub repository</option>
          </select>
        </Field>
        {creating ? (
          gh && !gh.configured ? (
            <div className="field">
              <ErrorNote error="Creating a repository needs a GitHub token." />
              <Link to="/settings">Add one in Settings</Link>
            </div>
          ) : (
            <>
              <Field label="Owner" hint="Your account or an organisation the token can create repositories in">
                <select value={owner} onChange={(e) => set("new_owner", e.target.value)}>
                  {owners?.map((o) => (
                    <option key={o}>{o}</option>
                  ))}
                </select>
                <ErrorNote error={ownersError} />
              </Field>
              <Field label="Repository name" hint="Created with a README on main">
                <input
                  required
                  pattern="[A-Za-z0-9._\-]+"
                  value={repoName}
                  onChange={(e) => set("new_name", e.target.value)}
                />
              </Field>
              <Field label="Private repository">
                <input type="checkbox" checked={form.new_private} onChange={(e) => set("new_private", e.target.checked)} />
              </Field>
            </>
          )
        ) : (
          <Field label="GitHub repository" hint="https://github.com/owner/repo.git (or any git URL)">
            <input required value={form.repo_url} onChange={(e) => set("repo_url", e.target.value)} />
          </Field>
        )}
        <Field label="Milestone branch" hint="Finished tasks merge here">
          <input value={form.milestone_branch} onChange={(e) => set("milestone_branch", e.target.value)} />
        </Field>
        <Field label="Project test command" hint="Run after every task to catch regressions (optional)">
          <input value={form.test_cmd} placeholder="e.g. pytest -q" onChange={(e) => set("test_cmd", e.target.value)} />
        </Field>
        <Field label="Executor">
          <select value={form.executor_kind} onChange={(e) => set("executor_kind", e.target.value)}>
            {(sys?.executors ?? ["pi", "opencode", "shell"]).map((x) => (
              <option key={x}>{x}</option>
            ))}
          </select>
        </Field>
        <Field label="Planner node" hint="Writes the spec with you and breaks it into tasks (local or Claude)">
          <select value={form.planner_node_id} onChange={(e) => set("planner_node_id", e.target.value)}>
            <option value="">— none —</option>
            {nodes?.map((n) => (
              <option key={n.id} value={n.id}>
                {n.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Planner model">
          <input list="planner-models" value={form.planner_model} onChange={(e) => set("planner_model", e.target.value)} />
          <datalist id="planner-models">
            {plannerNode?.models.map((m) => <option key={m} value={m} />)}
          </datalist>
        </Field>
        <Field label="Executor node" hint="Runs the coding agent (its key never enters the sandbox)">
          <select value={form.node_id} onChange={(e) => set("node_id", e.target.value)}>
            <option value="">— none —</option>
            {nodes?.map((n) => (
              <option key={n.id} value={n.id}>
                {n.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Executor model">
          <input list="models" value={form.model} onChange={(e) => set("model", e.target.value)} />
          <datalist id="models">
            {node?.models.map((m) => <option key={m} value={m} />)}
          </datalist>
        </Field>
        <Field label="Push milestone branch after each merge">
          <input type="checkbox" checked={form.push_after_merge} onChange={(e) => set("push_after_merge", e.target.checked)} />
        </Field>
        <Field label="Skip the spec phase" hint="Add tasks by hand instead of planning with the planner">
          <input type="checkbox" checked={form.skip_spec} onChange={(e) => set("skip_spec", e.target.checked)} />
        </Field>
      </div>
      <ErrorNote error={error} />
      <button className="btn primary" disabled={!!busy || (creating && (!gh?.configured || !owner))}>
        {busy || (creating ? "Create repository and add project" : "Add project")}
      </button>
    </form>
  );
}

export default function ProjectsPage() {
  const { data: projects, error } = usePoll<Project[]>("/api/projects", 3000);
  const { data: nodes } = usePoll<Node[]>("/api/nodes", 5000);
  const [adding, setAdding] = useState(false);
  const navigate = useNavigate();

  return (
    <div>
      <div className="page-head">
        <h1>Projects</h1>
        <button className="btn primary" onClick={() => setAdding((a) => !a)}>
          {adding ? "Cancel" : "Add project"}
        </button>
      </div>
      <ErrorNote error={error} />
      {adding && <AddProject onDone={(p) => navigate(`/projects/${p.id}`)} />}
      {!!projects?.length && <Portfolio projects={projects} nodes={nodes ?? []} />}
      {projects?.length === 0 && !adding && <p className="muted">No projects yet. Add one to get started.</p>}
      <div className="cards">
        {projects?.map((p) => (
          <Link key={p.id} to={`/projects/${p.id}`} className="card project-card">
            <div className="row">
              <h3>{p.name}</h3>
              <span>
                <span className={`badge phase-${p.phase}`}>{PHASE_LABEL[p.phase]}</span>{" "}
                {p.paused && <span className="badge state-paused">paused</span>}
              </span>
            </div>
            <div className="muted mono small">{p.repo_url}</div>
            <div className="muted small">→ {p.milestone_branch}</div>
            <ProgressBar p={p} />
            {p.active_task && <div className="small">▶ {p.active_task}</div>}
            <Counts p={p} />
            {(p.pending_gates ?? 0) > 0 && <div className="small warn">needs you: {p.pending_gates}</div>}
          </Link>
        ))}
      </div>
    </div>
  );
}
