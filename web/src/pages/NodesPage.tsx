import { useState } from "react";
import { del, patch, post } from "../api";
import { ActionButton, ErrorNote, Field, fmtTime } from "../components";
import { usePoll } from "../hooks";
import type { Node } from "../types";

export default function NodesPage() {
  const { data: nodes, error, reload } = usePoll<Node[]>("/api/nodes", 5000);
  const empty = {
    name: "",
    provider: "openai",
    base_url: "",
    api_key: "",
    capacity: 1,
    context_window: 32768,
    price_in: 0,
    price_out: 0,
  };
  const [f, setF] = useState(empty);
  const [formError, setFormError] = useState("");
  const [checkErrors, setCheckErrors] = useState<Record<number, string>>({});

  return (
    <div>
      <div className="page-head">
        <h1>Inference nodes</h1>
      </div>
      <p className="muted">
        Local OpenAI-compatible servers (llama.cpp, LM Studio, Ollama, MLX, vLLM…) or the Claude API. Foreman connects
        to them from this machine; agents reach them only through its gateway, which holds the API keys.
      </p>
      <ErrorNote error={error} />
      <div className="cards">
        {nodes?.map((n) => (
          <div key={n.id} className="card">
            <div className="row">
              <h3>{n.name}</h3>
              <span className={`dot ${n.healthy ? "ok" : n.healthy === false ? "bad" : ""}`}>
                {n.healthy == null ? "unchecked" : n.healthy ? "healthy" : "unreachable"}
              </span>
            </div>
            <div className="mono small">{n.base_url}</div>
            <div className="small muted">
              {n.provider === "anthropic" ? "Claude API" : "OpenAI-compatible"} · capacity {n.capacity} ·{" "}
              {n.context_window.toLocaleString()} ctx · checked {fmtTime(n.last_checked)}{" "}
              {n.has_api_key && "· api key set"}
            </div>
            <NodeLoad n={n} />
            {(n.price_in > 0 || n.price_out > 0) && (
              <div className="small">
                ${n.price_in.toFixed(2)} / ${n.price_out.toFixed(2)} per million tokens in / out
              </div>
            )}
            <div className="small">{n.models.length ? n.models.join(", ") : <span className="muted">no models listed</span>}</div>
            <ErrorNote error={checkErrors[n.id] ?? ""} />
            <CapacityEditor node={n} onSaved={reload} />
            <div className="actions">
              <ActionButton
                onClick={async () => {
                  const r = await post<Node>(`/api/nodes/${n.id}/check`);
                  setCheckErrors((e) => ({ ...e, [n.id]: r.error ?? "" }));
                  reload();
                }}
              >
                Check
              </ActionButton>
              <ActionButton kind="danger" onClick={async () => { if (confirm(`Remove ${n.name}?`)) { await del(`/api/nodes/${n.id}`); reload(); } }}>
                Remove
              </ActionButton>
            </div>
          </div>
        ))}
      </div>
      <form
        className="card form"
        onSubmit={async (e) => {
          e.preventDefault();
          setFormError("");
          try {
            const n = await post<Node>("/api/nodes", f);
            await post(`/api/nodes/${n.id}/check`);
            setF(empty);
            reload();
          } catch (err) {
            setFormError((err as Error).message);
          }
        }}
      >
        <h2>Add node</h2>
        <div className="grid2">
          <Field label="Name">
            <input required value={f.name} placeholder="mac-studio" onChange={(e) => setF({ ...f, name: e.target.value })} />
          </Field>
          <Field label="Provider" hint="Either can serve any role; Claude works with the OpenCode executor">
            <select
              value={f.provider}
              onChange={(e) =>
                setF({
                  ...f,
                  provider: e.target.value,
                  base_url: e.target.value === "anthropic" ? "https://api.anthropic.com" : "",
                  context_window: e.target.value === "anthropic" ? 1000000 : 32768,
                })
              }
            >
              <option value="openai">OpenAI-compatible (llama.cpp, LM Studio, Ollama, MLX…)</option>
              <option value="anthropic">Anthropic (Claude API)</option>
            </select>
          </Field>
          <Field label="Base URL" hint={f.provider === "openai" ? "Including /v1" : "Claude API base URL"}>
            <input required className="mono" value={f.base_url} placeholder="http://192.168.1.20:8080/v1" onChange={(e) => setF({ ...f, base_url: e.target.value })} />
          </Field>
          <Field label="Context window (tokens)" hint="What the model can actually use; sizes task context packs">
            <input type="number" min={1024} value={f.context_window} onChange={(e) => setF({ ...f, context_window: Number(e.target.value) })} />
          </Field>
          <Field label="API key" hint="Optional">
            <input type="password" value={f.api_key} onChange={(e) => setF({ ...f, api_key: e.target.value })} />
          </Field>
          <Field label="Capacity" hint="Concurrent tasks and planner calls this node can serve">
            <input type="number" min={1} value={f.capacity} onChange={(e) => setF({ ...f, capacity: Number(e.target.value) })} />
          </Field>
          <Field label="Price in (USD / million tokens)" hint="0 for local nodes; priced nodes count against spend caps">
            <input type="number" min={0} step="any" value={f.price_in} onChange={(e) => setF({ ...f, price_in: Number(e.target.value) })} />
          </Field>
          <Field label="Price out (USD / million tokens)">
            <input type="number" min={0} step="any" value={f.price_out} onChange={(e) => setF({ ...f, price_out: Number(e.target.value) })} />
          </Field>
        </div>
        <ErrorNote error={formError} />
        <button className="btn primary">Add node</button>
      </form>
    </div>
  );
}

function NodeLoad({ n }: { n: Node }) {
  const pct = Math.min(100, (n.active / n.capacity) * 100);
  return (
    <div className="node-load">
      <div className="meter-label">
        <span>
          {n.active} of {n.capacity} slot{n.capacity === 1 ? "" : "s"} busy
        </span>
        <span className="muted">{n.waiting ? `${n.waiting} waiting` : "nothing waiting"}</span>
      </div>
      <div className="meter-bar">
        <div className={`meter-fill ${pct >= 100 ? "warm" : ""}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

// Edits a node's capacity in place. The scheduler re-reads nodes every tick, so a change
// applies to the next task it starts; lowering it never stops tasks already running.
function CapacityEditor({ node, onSaved }: { node: Node; onSaved: () => void }) {
  const [value, setValue] = useState(node.capacity);
  const [error, setError] = useState("");
  return (
    <form
      className="row"
      onSubmit={async (e) => {
        e.preventDefault();
        setError("");
        try {
          await patch(`/api/nodes/${node.id}`, { capacity: value });
          onSaved();
        } catch (err) {
          setError((err as Error).message);
        }
      }}
    >
      <label className="small muted">
        Capacity{" "}
        <input type="number" min={1} max={64} value={value} onChange={(e) => setValue(Number(e.target.value))} style={{ width: 64 }} />
      </label>
      <button className="btn" disabled={value === node.capacity}>
        Save
      </button>
      <ErrorNote error={error} />
    </form>
  );
}
