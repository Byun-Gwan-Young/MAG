// C-19-9: capture the live monitor (a gateway session from the DB : steps, STOP card, block 403 cell)
// and the guide (gateway address, key header), desktop and phone width. Offline: frontend/dist served by
// `vite preview`, every /api/* call stubbed here with made-up data (no server, no DB, no provider).
// Run from the repository root after `npm run build` in frontend:
//   node reports/c19-9/capture.mjs
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const require = createRequire(join(root, "frontend", "package.json"));
const { chromium } = require("@playwright/test");
const { preview } = await import(pathToFileURL(require.resolve("vite")).href);
const data = require(join(root, "frontend", "public", "records.json"));

const out = join(root, "reports", "c19-9", "captures");
mkdirSync(out, { recursive: true });
const widths = {
  desktop: { width: 1280, height: 1400 },
  mobile: { width: 390, height: 844 },
};
const user = {
  id: 1,
  name: "데모 사용자",
  email: "demo@example.com",
  account_status: "active",
  session_scope: "full",
  csrf_token: "capture-only-csrf",
};
// GET /api/providers shape (storage.accounts.list_providers); kmu shown as not configured.
const providers = [
  ["openai", "OpenAI (GPT)", "global"],
  ["anthropic", "Anthropic (Claude)", "global"],
  ["gemini", "Google (Gemini)", "global"],
  ["xai", "xAI (Grok)", "global"],
  ["mistral", "Mistral", "global"],
  ["deepseek", "DeepSeek", "global"],
  ["groq", "Groq", "global"],
  ["together", "Together AI", "global"],
  ["fireworks", "Fireworks AI", "global"],
  ["openrouter", "OpenRouter", "global"],
  ["perplexity", "Perplexity", "global"],
  ["cohere", "Cohere", "global"],
  ["moonshot", "Moonshot (Kimi)", "global"],
  ["zai", "Z.ai (GLM)", "global"],
  ["upstage", "Upstage (Solar)", "korea"],
  ["clova", "네이버 (HyperCLOVA X)", "korea"],
  ["kmu", "학교 게이트웨이 (팀 데모)", "team"],
].map(([id, name, group]) => ({ id, name, group, available: id !== "kmu" }));
// GET /api/live/sessions{,/id} shape (backend/routers/live.py), made-up values.
const stopText =
  "개발 역할이 3단계 연속 테스트 통과 수와 파일을 바꾸지 못했습니다 (같은 결과 반복).";
const row = {
  id: "p3-support-run-07", short: "run-07", session_key: "support-run-07", name: "고객 지원 봇",
  task_name: "고객 지원 봇", variant: "support-run-07", mode: "block", class: "stalled", termination: "guardrail_stop",
  steps: 6, tokens: 48210, first_stop: 6, template: "E1", stop_text: stopText, blocked_step: 7,
  started_at: "2026-11-17T05:10:00+00:00", provider: "kmu", model: "glm-5.3-flash",
  live: { status: "ended", updated_at: "2026-11-17T05:16:40+00:00", idle_seconds: 4, in_progress: null },
};
const running = {
  ...row, id: "p4-search-run-02", short: "run-02", session_key: "search-run-02", name: "사내 검색 에이전트",
  task_name: "사내 검색 에이전트", variant: "search-run-02", mode: "observe", class: null, termination: null, steps: 3, tokens: null,
  first_stop: null, template: null, stop_text: null, blocked_step: null,
  started_at: "2026-11-17T05:18:00+00:00",
  live: { status: "running", updated_at: "2026-11-17T05:18:30+00:00", idle_seconds: 2, in_progress: null },
};
const roles = ["planner", "developer", "reviewer"];
const step = (n, state, label, extra = {}) => ({
  n, agent: roles[(n - 1) % 3], role: roles[(n - 1) % 3], state, label, stop: null, blocked: false,
  status: "completed", fail: [], tests: [7, 16], report: null, claim: null, tools: 0, tool_errors: 0,
  changed: 0, paths: [], tok: null, cum: null, cumc: null, ...extra,
});
const detail = {
  ...row,
  step_rows: [
    step(1, "baseline", "기준"), step(2, "baseline", "기준"), step(3, "baseline", "기준"),
    step(4, "stagnant", "정체"), step(5, "stagnant", "정체"),
    step(6, "stagnant", "정체", { stop: { t: "E1", text: stopText } }),
    step(7, null, "차단 (403)", { agent: null, role: null, blocked: true, status: "blocked", tests: null }),
  ],
};

const server = await preview({
  root: join(root, "frontend"),
  preview: { host: "127.0.0.1", port: 5176, strictPort: true, proxy: {} },
  logLevel: "error",
});
const base = "http://127.0.0.1:5176";
const browser = await chromium.launch({
  channel: process.env.BROWSER_CHANNEL || "chrome",
});
const written = [];
const shot = async (page, name) => {
  const file = join(out, name);
  await page.evaluate(() => document.activeElement?.blur());
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: file, fullPage: name.includes("mobile") });
  written.push(file.slice(root.length + 1));
};
try {
  for (const [wname, viewport] of Object.entries(widths)) {
    const context = await browser.newContext({ viewport, deviceScaleFactor: 1 });
    await context.route("https://accounts.google.com/**", (r) => r.abort());
    await context.route("**/api/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      const json = (body) => route.fulfill({ json: body });
      if (path === "/api/auth/me") return json(user);
      if (path === "/api/auth/config") return json({ client_id: "", open_signup: true });
      if (path === "/api/frontend/reference") return json(data);
      if (path === "/api/providers") return json(providers);
      if (path === "/api/summary") return json({ headline: data.headline, judgement_ms: data.judgement_ms });
      if (path === "/api/sessions") return json({ count: 0, sessions: [] });
      if (path === "/api/live/sessions")
        return json({ version: "service-live-v1", now: running.live.updated_at, sessions: [running, row] });
      if (path === `/api/live/sessions/${row.id}`)
        return json({ version: "service-live-v1", now: running.live.updated_at, session: detail });
      return route.fulfill({ status: 404, json: { detail: "Not found" } });
    });
    const page = await context.newPage();
    await page.goto(`${base}/#live`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "run-07 세션 보기" }).click();
    await page.locator(".live-alert").waitFor();
    await page.waitForTimeout(400);
    await shot(page, `live-monitor-${wname}.png`);
    await page.goto(`${base}/#guide`, { waitUntil: "networkidle" });
    await page.waitForTimeout(300);
    await shot(page, `guide-${wname}.png`);
    await context.close();
  }
} finally {
  await browser.close();
  await new Promise((done) => server.httpServer.close(done));
}
console.log(JSON.stringify({ captures: written }, null, 1));
