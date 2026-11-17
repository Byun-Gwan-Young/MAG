// C-19-9c: capture the key list right after a revoke (the revoked key gone, one notice), the project-delete
// confirmation dialog (name typed, button on) and the project list after the delete, desktop and phone width.
// Offline: frontend/dist served by `vite preview`, every /api/* call stubbed here with made-up data (no server,
// no DB, no provider, no LLM).
// Run from the repository root after `npm run build` in frontend:
//   node reports/c19-9c/capture.mjs
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const require = createRequire(join(root, "frontend", "package.json"));
const { chromium } = require("@playwright/test");
const { preview } = await import(pathToFileURL(require.resolve("vite")).href);
const data = require(join(root, "frontend", "public", "records.json"));

const out = join(root, "reports", "c19-9c", "captures");
mkdirSync(out, { recursive: true });
const widths = {
  desktop: { width: 1280, height: 900 },
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
const projectRow = (id, name, mode) => ({
  id, name, mode, provider: "kmu", rule_id: "deadlock-v0.3", created_at: "2026-11-17T05:00:00+00:00",
  utterance_log: false,
});
const keyRow = (id, prefix, name, created, used) => ({
  id, project_id: 3, key_prefix: prefix, name, created_at: created, last_used_at: used, revoked_at: null,
  revoked: false,
});

const server = await preview({
  root: join(root, "frontend"),
  preview: { host: "127.0.0.1", port: 5178, strictPort: true, proxy: {} },
  logLevel: "error",
});
const base = "http://127.0.0.1:5178";
const browser = await chromium.launch({
  channel: process.env.BROWSER_CHANNEL || "chrome",
});
const written = [];
const shot = async (page, name, fullPage = true) => {
  const file = join(out, name);
  await page.screenshot({ path: file, fullPage });
  written.push(file.slice(root.length + 1));
};
try {
  for (const [wname, viewport] of Object.entries(widths)) {
    // Fresh made-up state per width : three live keys, two projects.
    let keys = [
      keyRow(31, "a1b2c3d4", "발표 실행기", "2026-11-17T03:10:00+00:00", "2026-11-17T05:40:00+00:00"),
      keyRow(30, "e5f6a7b8", "옛 실행기", "2026-11-16T09:00:00+00:00", "2026-11-16T12:00:00+00:00"),
      keyRow(29, "c9d0e1f2", "배치 실행기", "2026-11-15T08:00:00+00:00", null),
    ];
    let projects = [projectRow(3, "발표 차단", "block"), projectRow(4, "시험 프로젝트", "observe")];
    const context = await browser.newContext({ viewport, deviceScaleFactor: 1 });
    await context.route("https://accounts.google.com/**", (r) => r.abort());
    await context.route("**/api/**", (route) => {
      const req = route.request();
      const path = new URL(req.url()).pathname;
      const json = (body) => route.fulfill({ json: body });
      if (path === "/api/auth/me") return json(user);
      if (path === "/api/auth/config") return json({ client_id: "", open_signup: true });
      if (path === "/api/frontend/reference") return json(data);
      if (path === "/api/providers") return json(providers);
      if (path === "/api/projects") return json(projects);
      if (path === "/api/projects/3/keys") return json(keys);
      if (path === "/api/keys/30/revoke") {
        keys = keys.filter((k) => k.id !== 30);
        return json({ id: 30, revoked: true });
      }
      if (/^\/api\/projects\/\d+\/sessions$/.test(path))
        return json({ items: [], total: 0, offset: 0, limit: 20 });
      if (path === "/api/projects/4" && req.method() === "DELETE") {
        projects = projects.filter((p) => p.id !== 4);
        return json({ id: 4, deleted: true, revoked_api_keys: 1 });
      }
      return route.fulfill({ status: 404, json: { detail: "Not found" } });
    });
    const page = await context.newPage();
    // 1. The key list right after revoking "옛 실행기" : the row is gone, one notice.
    await page.goto(`${base}/#keys`, { waitUntil: "networkidle" });
    await page.locator("td.mono").first().waitFor();
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("row", { name: /옛 실행기/ }).getByRole("button", { name: "폐기" }).click();
    await page.getByText("'옛 실행기' 키를 폐기했습니다.").waitFor();
    await page.mouse.move(0, 0);
    await page.waitForTimeout(300);
    // The clicked button left the DOM with its row : clear focus and the table's sideways scroll.
    const focused = await page.evaluate(() => {
      const name = document.activeElement?.className || document.activeElement?.tagName;
      document.activeElement?.blur();
      document.querySelectorAll(".table-wrap").forEach((t) => (t.scrollLeft = 0));
      window.scrollTo(0, 0); // a full-page shot of a scrolled page draws the fixed skip link mid-page
      return name;
    });
    console.error(`focus after revoke (${wname}): ${focused}`);
    await shot(page, `key-list-${wname}.png`);
    // 2. The delete confirmation with the name typed (button on). Viewport shot : the dialog is fixed.
    await page.goto(`${base}/#projects`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: /시험 프로젝트/ }).click();
    await page.getByRole("button", { name: "프로젝트 삭제", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "프로젝트를 삭제할까요?" });
    await dialog.getByLabel("확인을 위해 프로젝트 이름을 그대로 입력하세요").fill("시험 프로젝트");
    await page.evaluate(() => document.activeElement?.blur());
    await page.waitForTimeout(300);
    await shot(page, `project-delete-dialog-${wname}.png`, false);
    // 3. After the delete : the project is gone from the list, one notice.
    await dialog.getByRole("button", { name: "프로젝트 삭제" }).click();
    await page.getByText("'시험 프로젝트' 프로젝트를 삭제했습니다.", { exact: false }).waitFor();
    await page.evaluate(() => {
      document.activeElement?.blur();
      window.scrollTo(0, 0);
    });
    await page.waitForTimeout(300);
    await shot(page, `project-deleted-${wname}.png`);
    await context.close();
  }
} finally {
  await browser.close();
  await new Promise((done) => server.httpServer.close(done));
}
console.log(JSON.stringify({ captures: written }, null, 1));
