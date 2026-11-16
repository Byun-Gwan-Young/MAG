// C-19-8P: capture the project screen's provider picker (create) and provider setting (change),
// desktop and phone width. Offline: frontend/dist served by `vite preview`, every /api/* call stubbed
// here with made-up projects (no server, no DB, no provider is contacted).
// Run from the repository root after `npm run build` in frontend:
//   node reports/c19-8p/capture.mjs
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const require = createRequire(join(root, "frontend", "package.json"));
const { chromium } = require("@playwright/test");
const { preview } = await import(pathToFileURL(require.resolve("vite")).href);
const data = require(join(root, "frontend", "public", "records.json"));

const out = join(root, "reports", "c19-8p", "captures");
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
// Same shape as GET /api/providers (storage.accounts.list_providers); kmu shown as not configured.
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
const projects = [
  { id: 3, name: "고객 지원 봇", mode: "observe", provider: "openai", rule_id: "deadlock-v0.3" },
  { id: 4, name: "사내 검색 에이전트", mode: "block", provider: "upstage", rule_id: "deadlock-v0.3" },
];

const server = await preview({
  root: join(root, "frontend"),
  preview: { host: "127.0.0.1", port: 5175, strictPort: true, proxy: {} },
  logLevel: "error",
});
const base = "http://127.0.0.1:5175";
const browser = await chromium.launch({
  channel: process.env.BROWSER_CHANNEL || "chrome",
});
const written = [];
const shot = async (page, name) => {
  const file = join(out, name);
  // Desktop fits the tall viewport (the sticky menu breaks full-page shots); the phone scrolls.
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
      if (path === "/api/projects") return json(projects);
      if (/^\/api\/projects\/\d+\/sessions$/.test(path))
        return json({ items: [], total: 0, offset: 0, limit: 20 });
      return route.fulfill({ status: 404, json: { detail: "Not found" } });
    });
    const page = await context.newPage();
    await page.goto(`${base}/#projects`, { waitUntil: "networkidle" });
    await page.getByLabel("LLM 제공자").selectOption("anthropic");
    await page.getByLabel("프로젝트 이름").fill("캡스톤 실험 01");
    await page.waitForTimeout(300);
    await shot(page, `project-create-${wname}.png`);
    await page.getByRole("button", { name: /고객 지원 봇/ }).click();
    await page.getByLabel("바꿀 제공자").selectOption("clova");
    await page.waitForTimeout(300);
    await shot(page, `project-settings-${wname}.png`);
    await context.close();
  }
} finally {
  await browser.close();
  await new Promise((done) => server.httpServer.close(done));
}
console.log(JSON.stringify({ captures: written }, null, 1));
