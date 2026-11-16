// C-19-10T: capture the original MAG screens and the React screens side by side.
// Offline: the original is reports/c19-3/demo.html and backend/templates/login.html read from disk,
// the React side is frontend/dist served by `vite preview` with ?preview=1 (login screen: /api/auth/* stubbed).
// Run from the repository root after `npm run build` in frontend:
//   node reports/c19-10t/capture.mjs
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const require = createRequire(join(root, "frontend", "package.json"));
const { chromium } = require("@playwright/test");
const { preview } = await import(pathToFileURL(require.resolve("vite")).href);

const out = join(root, "reports", "c19-10t", "captures");
const work = join(out, ".work");
mkdirSync(work, { recursive: true });

const widths = {
  desktop: { width: 1280, height: 800 },
  mobile: { width: 390, height: 844 },
};
const legacy = pathToFileURL(join(root, "reports", "c19-3", "demo.html")).href;
const template = readFileSync(
  join(root, "backend", "templates", "login.html"),
  "utf8",
);
const fill = (values) =>
  template.replace(/\{\{([A-Z_]+)\}\}/g, (_, k) => values[k] ?? "");
const loginFile = join(work, "legacy-login.html");
const accountFile = join(work, "legacy-account.html");
writeFileSync(
  loginFile,
  fill({
    LOGIN_DISPLAY: "block",
    USER_DISPLAY: "none",
    SIGNUP_NOTE: "처음 로그인하면 계정이 만들어집니다.",
  }),
);
writeFileSync(
  accountFile,
  fill({
    LOGIN_DISPLAY: "none",
    USER_DISPLAY: "block",
    NAME: "데모 사용자",
    EMAIL: "demo@example.com",
    ACCOUNT_MESSAGE: "활성 계정",
  }),
);

// [name, original URL or null, React path]
const pages = [
  ["dashboard", `${legacy}#dashboard`, "/?preview=1#dashboard"],
  ["session", `${legacy}#session`, "/?preview=1#session"],
  ["compare", `${legacy}#compare`, "/?preview=1#compare"],
  ["stats", `${legacy}#stats`, "/?preview=1#stats"],
  ["system", `${legacy}#system`, "/?preview=1#system"],
  ["guide", `${legacy}#guide`, "/?preview=1#guide"],
  ["login", pathToFileURL(loginFile).href, "/login"],
  ["projects", null, "/?preview=1#projects"],
  ["account", pathToFileURL(accountFile).href, "/?preview=1#account"],
];

const server = await preview({
  root: join(root, "frontend"),
  preview: { host: "127.0.0.1", port: 5174, strictPort: true, proxy: {} },
  logLevel: "error",
});
const base = "http://127.0.0.1:5174";
const browser = await chromium.launch({
  channel: process.env.BROWSER_CHANNEL || "chrome",
});
const written = [];
try {
  for (const [wname, viewport] of Object.entries(widths)) {
    const context = await browser.newContext({
      viewport,
      deviceScaleFactor: 1,
    });
    // No server: the signed-out login screen gets a 401 and an empty sign-in config.
    await context.route("**/api/auth/me", (r) =>
      r.fulfill({ status: 401, json: { detail: "not signed in" } }),
    );
    await context.route("**/api/auth/config", (r) =>
      r.fulfill({ json: { client_id: "", open_signup: true } }),
    );
    await context.route("https://accounts.google.com/**", (r) => r.abort());
    const page = await context.newPage();
    for (const [name, oldUrl, newPath] of pages) {
      const shots = {};
      for (const [side, url] of [
        ["old", oldUrl],
        ["new", newPath && base + newPath],
      ]) {
        if (!url) continue;
        await page.goto(url, { waitUntil: "networkidle" });
        await page.waitForTimeout(400);
        const file = join(work, `${name}-${wname}-${side}.png`);
        await page.screenshot({ path: file });
        shots[side] = readFileSync(file).toString("base64");
      }
      const cell = (side, label) =>
        `<figure><figcaption>${label}</figcaption>${
          shots[side]
            ? `<img src="data:image/png;base64,${shots[side]}">`
            : `<div class="none" style="width:${viewport.width}px;height:${viewport.height}px">원래 화면에 없는 페이지</div>`
        }</figure>`;
      await page.setViewportSize({
        width: viewport.width * 2 + 60,
        height: viewport.height + 80,
      });
      await page.setContent(
        `<style>body{margin:0;padding:16px;display:flex;gap:28px;background:#d8d4cc;font:14px -apple-system,sans-serif}
figure{margin:0}figcaption{font-weight:700;margin-bottom:6px}img,.none{display:block;border:1px solid #9a9488}
.none{display:grid;place-items:center;background:#f4efe6;color:#7a7468}</style>
${cell("old", `원래 (demo_ui · login.html) — ${name} · ${wname}`)}${cell("new", `React (C-19-10T) — ${name} · ${wname}`)}`,
      );
      const pair = join(out, `${name}-${wname}.png`);
      await page.screenshot({ path: pair, fullPage: true });
      written.push(pair.slice(root.length + 1));
      await page.setViewportSize(viewport);
    }
    await context.close();
  }
} finally {
  await browser.close();
  await new Promise((done) => server.httpServer.close(done));
  rmSync(work, { recursive: true, force: true });
}
console.log(JSON.stringify({ captures: written }, null, 1));
