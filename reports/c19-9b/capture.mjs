// C-19-9b: capture the live monitor (a gateway session : step table with this-step and cumulative tokens, the
// cumulative bar with its STOP mark, basis lines, an opened row with the stored utterance excerpt), the session
// replay of a demo record (same table, no utterances), and the project's utterance-log setting, desktop and phone
// width. Offline: frontend/dist served by `vite preview`, every /api/* call stubbed here with made-up data (no
// server, no DB, no provider, no LLM).
// Run from the repository root after `npm run build` in frontend:
//   node reports/c19-9b/capture.mjs
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const require = createRequire(join(root, "frontend", "package.json"));
const { chromium } = require("@playwright/test");
const { preview } = await import(pathToFileURL(require.resolve("vite")).href);
const data = require(join(root, "frontend", "public", "records.json"));

const out = join(root, "reports", "c19-9b", "captures");
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
const providers = [
  ["openai", "OpenAI (GPT)", "global"],
  ["anthropic", "Anthropic (Claude)", "global"],
  ["upstage", "Upstage (Solar)", "korea"],
  ["kmu", "학교 게이트웨이 (팀 데모)", "team"],
].map(([id, name, group]) => ({ id, name, group, available: true }));

// GET /api/live/sessions{,/id} shape (backend/routers/live.py, C-19-9b fields), made-up values.
const stopText =
  "최근 3 step 동안 기획 담당 · 개발 담당 · 검토 담당 모두 파일 변화 · 통과 테스트 증가가 없습니다. 통과 테스트 14/16.";
const row = {
  id: "p3-support-run-07", short: "run-07", session_key: "support-run-07", name: "고객 지원 봇",
  task_name: "고객 지원 봇", variant: "support-run-07", mode: "block", class: "stalled", termination: "guardrail_stop",
  steps: 7, tokens: 61840, first_stop: 7, template: "E1", stop_text: stopText, blocked_step: 8,
  started_at: "2026-11-17T05:10:00+00:00", provider: "kmu", model: "glm-5.3-flash",
  live: { status: "ended", updated_at: "2026-11-17T05:16:40+00:00", idle_seconds: 4, in_progress: null },
};
const roles = { planner: "기획 담당", developer: "개발 담당", reviewer: "검토 담당" };
const order = Object.keys(roles);
const step = (n, state, label, tok, cum, basis, extra = {}) => ({
  n, agent: order[(n - 1) % 3], role: roles[order[(n - 1) % 3]], state, label, stop: null, blocked: false,
  status: "completed", fail: [], tests: [14, 16], report: null, claim: null, tools: 1, tool_errors: 0,
  changed: 0, paths: [], tok, cum, cumc: null, basis, said: [], ...extra,
});
const said = (call, text) => [{ call, text }];
const detail = {
  ...row,
  utterance_log: true,
  step_rows: [
    step(1, "baseline", "기준", 7120, 7120, "기준점 : 이 역할의 첫 관측이라 비교할 이전 기록이 없습니다", {
      tests: [12, 16],
      said: said(1, "요구사항을 읽었습니다. 환불 문의의 상태 값이 SPEC 과 테스트에서 다르게 쓰여 있어 SPEC 을 먼저 맞추겠습니다."),
    }),
    step(2, "baseline", "기준", 8410, 15530, "기준점 : 이 역할의 첫 관측이라 비교할 이전 기록이 없습니다", {
      tests: [12, 16], said: said(2, "상태 전이 함수를 고쳐 환불 접수 → 처리 중 경로를 추가했습니다."),
    }),
    step(3, "baseline", "기준", 6980, 22510, "기준점 : 이 역할의 첫 관측이라 비교할 이전 기록이 없습니다", {
      tests: [12, 16], said: said(3, "테스트 12/16 통과. 상태 이름 두 개가 SPEC 과 어긋납니다."),
    }),
    step(4, "progress", "진전", 9050, 31560, "새 파일 내용 2 개 · 통과 테스트 12 → 14 · 쟁점 ownership 닫힘", {
      changed: 2, paths: ["SPEC.md", "src/refund.py"],
      said: said(4, "SPEC 의 상태 이름을 테스트에 맞췄고 담당 팀 쟁점을 정리했습니다."),
    }),
    step(5, "stagnant", "정체", 10120, 41680,
      "파일 변화 없음 · 통과 테스트 14/16 그대로 · 새 쟁점 없음 → 진전 없는 차례 1/3", {
        said: said(5, "남은 두 테스트는 SPEC 해석 문제라 기획 담당의 결정이 필요합니다."),
      }),
    step(6, "stagnant", "정체", 9870, 51550,
      "파일 변화 없음 · 통과 테스트 14/16 그대로 · 새 쟁점 없음 → 진전 없는 차례 2/3", {
        said: said(6, "개발 담당이 구현을 보완해야 합니다. 테스트 결과는 같습니다."),
      }),
    step(7, "stagnant", "정체", 10290, 61840, stopText, {
      stop: { t: "E1", text: stopText },
      said: said(7, "SPEC 은 이미 맞습니다. 검토 담당이 테스트를 다시 확인해 주세요."),
    }),
    step(8, null, "차단 (403)", 0, 61840,
      "차단 : STOP 뒤의 요청이라 업스트림으로 보내지 않고 403 으로 돌려주었습니다", {
        agent: null, role: null, blocked: true, status: "blocked", tests: null, tools: 0,
      }),
  ],
};

// GET /api/sessions{,/id,/id/steps} shape (service/api.py, C-19-9b basis), a made-up demo record.
const demoId = "DEMO-CT-stall-v1-capture0000000000000000000000";
const demoRow = {
  session_id: demoId, scenario_id: "DEMO-CT-stall-v1", task: "CT", task_type: "T3", variant: "stall",
  mode: "observe", class: "stalled", steps: 6, tokens_total: 48900, first_stop_step: 6,
  first_stop_text: stopText, first_stop_template: "E1", blocked_step: null, termination_reason: "max_steps",
  started_at: "2026-10-12T02:00:00+00:00", role_labels: roles, validity: "valid",
};
const demoSteps = [
  [1, "planner", "baseline", "기준", 6200, "기준점 : 이 역할의 첫 관측이라 비교할 이전 기록이 없습니다", [7, 17]],
  [2, "developer", "baseline", "기준", 8100, "기준점 : 이 역할의 첫 관측이라 비교할 이전 기록이 없습니다", [7, 17]],
  [3, "reviewer", "baseline", "기준", 7400, "기준점 : 이 역할의 첫 관측이라 비교할 이전 기록이 없습니다", [7, 17]],
  [4, "planner", "progress", "진전", 9300, "새 파일 내용 1 개 · 통과 테스트 7 → 16 · 쟁점 requirements 닫힘", [16, 17]],
  [5, "developer", "stagnant", "정체", 8700,
    "파일 변화 없음 · 통과 테스트 16/17 그대로 · 새 쟁점 없음 → 진전 없는 차례 1/3", [16, 17]],
  [6, "reviewer", "stagnant", "정체", 9200, stopText, [16, 17]],
];
let cumulative = 0;
const replay = demoSteps.map(([n, agent, state, label, tokens, basis, tests]) => {
  cumulative += tokens;
  return {
    step_id: n, agent, role: roles[agent], state, state_label: label, step_status: "completed",
    failure_reasons: [], report: null, completion_claim: null, tools: [], tests: { passed: tests[0], total: tests[1] },
    changed_paths: n === 4 ? ["SPEC.md"] : [], decision: n === 6 ? "deadlock" : "continue",
    stop: n === 6 ? { template_id: "E1", text: stopText } : null, blocked: false,
    tokens: { prompt: tokens - 900, cached_prompt: 0, completion: 900, reasoning: 300 }, usage_missing_turns: 0,
    credits_estimated: null, cumulative_tokens: cumulative, cumulative_credits: null, basis,
    judgement: { judgement_ns: 41000, reason: null, consecutive_steps: n >= 5 ? n - 4 : 0 },
  };
});

const project = {
  id: 3, name: "고객 지원 봇", mode: "block", provider: "kmu", rule_id: "deadlock-v0.3",
  created_at: "2026-11-17T05:00:00+00:00", utterance_log: true,
};

const server = await preview({
  root: join(root, "frontend"),
  preview: { host: "127.0.0.1", port: 5177, strictPort: true, proxy: {} },
  logLevel: "error",
});
const base = "http://127.0.0.1:5177";
const browser = await chromium.launch({
  channel: process.env.BROWSER_CHANNEL || "chrome",
});
const written = [];
const shot = async (page, name) => {
  const file = join(out, name);
  await page.evaluate(() => document.activeElement?.blur());
  await page.screenshot({ path: file, fullPage: true });
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
      if (path === "/api/sessions") return json({ count: 1, sessions: [demoRow] });
      if (path === `/api/sessions/${demoId}`)
        return json({ session: { ...demoRow, scenario: { korean_name: "코드 작업 · 교착판 (가짜 데이터)" } } });
      if (path === `/api/sessions/${demoId}/steps`) return json({ session_id: demoId, steps: replay });
      if (path === "/api/live/sessions")
        return json({ version: "service-live-v1", now: row.live.updated_at, sessions: [row] });
      if (path === `/api/live/sessions/${row.id}`)
        return json({ version: "service-live-v1", now: row.live.updated_at, session: detail });
      if (path === "/api/projects") return json([project]);
      if (path === "/api/projects/3/sessions") return json({ items: [], total: 0, offset: 0, limit: 20 });
      return route.fulfill({ status: 404, json: { detail: "Not found" } });
    });
    const page = await context.newPage();
    await page.goto(`${base}/#live`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "run-07 세션 보기" }).click();
    await page.locator(".live-alert").waitFor();
    await page.getByRole("button", { name: "조회 일시정지" }).click();
    await page.getByRole("button", { name: "5단계 펼치기" }).click();
    await page.getByRole("button", { name: "7단계 펼치기" }).click();
    await page.waitForTimeout(400);
    await shot(page, `live-monitor-${wname}.png`);
    await page.goto(`${base}/#session/${encodeURIComponent(demoId)}`, { waitUntil: "networkidle" });
    await page.locator(".step-table").waitFor();
    await page.getByRole("button", { name: "6단계 펼치기" }).click();
    await page.waitForTimeout(300);
    await shot(page, `session-replay-${wname}.png`);
    await page.goto(`${base}/#projects`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: /고객 지원 봇/ }).click();
    await page.getByRole("radiogroup", { name: "발화 기록" }).waitFor();
    await page.waitForTimeout(300);
    await shot(page, `project-utterance-log-${wname}.png`);
    await context.close();
  }
} finally {
  await browser.close();
  await new Promise((done) => server.httpServer.close(done));
}
console.log(JSON.stringify({ captures: written }, null, 1));
