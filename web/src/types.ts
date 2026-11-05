export type TaskState =
  | "proposed"
  | "queued"
  | "running"
  | "validating"
  | "done"
  | "failed"
  | "paused"
  | "stopped_by_user"
  | "discarded"
  | "awaiting_merge"
  | "blocked"
  | "split";

export interface RoleBinding {
  node_id: number | null;
  model: string;
}

export type ProjectPhase = "specify" | "decomposing" | "plan_review" | "execute" | "done";

export interface Project {
  id: number;
  name: string;
  phase: ProjectPhase;
  feature: string;
  acceptance_cmd: string;
  max_task_files: number;
  context_budget_pct: number;
  gates: Record<string, unknown>;
  watchdog: Record<string, unknown>;
  watchdog_effective?: WatchdogLimits;
  gates_effective?: Record<string, boolean>;
  baseline_tasks: number;
  acceptance_failures: number;
  acceptance_commit: string;
  acceptance_passed: boolean;
  milestone_pr_url: string;
  repo_url: string;
  default_branch: string;
  milestone_branch: string;
  test_cmd: string;
  executor_kind: string;
  network: "isolated" | "open";
  role_bindings: Record<string, RoleBinding>;
  paused: boolean;
  push_after_merge: boolean;
  spend_usd: number;
  spend_cap_usd: number;
  min_task_coverage: number;
  test_paths: string[];
  coverage?: number | null;
  created_at: number;
  task_counts?: Partial<Record<TaskState, number>>;
  pending_gates?: number;
  active_task?: string;
}

export interface TaskRun {
  id: number;
  task_id: number;
  node_id: number | null;
  model: string;
  handle: string;
  state: "running" | "succeeded" | "failed" | "stopped" | "interrupted";
  model_requests: number;
  tokens_in: number;
  tokens_out: number;
  watchdog: string;
  failure_kind: string;
  diff_lines: number;
  exit_code: number | null;
  step: "" | "tester" | "builder";
  reason: string;
  started_at: number;
  ended_at: number | null;
}

export interface Task {
  id: number;
  project_id: number;
  title: string;
  description: string;
  todos: string[];
  allowed_globs: string[];
  validation_cmd: string;
  deps: number[];
  key: string;
  kind: "setup" | "acceptance" | "feature" | "docs";
  criteria: string[];
  tests_ready: boolean;
  coverage_rounds: number;
  req_refs: string[];
  context_files: string[];
  size_violations: string[];
  pack_tokens: number;
  escalation_level: number;
  level_failures: number;
  parent_id: number | null;
  origin: string;
  executor_kind: string;
  node_id: number | null;
  model: string;
  executor_script: string;
  timeout_s: number;
  state: TaskState;
  hold: boolean;
  reason: string;
  attempts: number;
  created_at: number;
  updated_at: number;
  runs?: TaskRun[];
  stop_pending?: boolean;
}

export interface Node {
  id: number;
  name: string;
  provider: "openai" | "anthropic";
  context_window: number;
  base_url: string;
  has_api_key: boolean;
  capacity: number;
  models: string[];
  healthy: boolean | null;
  last_checked: number | null;
  price_in: number;
  price_out: number;
  active: number;
  waiting: number;
  error?: string;
}

export interface FEvent {
  id: number;
  project_id: number | null;
  task_id: number | null;
  run_id: number | null;
  kind:
    | "log"
    | "state"
    | "test"
    | "diff"
    | "control"
    | "system"
    | "gate"
    | "planner"
    | "planner_thinking"
    | "research"
    | "denied"
    | "planner_start"
    | "planner_end"
    | "watchdog"
    | "replan";
  message: string;
  data: Record<string, unknown>;
  ts: number;
}

export interface GitHubTokenStatus {
  configured: boolean;
  source: "dashboard" | "environment" | "";
  hint: string;
  ok?: boolean;
  login?: string;
  scopes?: string | null;
  error?: string;
}

export interface SystemInfo {
  estop: boolean;
  runtime: string;
  heartbeats: Record<string, number>;
  executors: string[];
  pending_gates: number;
}

export interface SpecMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  files: string[];
  job_id: number | null;
  ts: number;
}

export interface PlannerJob {
  id: number;
  kind: "chat" | "decompose";
  state: "queued" | "running" | "done" | "failed" | "stopped";
  error: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  started_at: number | null;
  ended_at: number | null;
}

export type DocName = "requirements" | "design" | "acceptance" | "tasks";

export interface SpecState {
  phase: ProjectPhase;
  feature: string;
  acceptance_cmd: string;
  spec_approved_at: number | null;
  docs: Record<DocName, string>;
  paths: Record<DocName, string>;
  messages: SpecMessage[];
  active_job: PlannerJob | null;
  last_job: PlannerJob | null;
}

export interface WatchdogLimits {
  escalation: boolean;
  max_requests: number;
  max_tokens: number;
  max_diff_lines: number;
  repeat_limit: number;
  max_retries: number;
  split: boolean;
  replan: boolean;
  growth_cap_pct: number;
  max_acceptance_failures: number;
}

export type GateKind =
  | "plan_revision"
  | "spec_change"
  | "task_merge"
  | "escalation"
  | "milestone"
  | "budget"
  | "coverage";

export interface TestCase {
  id: number;
  name: string;
  classname: string;
  file: string;
  status: "passed" | "failed" | "error" | "skipped";
  duration_s: number;
  message: string;
  task_test: boolean;
}

export interface TestRun {
  id: number;
  task_id: number | null;
  step: "tester" | "builder" | "acceptance";
  commit: string;
  passed: number;
  failed: number;
  skipped: number;
  errors: number;
  duration_s: number;
  coverage: number | null;
  changed_coverage: number | null;
  changed_lines: number;
  files: Record<string, { covered: number; total: number }>;
  problem: string;
  created_at: number;
  cases?: TestCase[];
}

export interface CoveragePoint {
  ts: number;
  coverage: number | null;
  tests: number;
  passed: number;
  task_id: number | null;
}

export interface RoleInfo {
  key: string;
  name: string;
  summary: string;
  may: string[];
  may_not: string[];
}

export interface Gate {
  id: number;
  project_id: number;
  task_id: number | null;
  kind: GateKind;
  title: string;
  detail: string;
  payload: Record<string, unknown>;
  state: "pending" | "approved" | "rejected" | "cancelled";
  note: string;
  created_at: number;
  resolved_at: number | null;
  project_name: string;
  task_title: string;
}

export interface PlanRevision {
  id: number;
  version: number;
  trigger: string;
  summary: string;
  ops: { op: "add" | "remove" | "modify"; key: string; reason: string; task?: { title?: string }; changes?: Record<string, unknown> }[];
  spec_change: { reason?: string; proposal?: string };
  state: "applied" | "pending" | "rejected" | "empty";
  created_at: number;
}
