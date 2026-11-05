import { useEffect, useMemo, useRef, useState } from "react";
import { post, put } from "../api";
import { ActionButton, ErrorNote, fmtTime } from "../components";
import { useEventStream, usePoll } from "../hooks";
import type { DocName, Project, SpecState } from "../types";

const DOCS: { name: DocName; label: string }[] = [
  { name: "requirements", label: "Requirements" },
  { name: "design", label: "Design" },
  { name: "acceptance", label: "Acceptance criteria" },
  { name: "tasks", label: "Tasks" },
];
const PLANNING: DocName[] = ["requirements", "design", "acceptance"];

/** Live planner output for the active job, assembled from streamed events: the reply,
 * and the reasoning a thinking model streams before (or while) it writes one. */
function usePlannerStream(projectId: number, jobId: number | undefined) {
  const { events } = useEventStream({ project_id: projectId });
  return useMemo(() => {
    const out = { text: "", thinking: "", research: [] as string[] };
    if (jobId === undefined) return out;
    for (const e of events) {
      if (e.data.job_id !== jobId) continue;
      if (e.kind === "planner") out.text += e.message;
      else if (e.kind === "planner_thinking") out.thinking += e.message;
      else if (e.kind === "research") out.research.push(e.message);
    }
    return out;
  }, [events, jobId]);
}

// How much of the reasoning to show while it streams: the latest part is the useful one.
const THINKING_TAIL = 1200;

function Thinking({ text, answering }: { text: string; answering: boolean }) {
  if (!text) return null;
  const words = text.trim().split(/\s+/).length;
  if (!answering) {
    return (
      <div className="thinking">
        <div className="msg-meta">Thinking… ({words.toLocaleString()} words so far)</div>
        <div className="thinking-body">{text.length > THINKING_TAIL ? "…" + text.slice(-THINKING_TAIL) : text}</div>
      </div>
    );
  }
  return (
    <details className="thinking">
      <summary className="msg-meta">Thought for {words.toLocaleString()} words</summary>
      <div className="thinking-body">{text}</div>
    </details>
  );
}

/** Hide <<<FILE ...>>> bodies while streaming; show which file is being written. Mirrors
 * the server's lenient parser: the path may be on the next line, and a block may end at
 * the next <<<FILE / <<<ACCEPTANCE_CMD instead of >>>. */
function summarizeStream(text: string): string {
  const out: string[] = [];
  const lines = text.split("\n");
  let open: { kind: "file" | "cmd"; label: string; body: string[] } | null = null;
  const close = (done: boolean) => {
    if (!open) return;
    out.push(
      open.kind === "file"
        ? done
          ? `[wrote ${open.label}]`
          : `[writing ${open.label}…]`
        : `[acceptance command: ${open.body.join(" ").trim()}]`,
    );
    open = null;
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const file = /^<<<FILE(?:[ \t]+(.*?))?[ \t]*$/.exec(line);
    if (file) {
      close(true);
      let path = (file[1] ?? "").trim();
      const next = (lines[i + 1] ?? "").trim();
      if (!path && next && !/\s/.test(next)) {
        path = next;
        i++;
      }
      if (path) open = { kind: "file", label: path, body: [] };
    } else if (/^<<<ACCEPTANCE_CMD[ \t]*$/.test(line)) {
      close(true);
      open = { kind: "cmd", label: "", body: [] };
    } else if (open && line.trim() === ">>>") {
      close(true);
    } else if (open) {
      open.body.push(line);
    } else {
      out.push(line);
    }
  }
  close(false);
  return out.join("\n");
}

function Chat({ spec, project, reload }: { spec: SpecState; project: Project; reload: () => void }) {
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const active = spec.active_job;
  const streaming = usePlannerStream(project.id, active?.id);
  const bottom = useRef<HTMLDivElement>(null);
  const canChat = spec.phase === "specify" && !active;

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [spec.messages.length, streaming.text.length, streaming.thinking.length]);

  async function send() {
    if (!text.trim()) return;
    setError("");
    try {
      await post(`/api/projects/${project.id}/spec/messages`, { content: text });
      setText("");
      reload();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <section className="card chat">
      <h2>
        Project Planning Agent
        {active && <span className="badge state-running">{active.kind === "chat" ? "thinking…" : "breaking down tasks…"}</span>}
      </h2>
      <div className="chat-log">
        {spec.messages.length === 0 && !active && (
          <div className="muted chat-empty">
            Describe what you want to build. The planner asks questions, researches what it doesn't know, and
            writes the requirements, design and acceptance criteria with you, in plain language. No code yet: the
            Tester and Builder agents take it from there.
          </div>
        )}
        {spec.messages.map((m) => (
          <div key={m.id} className={`msg msg-${m.role}`}>
            <div className="msg-meta">
              {m.role === "user" ? "You" : "Planner"} · {fmtTime(m.ts)}
            </div>
            <div className="msg-body">{m.content}</div>
            {m.files.length > 0 && (
              <div className="msg-files">
                {m.files.map((f) => (
                  <code key={f}>{f}</code>
                ))}
              </div>
            )}
          </div>
        ))}
        {active && (
          <div className="msg msg-assistant streaming">
            <div className="msg-meta">Planner · {active.model || "…"}</div>
            {streaming.research.length > 0 && (
              <div className="research small muted">
                {streaming.research.map((r, i) => (
                  <div key={i}>🔎 {r}</div>
                ))}
              </div>
            )}
            <Thinking text={streaming.thinking} answering={!!streaming.text} />
            {(streaming.text || !streaming.thinking) && (
              <div className="msg-body">{summarizeStream(streaming.text) || "…"}</div>
            )}
          </div>
        )}
        <div ref={bottom} />
      </div>
      <ErrorNote error={error} />
      <div className="chat-input">
        <textarea
          rows={3}
          value={text}
          disabled={!canChat}
          placeholder={
            spec.phase !== "specify"
              ? "The documents are approved. Reopen them to keep talking."
              : active
                ? "Waiting for the planner…"
                : "Message the planner (Ctrl+Enter to send)"
          }
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send();
          }}
        />
        <button className="btn primary" disabled={!canChat || !text.trim()} onClick={send}>
          Send
        </button>
      </div>
    </section>
  );
}

function Docs({ spec, project, reload }: { spec: SpecState; project: Project; reload: () => void }) {
  const [tab, setTab] = useState<DocName>("requirements");
  const [editing, setEditing] = useState<string | null>(null);
  const [error, setError] = useState("");
  const editable = spec.phase === "specify" && tab !== "tasks" && !spec.active_job;
  const content = spec.docs[tab];

  return (
    <section className="card docs">
      <div className="tabs">
        {DOCS.map((d) => (
          <button
            key={d.name}
            className={tab === d.name ? "active" : ""}
            onClick={() => {
              setTab(d.name);
              setEditing(null);
            }}
          >
            {d.label}
            {!spec.docs[d.name] && <span className="muted"> ·</span>}
          </button>
        ))}
        <span className="muted small mono">{spec.paths[tab]}</span>
      </div>
      {editing !== null ? (
        <>
          <textarea className="doc-editor mono" value={editing} onChange={(e) => setEditing(e.target.value)} />
          <ErrorNote error={error} />
          <div className="actions">
            <ActionButton
              kind="primary"
              onClick={async () => {
                try {
                  await put(`/api/projects/${project.id}/spec/docs/${tab}`, { content: editing });
                  setEditing(null);
                  setError("");
                  reload();
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
            >
              Save &amp; commit
            </ActionButton>
            <button className="btn" onClick={() => setEditing(null)}>
              Cancel
            </button>
          </div>
        </>
      ) : (
        <>
          {content ? <pre className="doc">{content}</pre> : <div className="muted doc-empty">Not written yet.</div>}
          {editable && (
            <div className="actions">
              <button className="btn" onClick={() => setEditing(content)}>
                Edit
              </button>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function Approve({ spec, project, reload }: { spec: SpecState; project: Project; reload: () => void }) {
  const [error, setError] = useState("");
  const missing = PLANNING.filter((d) => !spec.docs[d].trim()).map((d) => DOCS.find((x) => x.name === d)?.label);

  return (
    <section className="card approve">
      <h2>Definition of done</h2>
      <p className="small">
        The acceptance criteria are the definition of done. Once you approve the documents, the Task Planning Agent
        breaks the work into tasks, and the Tester Agent turns the criteria into acceptance tests.
      </p>
      {spec.phase === "specify" && (
        <>
          {missing.length > 0 && <div className="small muted">Still missing: {missing.join(", ")}.</div>}
          <ErrorNote error={error} />
          <ActionButton
            kind="primary"
            disabled={missing.length > 0 || !!spec.active_job}
            onClick={async () => {
              setError("");
              try {
                await post(`/api/projects/${project.id}/spec/approve`);
                reload();
              } catch (e) {
                setError((e as Error).message);
              }
            }}
          >
            Approve documents &amp; break into tasks
          </ActionButton>
        </>
      )}
      {spec.phase === "decomposing" && (
        <div className="small">Approved. The Task Planning Agent is breaking the work into tasks…</div>
      )}
    </section>
  );
}

export default function SpecWorkspace({ project }: { project: Project }) {
  const { data: spec, error, reload } = usePoll<SpecState>(`/api/projects/${project.id}/spec`, 1500);
  if (!spec) return <ErrorNote error={error} />;
  return (
    <div className="spec-grid">
      <Chat spec={spec} project={project} reload={reload} />
      <div className="spec-side">
        <Approve spec={spec} project={project} reload={reload} />
        <Docs spec={spec} project={project} reload={reload} />
      </div>
    </div>
  );
}
