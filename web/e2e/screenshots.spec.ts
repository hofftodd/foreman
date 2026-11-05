import { execFileSync } from "node:child_process";
import { readFileSync, rmSync } from "node:fs";
import { dirname } from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

// Stages a realistic scene and captures the README screenshots.
// Skipped unless CAPTURE_SCREENSHOTS=1 (see scripts/screenshots.sh).
test.skip(!process.env.CAPTURE_SCREENSHOTS, "screenshot capture only");
test.use({ viewport: { width: 1280, height: 800 } });

const ORIGIN = process.env.ORIGIN_REPO!;
const OUT = process.env.SHOTS_DIR ?? "../docs/screenshots";

async function project(request: APIRequestContext, name: string, extra: Record<string, unknown> = {}) {
  // One bare repo per project, copied from the seeded origin, so the URLs read naturally.
  const repo = `${dirname(ORIGIN)}/${name}.git`;
  if (repo !== ORIGIN) {
    rmSync(repo, { recursive: true, force: true });
    execFileSync("git", ["clone", "-q", "--bare", ORIGIN, repo]);
  }
  const r = await request.post("/api/projects", {
    data: { name, repo_url: repo, executor_kind: "shell", test_cmd: "test -f README.md", skip_spec: true,
      watchdog: { escalation: false }, gates: { milestone: false }, ...extra },
  });
  expect(r.ok(), await r.text()).toBeTruthy();
  return ((await r.json()) as { id: number }).id;
}

async function task(request: APIRequestContext, pid: number, data: Record<string, unknown>) {
  const r = await request.post(`/api/projects/${pid}/tasks`, { data });
  expect(r.ok(), await r.text()).toBeTruthy();
  return ((await r.json()) as { id: number }).id;
}

async function state(request: APIRequestContext, id: number) {
  const t = (await (await request.get(`/api/tasks/${id}`)).json()) as { state: string; reason: string };
  return t.state === "failed" ? `failed: ${t.reason}` : t.state;
}

const shot = (page: Page, name: string) => page.screenshot({ path: `${OUT}/${name}.png` });

// Scripted Tester and Builder steps for a task (the shell executor's role scripts).
const RUNNER = readFileSync(new URL("fixtures/greeter-run.sh", import.meta.url), "utf8");
const write = (path: string, body: string) => `mkdir -p "$(dirname ${path})"\ncat > ${path} <<'EOS'\n${body}EOS\n`;
const roles = (tester: string, builder: string) => JSON.stringify({ tester, builder });

// A shell script that looks like an agent working through a task.
const agent = (steps: string[], tail = "") =>
  steps.map((s) => `echo '${s}'; sleep 0.4`).join("\n") + (tail ? `\n${tail}` : "");

test("capture README screenshots", async ({ page, request }) => {
  test.setTimeout(240_000);

  // A second, finished project for the overview page.
  const docs = await project(request, "docs-site");
  const d1 = await task(request, docs, {
    title: "Add getting-started page",
    executor_script: "mkdir -p docs && echo '# Getting started' > docs/start.md",
    validation_cmd: "test -f docs/start.md",
  });
  await expect.poll(() => state(request, d1), { timeout: 60_000 }).toBe("done");

  // A project built the new way: the Tester writes each task's tests, the Builder the code.
  const greeter = await project(request, "hello-cli", { test_cmd: "" });
  const g1 = await task(request, greeter, {
    title: "Greet by name",
    description: "Turn a name into a greeting.",
    criteria: ['`greet.sh bob` prints "hello, bob"'],
    executor_script: roles(
      write("tests/run.sh", RUNNER) + write("tests/check_greet.sh", 'test "$(sh greet.sh bob)" = "hello, bob"\n'),
      write("greet.sh", 'echo "hello, $1"\n'),
    ),
  });
  const g2 = await task(request, greeter, {
    title: "Shout when asked",
    description: "An option to shout the greeting.",
    criteria: ['`greet.sh --shout bob` prints "HELLO, BOB"'],
    deps: [g1],
    executor_script: roles(
      write("tests/check_shout.sh", 'test "$(sh greet.sh --shout bob)" = "HELLO, BOB"\n'),
      write(
        "greet.sh",
        'if [ "$1" = "--shout" ]; then\n  echo "hello, $2" | tr a-z A-Z\n  exit 0\nfi\n' +
          'if [ -z "$1" ]; then echo "usage: greet.sh NAME" >&2; exit 2; fi # uncovered\necho "hello, $1"\n',
      ),
    ),
  });
  const g3 = await task(request, greeter, {
    title: "Say goodbye",
    description: "A farewell to go with the greeting.",
    criteria: ['`bye.sh bob` prints "goodbye, bob"'],
    deps: [g2],
    executor_script: roles(
      write("tests/check_bye.sh", 'test "$(sh bye.sh bob)" = "goodbye, bob"\n'),
      write("bye.sh", 'echo "goodbye, $1"\n'),
    ),
  });
  await expect.poll(() => state(request, g3), { timeout: 90_000 }).toBe("done");

  const pid = await project(request, "todo-api");
  const config = await task(request, pid, {
    title: "Add config loader",
    description: "Load settings from environment variables with sane defaults.",
    todos: ["Create src/config.py", "Read PORT and DATABASE_URL", "Default PORT to 8080"],
    allowed_globs: ["src/config.py"],
    executor_script: agent(
      ["> reading src/app.py", "> creating src/config.py", "> running validation"],
      "cat > src/config.py <<'EOF'\nimport os\n\nPORT = int(os.environ.get(\"PORT\", \"8080\"))\nDATABASE_URL = os.environ.get(\"DATABASE_URL\", \"sqlite:///todo.db\")\nEOF",
    ),
    validation_cmd: "grep -q PORT src/config.py",
  });
  const tests = await task(request, pid, {
    title: "Write integration tests",
    allowed_globs: ["tests/*"],
    executor_script: agent(["> creating tests/test_api.py"], "mkdir -p tests && printf 'def test_list():\\n    assert list_todos() == []\\n' > tests/test_api.py"),
    validation_cmd: "python3 -m pytest -q tests/",
  });
  const cli = await task(request, pid, {
    title: "Parse CLI arguments",
    description: "Add an argparse entry point that starts the server.",
    todos: ["Create src/cli.py", "Support --port and --reload", "Wire into pyproject scripts"],
    allowed_globs: ["src/cli.py"],
    deps: [config],
    executor_script: agent(
      [
        "> reading src/config.py",
        "> plan: argparse parser with --port/--reload, call app.run()",
        "> creating src/cli.py",
      ],
      "cat > src/cli.py <<'EOF'\nimport argparse\n\nfrom src import config\n\n\ndef main() -> None:\n    p = argparse.ArgumentParser(prog=\"todo-api\")\n    p.add_argument(\"--port\", type=int, default=config.PORT)\nEOF\n" +
        "i=0; while true; do i=$((i+1)); echo \"> running tests (attempt $i): 3 passed, 1 failed - test_reload_flag\"; sleep 1.5; done",
    ),
    validation_cmd: "python3 -m pytest -q tests/test_cli.py",
  });
  await task(request, pid, {
    title: "Add --verbose flag",
    allowed_globs: ["src/cli.py"],
    deps: [cli],
    executor_script: "true",
    validation_cmd: "grep -q verbose src/cli.py",
  });

  await expect.poll(() => state(request, config), { timeout: 60_000 }).toBe("done");
  await expect.poll(() => state(request, tests), { timeout: 60_000 }).toMatch(/^failed/);
  await expect.poll(() => state(request, cli), { timeout: 60_000 }).toBe("running");

  // Inference nodes: two local servers and Claude (all answered by the scripted model server)
  const llmRoot = process.env.FAKE_LLM_URL!.replace(/\/v1$/, "");
  const nodes: Record<string, number> = {};
  for (const n of [
    { name: "mac-studio", capacity: 2, context_window: 131072 },
    { name: "r9700-mini", capacity: 1, context_window: 32768 },
    { name: "claude-opus", provider: "anthropic", base_url: llmRoot, api_key: "sk-demo", capacity: 2,
      context_window: 1000000, price_in: 4, price_out: 20 },
    { name: "claude-haiku", provider: "anthropic", base_url: llmRoot, api_key: "sk-demo", capacity: 4,
      context_window: 1000000, price_in: 0.1, price_out: 0.5 },
  ]) {
    const r = await request.post("/api/nodes", { data: { base_url: process.env.FAKE_LLM_URL, ...n } });
    const id = ((await r.json()) as { id: number }).id;
    nodes[n.name] = id;
    await request.post(`/api/nodes/${id}/check`);
  }

  // 1. Projects overview
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "todo-api" })).toBeVisible();
  await shot(page, "projects");

  // 2. Project board with a running task
  await page.goto(`/projects/${pid}`);
  await expect(page.locator(".column").nth(1)).toContainText("Parse CLI arguments");
  await page.waitForTimeout(3000);
  await shot(page, "project-board");

  // 2b. Progress over time
  const progress = page.locator("section.card", { has: page.getByRole("heading", { name: "Progress" }) });
  await expect(progress.locator("svg")).toBeVisible();
  await progress.screenshot({ path: `${OUT}/project-progress.png` });

  // 2c. Nodes with their load
  await page.goto("/nodes");
  await expect(page.getByText("mac-studio")).toBeVisible();
  await expect(page.locator(".dot.ok").first()).toBeVisible();
  await shot(page, "nodes");

  // 3. Live log of the running task
  await page.goto(`/tasks/${cli}`);
  await expect(page.locator(".event-log")).toContainText("attempt 3", { timeout: 30_000 });
  await shot(page, "task-live-log");

  // 4. Diff of a finished task
  await page.goto(`/tasks/${config}`);
  await page.getByRole("button", { name: "Diff" }).click();
  await expect(page.locator(".diff")).toContainText("DATABASE_URL");
  await shot(page, "task-diff");

  // 4b. A task's tests: red after the Tester, green after the Builder, coverage
  await page.goto(`/tasks/${g2}`);
  await page.getByRole("button", { name: "Tests" }).click();
  await expect(page.locator(".tests-view")).toContainText("Builder step");
  await shot(page, "task-tests");

  // 4c. The project's tests and coverage over time
  await page.goto(`/projects/${greeter}`);
  const testsCard = page.locator("section.card", { has: page.getByRole("heading", { name: "Tests", exact: true }) });
  await expect(testsCard.locator("svg")).toBeVisible();
  await testsCard.screenshot({ path: `${OUT}/project-tests.png` });

  // 5. The big red button: stop the looping task
  await page.goto(`/tasks/${cli}`);
  await page.getByRole("button", { name: "STOP", exact: true }).click();
  await expect(page.locator("h1 .badge")).toHaveText(/stopped by user/i, { timeout: 10_000 });
  await expect(page.locator(".event-log")).toContainText("run killed");
  await shot(page, "task-stopped");

  // 6. E-stop
  const long = await task(request, docs, {
    title: "Generate API reference",
    executor_script: "while true; do echo '> generating...'; sleep 1; done",
    validation_cmd: "true",
  });
  await expect.poll(() => state(request, long), { timeout: 60_000 }).toBe("running");
  await page.goto(`/projects/${docs}`);
  await page.getByRole("button", { name: "E-STOP ALL" }).click();
  await expect(page.locator(".estop-banner")).toBeVisible();
  await expect(page.locator(".column").nth(2)).toContainText("Generate API reference", { timeout: 10_000 });
  await shot(page, "estop");
  await page.getByRole("button", { name: "Clear E-stop" }).click();

  // 7. Project settings: which model each agent uses, tests, autonomy, checkpoints, spend cap
  await request.patch(`/api/projects/${docs}`, {
    data: {
      executor_kind: "opencode",
      test_cmd: "",
      spend_cap_usd: 5,
      watchdog: { escalation: true },
      role_bindings: {
        project_planner: { node_id: nodes["claude-opus"], model: "claude-opus-5-5" },
        task_planner: { node_id: nodes["claude-opus"], model: "claude-opus-5-5" },
        tester: { node_id: nodes["mac-studio"], model: "qwen3-coder-30b" },
        builder: { node_id: nodes["r9700-mini"], model: "qwen3-coder-30b" },
        escalation: { node_id: nodes["claude-opus"], model: "claude-opus-5-5" },
      },
    },
  });
  await page.goto(`/projects/${docs}`);
  await page.getByRole("button", { name: "Settings" }).click();
  const settings = page.locator("form.card", { has: page.getByRole("heading", { name: "Settings" }) });
  await expect(settings).toBeVisible();
  // Clip from the full page so the sticky top bar doesn't cover the form.
  await page.evaluate(() => window.scrollTo(0, 0));
  const box = (await settings.boundingBox())!;
  await page.screenshot({ path: `${OUT}/project-settings.png`, fullPage: true, clip: box });

  // 8. What each agent may and may not do
  const rolesCard = page.locator("section.card", { has: page.getByRole("heading", { name: /Agents and what they may do/ }) });
  await rolesCard.scrollIntoViewIfNeeded();
  await rolesCard.screenshot({ path: `${OUT}/agent-roles.png` });
});
