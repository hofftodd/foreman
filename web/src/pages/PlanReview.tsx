import { useState } from "react";
import { patch, post } from "../api";
import { ActionButton, ErrorNote, Field, lines } from "../components";
import { usePoll } from "../hooks";
import type { Project, Task } from "../types";

const KIND_LABEL: Record<Task["kind"], string> = {
  setup: "standard task: setup",
  acceptance: "standard task: acceptance tests",
  feature: "",
  docs: "standard task: documentation",
};

function TaskEditor({ t, onSaved }: { t: Task; onSaved: () => void }) {
  const [f, setF] = useState({ title: t.title, description: t.description, criteria: t.criteria.join("\n") });
  const [error, setError] = useState("");
  const set = (k: string, v: string) => setF((x) => ({ ...x, [k]: v }));
  return (
    <form
      className="form task-editor"
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          await patch(`/api/tasks/${t.id}`, {
            title: f.title,
            description: f.description,
            criteria: lines(f.criteria),
          });
          onSaved();
        } catch (err) {
          setError((err as Error).message);
        }
      }}
    >
      <div className="grid2">
        <Field label="Title">
          <input value={f.title} onChange={(e) => set("title", e.target.value)} />
        </Field>
        <Field label="Goal">
          <textarea rows={3} value={f.description} onChange={(e) => set("description", e.target.value)} />
        </Field>
        <Field label="Done when" hint="Plain-language criteria, one per line; the Tester turns them into tests">
          <textarea rows={4} value={f.criteria} onChange={(e) => set("criteria", e.target.value)} />
        </Field>
      </div>
      <ErrorNote error={error} />
      <button className="btn primary">Save &amp; re-check size</button>
    </form>
  );
}

function ProposedTask({ t, all, reload }: { t: Task; all: Task[]; reload: () => void }) {
  const [editing, setEditing] = useState(false);
  const deps = t.deps.map((d) => all.find((x) => x.id === d)?.key || `#${d}`);
  const tooBig = t.size_violations.length > 0;
  return (
    <div className={`card proposed ${tooBig ? "too-big" : ""}`}>
      <div className="row">
        <h3>
          <span className="muted">{t.key || `#${t.id}`}</span> {t.title}{" "}
          {KIND_LABEL[t.kind] && <span className="badge kind-badge">{KIND_LABEL[t.kind]}</span>}
        </h3>
        <div className="actions">
          <button className="btn" onClick={() => setEditing((x) => !x)}>
            {editing ? "Close" : "Edit"}
          </button>
          <ActionButton
            kind="danger"
            onClick={async () => {
              await post(`/api/tasks/${t.id}/discard`);
              reload();
            }}
          >
            Drop
          </ActionButton>
        </div>
      </div>
      {tooBig && (
        <div className="error-note">
          Too big: {t.size_violations.join("; ")}. Edit or split it.
        </div>
      )}
      {editing ? (
        <TaskEditor
          t={t}
          onSaved={() => {
            setEditing(false);
            reload();
          }}
        />
      ) : (
        <div className="proposed-body">
          {t.description && <p className="pre">{t.description}</p>}
          {t.criteria.length > 0 && (
            <>
              <div className="small">
                <b>Done when:</b>
              </div>
              <ul className="small criteria">
                {t.criteria.map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            </>
          )}
          <div className="small">
            {t.req_refs.length > 0 && (
              <div>
                <b>Implements:</b> {t.req_refs.join(", ")}
              </div>
            )}
            {deps.length > 0 && (
              <div>
                <b>After:</b> {deps.join(", ")}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default function PlanReview({ project, onChange }: { project: Project; onChange: () => void }) {
  const { data: tasks, reload } = usePoll<Task[]>(`/api/projects/${project.id}/tasks`, 3000);
  const [error, setError] = useState("");
  const proposed = (tasks ?? []).filter((t) => t.state === "proposed");
  const blocked = proposed.filter((t) => t.size_violations.length > 0);
  const refresh = () => {
    reload();
    onChange();
  };

  return (
    <div>
      <section className="card review-head">
        <div>
          <h2>Review the plan</h2>
          <p className="muted small">
            The Task Planning Agent broke the approved documents into {proposed.length} tasks, in plain language. For
            each one the Tester Agent writes the tests from its criteria, then the Builder Agent writes the code until
            they pass. Nothing runs until you approve.
          </p>
        </div>
        <div className="actions">
          <ActionButton
            onClick={async () => {
              await post(`/api/projects/${project.id}/spec/reopen`);
              refresh();
            }}
          >
            Back to documents
          </ActionButton>
          <ActionButton
            onClick={async () => {
              await post(`/api/projects/${project.id}/plan/redecompose`);
              refresh();
            }}
          >
            Re-run breakdown
          </ActionButton>
          <ActionButton
            kind="primary"
            disabled={proposed.length === 0 || blocked.length > 0}
            onClick={async () => {
              setError("");
              try {
                await post(`/api/projects/${project.id}/plan/approve`);
                refresh();
              } catch (e) {
                setError((e as Error).message);
              }
            }}
          >
            Approve plan &amp; start
          </ActionButton>
        </div>
      </section>
      <ErrorNote error={error} />
      {blocked.length > 0 && (
        <div className="error-note">
          {blocked.length} task(s) are too big. Edit them, drop them, or re-run the breakdown.
        </div>
      )}
      {proposed.map((t) => (
        <ProposedTask key={t.id} t={t} all={tasks ?? []} reload={reload} />
      ))}
    </div>
  );
}
