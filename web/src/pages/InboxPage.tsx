import { useState } from "react";
import { Link } from "react-router-dom";
import { post } from "../api";
import { ErrorNote, fmtTime } from "../components";
import { usePoll } from "../hooks";
import type { Gate, GateKind } from "../types";

// What approving / rejecting means for each kind of checkpoint.
const ACTIONS: Record<GateKind, { label: string; approve: string; reject: string }> = {
  task_merge: { label: "Merge", approve: "Merge", reject: "Reject" },
  escalation: { label: "Stuck", approve: "Try again", reject: "Discard" },
  plan_revision: { label: "Plan change", approve: "Apply changes", reject: "Reject" },
  spec_change: { label: "Spec change", approve: "Update the documents", reject: "Dismiss" },
  milestone: { label: "Milestone", approve: "Push & open PR", reject: "Not now" },
  budget: { label: "Budget", approve: "Raise the cap", reject: "Pause project" },
  coverage: { label: "Under-tested", approve: "Merge anyway", reject: "Back to the Tester" },
};

function GateCard({ g, onDone }: { g: Gate; onDone: () => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const a = ACTIONS[g.kind];
  const decide = async (approve: boolean, action = "") => {
    setBusy(true);
    setError("");
    try {
      await post(`/api/gates/${g.id}/resolve`, { approve, note, action });
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className={`card gate gate-${g.kind}`}>
      <div className="row">
        <div>
          <span className={`badge gate-badge-${g.kind}`}>{a.label}</span>{" "}
          <Link to={`/projects/${g.project_id}`}>{g.project_name}</Link>
          {g.task_id && (
            <>
              {" "}
              · <Link to={`/tasks/${g.task_id}`}>task #{g.task_id}</Link>
            </>
          )}
        </div>
        <span className="muted small">{fmtTime(g.created_at)}</span>
      </div>
      <h3>{g.title}</h3>
      {g.detail && <pre className="gate-detail">{g.detail}</pre>}
      <ErrorNote error={error} />
      <div className="actions">
        <input className="grow" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
        <button className="btn" disabled={busy} onClick={() => decide(false)}>
          {a.reject}
        </button>
        {g.kind === "escalation" && g.task_id && (
          <button
            className="btn"
            disabled={busy}
            title="The tests may be wrong or unclear: the Tester writes them again"
            onClick={() => decide(true, "send_back")}
          >
            Back to the Tester
          </button>
        )}
        <button className="btn primary" disabled={busy} onClick={() => decide(true)}>
          {a.approve}
        </button>
      </div>
    </div>
  );
}

export default function InboxPage() {
  const { data: pending, error, reload } = usePoll<Gate[]>("/api/gates?state=pending", 3000);
  const { data: all } = usePoll<Gate[]>("/api/gates?state=all", 10000);
  const recent = (all ?? []).filter((g) => g.state !== "pending").slice(0, 15);
  return (
    <div>
      <div className="page-head">
        <h1>Needs you</h1>
      </div>
      <ErrorNote error={error} />
      {pending?.length === 0 && (
        <p className="muted">Nothing is waiting for you. Agents keep working until something needs a decision.</p>
      )}
      {pending?.map((g) => <GateCard key={g.id} g={g} onDone={reload} />)}
      {recent.length > 0 && (
        <section className="card">
          <h2>Recent decisions</h2>
          <table className="runs">
            <tbody>
              {recent.map((g) => (
                <tr key={g.id}>
                  <td className="small muted">{fmtTime(g.resolved_at)}</td>
                  <td>
                    <span className={`badge gate-badge-${g.kind}`}>{ACTIONS[g.kind].label}</span>
                  </td>
                  <td>{g.title}</td>
                  <td className="small">{g.project_name}</td>
                  <td className={`small decision-${g.state}`}>{g.state}</td>
                  <td className="small muted">{g.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}
