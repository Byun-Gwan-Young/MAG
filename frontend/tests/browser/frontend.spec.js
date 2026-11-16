import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
const data = JSON.parse(
  readFileSync(new URL("../../public/records.json", import.meta.url)),
);
const user = {
  id: 1,
  name: "테스트 사용자",
  email: "test@example.com",
  account_status: "active",
  session_scope: "full",
  csrf_token: "test-only-csrf",
};
// C-19-8P: what GET /api/providers returns (ids, names, groups, availability; never an address).
const providers = [
  { id: "openai", name: "OpenAI (GPT)", group: "global", available: true },
  {
    id: "anthropic",
    name: "Anthropic (Claude)",
    group: "global",
    available: true,
  },
  { id: "gemini", name: "Google (Gemini)", group: "global", available: true },
  { id: "groq", name: "Groq", group: "global", available: true },
  { id: "upstage", name: "Upstage (Solar)", group: "korea", available: true },
  {
    id: "clova",
    name: "네이버 (HyperCLOVA X)",
    group: "korea",
    available: true,
  },
  {
    id: "kmu",
    name: "학교 게이트웨이 (팀 데모)",
    group: "team",
    available: false,
  },
];

test("preview navigation, combined filters, replay, comparison and responsive layout", async ({
  page,
}) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/?preview=1");
  await expect(
    page.getByRole("heading", { name: "대시보드", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("54", { exact: true })).toBeVisible();
  await page.screenshot({
    path: test.info().outputPath("dashboard.png"),
    fullPage: true,
  });
  await page.getByLabel("모드 필터").selectOption("block");
  await expect(page.getByText(/총 13개/)).toBeVisible();
  await page.getByLabel("시나리오 필터").selectOption("DEMO-CL");
  await expect(page.getByText(/총 3개/)).toBeVisible();
  await page
    .getByRole("button", { name: "코드 버그 수정 · 교착판", exact: true })
    .first()
    .click();
  await expect(page.getByRole("heading", { name: /1단계/ })).toBeVisible();
  await page.getByRole("button", { name: "다음 단계", exact: true }).click();
  await expect(page.getByRole("heading", { name: /2단계/ })).toBeVisible();
  await page.getByRole("button", { name: "첫 STOP으로" }).click();
  await expect(page.locator(".stop-message")).toBeVisible();
  for (const name of [
    "비교",
    "통계",
    "시스템 구조",
    "사용 가이드",
    "실시간 모니터",
    "프로젝트",
    "API 키 관리",
    "내 계정",
  ]) {
    await page
      .getByRole("navigation")
      .getByRole("button", { name, exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name, exact: true }).first(),
    ).toBeVisible();
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "메뉴 열기" }).click();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "대시보드", exact: true })
    .click();
  await expect(page.locator(".sidebar")).not.toHaveClass(/expanded/);
  await expect(page.locator(".sidebar")).toBeHidden();
  const dimensions = await page.evaluate(() => ({
    viewport: innerWidth,
    document: document.documentElement.scrollWidth,
    blocks: [
      ...document.querySelectorAll(
        ".app,.main-shell,main,.panel,.metrics,.dashboard-grid,footer,.topbar",
      ),
    ].map((e) => ({
      name: e.className || e.tagName,
      width: e.getBoundingClientRect().width,
      right: e.getBoundingClientRect().right,
    })),
  }));
  expect(dimensions.document, JSON.stringify(dimensions)).toBeLessThanOrEqual(
    dimensions.viewport + 1,
  );
  await page.screenshot({
    path: test.info().outputPath("mobile.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("anonymous session renders React login; server errors do not become demo successes", async ({
  page,
}) => {
  await page.route("**/api/auth/me", (route) =>
    route.fulfill({ status: 401, contentType: "application/json", body: "{}" }),
  );
  await page.goto("/login");
  await expect(
    page.getByRole("heading", { name: "실험 워크스페이스에 로그인" }),
  ).toBeVisible();
  await page.unroute("**/api/auth/me");
  await page.route("**/api/auth/me", (route) =>
    route.fulfill({ status: 503, contentType: "application/json", body: "{}" }),
  );
  await page.reload();
  await expect(page.getByRole("alert")).toContainText("DB");
  await expect(page.getByText("54", { exact: true })).toHaveCount(0);
});

test("authenticated API, project create with CSRF, API failure and polling cleanup", async ({
  page,
}) => {
  const calls = [];
  let created = false;
  let liveCalls = 0;
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const respond = (body, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    if (path === "/api/auth/me") return respond(user);
    if (path === "/api/frontend/reference") return respond(data);
    if (path === "/api/summary")
      return respond({
        headline: data.headline,
        judgement_ms: data.judgement_ms,
      });
    if (path === "/api/sessions")
      return respond({
        count: data.sessions.length,
        sessions: data.sessions.map((s) => ({
          session_id: s.id,
          task: s.task,
          scenario_id: s.scenario,
          variant: s.variant,
          mode: s.mode,
          class: s.class,
          steps: s.steps,
          tokens_total: s.tokens,
          first_stop_step: s.first_stop,
          role_labels: s.roles,
        })),
      });
    if (path.startsWith("/api/sessions/")) {
      const s = data.sessions.find(
        (s) => s.id === decodeURIComponent(path.split("/")[3]),
      );
      if (!s) return respond({}, 404);
      if (path.endsWith("/steps"))
        return respond({
          steps: s.step_rows.map((f) => ({
            step_id: f.n,
            agent: f.agent,
            role: f.role,
            state: f.state,
            state_label: f.label,
            stop: f.stop ? { template_id: f.stop.t, text: f.stop.text } : null,
            blocked: f.blocked,
            tests: f.tests ? { passed: f.tests[0], total: f.tests[1] } : null,
            tokens: { prompt: f.tok, completion: 0 },
            cumulative_tokens: f.cum,
            changed_paths: f.paths,
          })),
        });
      return respond({
        session: { scenario: { korean_name: s.name, title: s.title } },
      });
    }
    if (path === "/api/providers") return respond(providers);
    if (path === "/api/projects" && req.method() === "POST") {
      calls.push(req.postDataJSON());
      created = true;
      return respond({ id: 1, name: "로컬 실험", mode: "observe" }, 201);
    }
    if (path === "/api/projects")
      return respond(
        created
          ? [
              {
                id: 1,
                name: "로컬 실험",
                mode: "observe",
                rule_id: "deadlock-v0.3",
              },
            ]
          : [],
      );
    if (path === "/api/projects/1/sessions")
      return respond({
        items: [
          {
            id: "owned-session",
            scenario_id: 1,
            status: "completed",
            started_at: "2026-11-01T00:00:00Z",
          },
        ],
        total: 1,
        offset: 0,
        limit: 20,
      });
    if (path === "/api/projects/1/sessions/owned-session")
      return respond({
        id: "owned-session",
        scenario_id: 1,
        status: "completed",
        steps_reached: 7,
      });
    if (path === "/api/live/sessions") {
      liveCalls++;
      return respond({ now: new Date().toISOString(), sessions: [] });
    }
    return respond({ detail: "Not found" }, 404);
  });
  await page.goto("/");
  await expect(page.locator(".data-toolbar")).toContainText(
    "DB 조회 API · 집계와 세션",
  );
  await page.locator(".table-link").first().click();
  await expect(page.getByRole("heading", { name: /1단계/ })).toBeVisible();
  await page.getByRole("button", { name: "다음 단계", exact: true }).click();
  await expect(page.getByRole("heading", { name: /2단계/ })).toBeVisible();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "프로젝트", exact: true })
    .click();
  await page.getByLabel("프로젝트 이름").fill("로컬 실험");
  await page
    .getByRole("button", { name: "프로젝트 만들기", exact: true })
    .click();
  await expect(page.getByRole("button", { name: /로컬 실험/ })).toBeVisible();
  expect(calls).toEqual([
    {
      name: "로컬 실험",
      mode: "observe",
      provider: "openai",
      csrf_token: user.csrf_token,
    },
  ]);
  await page.getByRole("button", { name: /로컬 실험/ }).click();
  await page
    .getByRole("button", { name: "owned-session", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "프로젝트 세션 상세" }),
  ).toBeVisible();
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "실시간 모니터", exact: true })
    .click();
  await expect.poll(() => liveCalls).toBeGreaterThan(0);
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "대시보드", exact: true })
    .click();
  const count = liveCalls;
  await page.waitForTimeout(2800);
  expect(liveCalls).toBe(count);
  await page.route("**/api/summary", (route) =>
    route.fulfill({ status: 503, contentType: "application/json", body: "{}" }),
  );
  await page.getByRole("button", { name: "새로고침", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("DB");
});

test("C-19-10: sign-in config from the server, live monitor waits for the gateway, sign-out with the session CSRF", async ({
  page,
}) => {
  let signedIn = false;
  let liveCalls = 0;
  const signouts = [];
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const respond = (body, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    if (path === "/api/auth/config")
      return respond({
        google_client_id: "",
        login_uri: "",
        open_signup: true,
      });
    if (path === "/api/auth/me")
      return signedIn
        ? respond(user)
        : respond({ detail: "Login required" }, 401);
    if (path === "/api/auth/signout") {
      signouts.push(req.postDataJSON());
      signedIn = false;
      return respond({ login_required: true });
    }
    if (path === "/api/frontend/reference") return respond(data);
    if (path === "/api/summary")
      return respond({
        headline: data.headline,
        judgement_ms: data.judgement_ms,
      });
    if (path === "/api/sessions") return respond({ count: 0, sessions: [] });
    if (path.startsWith("/api/live/")) {
      liveCalls++;
      return respond({ detail: "Not Found" }, 404);
    }
    return respond({ detail: "Not found" }, 404);
  });
  await page.goto("/login");
  await expect(
    page.getByRole("heading", { name: "실험 워크스페이스에 로그인" }),
  ).toBeVisible();
  await expect(
    page.getByText("Google 클라이언트 ID가 필요합니다"),
  ).toBeVisible();
  signedIn = true;
  await page.goto("/#live");
  await expect(page.getByText("서버 게이트웨이 준비 중")).toBeVisible();
  await page.waitForTimeout(3000);
  expect(liveCalls).toBe(1);
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "내 계정", exact: true })
    .click();
  await page.getByRole("button", { name: "로그아웃", exact: true }).click();
  await page.waitForURL("**/login");
  expect(signouts).toEqual([{ csrf_token: user.csrf_token }]);
  await expect(
    page.getByRole("heading", { name: "실험 워크스페이스에 로그인" }),
  ).toBeVisible();
});

test("C-19-8K: key issue shows the raw key once, list, revoke, project mode, guide", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const raw = "gk_ab12cd34_" + "x".repeat(43);
  const writes = [];
  let keys = [];
  let mode = "observe";
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const respond = (body, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    if (path === "/api/auth/me") return respond(user);
    if (path === "/api/frontend/reference") return respond(data);
    if (path === "/api/providers") return respond(providers);
    if (path === "/api/projects")
      return respond([
        { id: 5, name: "키 실험", mode, rule_id: "deadlock-v0.3" },
      ]);
    if (path === "/api/projects/5/keys" && req.method() === "POST") {
      writes.push([path, req.postDataJSON()]);
      const key = {
        id: 9,
        project_id: 5,
        key_prefix: "ab12cd34",
        name: "실행기",
        created_at: "2026-11-17T03:00:00Z",
        last_used_at: null,
        revoked_at: null,
        revoked: false,
      };
      keys = [key];
      return respond({ ...key, api_key: raw }, 201);
    }
    if (path === "/api/projects/5/keys") return respond(keys);
    if (path === "/api/keys/9/revoke") {
      writes.push([path, req.postDataJSON()]);
      keys = [
        { ...keys[0], revoked: true, revoked_at: "2026-11-17T04:00:00Z" },
      ];
      return respond({ id: 9, revoked: true });
    }
    if (path === "/api/projects/5" && req.method() === "PATCH") {
      writes.push([path, req.postDataJSON()]);
      mode = req.postDataJSON().mode;
      return respond({
        id: 5,
        name: "키 실험",
        mode,
        rule_id: "deadlock-v0.3",
      });
    }
    return respond({ detail: "Not found" }, 404);
  });
  await page.goto("/#keys");
  await expect(
    page.getByText("이 프로젝트에 발급한 키가 없습니다"),
  ).toBeVisible();
  await expect(page.getByText("서버 API 미제공")).toHaveCount(0);
  await page.getByLabel("키 이름").fill("실행기");
  await page.getByRole("button", { name: "키 발급", exact: true }).click();
  await expect(page.getByLabel("새 API 키")).toHaveText(raw);
  await expect(
    page.getByText("다시 볼 수 없습니다", { exact: false }),
  ).toBeVisible();
  await page.getByRole("button", { name: "복사", exact: true }).click();
  await expect(page.getByRole("button", { name: "복사됨" })).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(raw);
  await expect(page.locator("td.mono")).toHaveText("gk_ab12cd34_…");
  await page.getByRole("button", { name: "확인했습니다" }).click();
  await expect(page.getByText(raw)).toHaveCount(0);
  expect(await page.content()).not.toContain(raw);
  expect(
    await page.evaluate(() =>
      JSON.stringify({ ...localStorage, ...sessionStorage }),
    ),
  ).not.toContain("gk_");
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "폐기", exact: true }).click();
  await expect(page.locator(".badge.blocked")).toContainText("폐기");
  await page
    .getByRole("navigation")
    .getByRole("button", { name: "프로젝트", exact: true })
    .click();
  await page.getByRole("button", { name: /키 실험/ }).click();
  await expect(page.getByRole("radio", { name: /관측/ })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  await page.getByRole("radio", { name: /차단/ }).click();
  await expect(page.getByRole("radio", { name: /차단/ })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  expect(writes).toEqual([
    ["/api/projects/5/keys", { name: "실행기", csrf_token: user.csrf_token }],
    ["/api/keys/9/revoke", { csrf_token: user.csrf_token }],
    ["/api/projects/5", { mode: "block", csrf_token: user.csrf_token }],
  ]);
  await page.goto("/?preview=1#guide");
  await expect(
    page.getByRole("heading", { name: /API 키 발급/ }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: /모델 호출 주소 연결/ }),
  ).toContainText("서버 게이트웨이 준비 중");
  await expect(page.getByText("API 키 관리 HTTP API가 없습니다")).toHaveCount(
    0,
  );
});

test("C-19-8P: pick the LLM provider on create, see it in the list, change it", async ({
  page,
}) => {
  const writes = [];
  let list = [];
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const respond = (body, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    if (path === "/api/auth/me") return respond(user);
    if (path === "/api/frontend/reference") return respond(data);
    if (path === "/api/providers") return respond(providers);
    if (path === "/api/projects" && req.method() === "POST") {
      const body = req.postDataJSON();
      writes.push([path, body]);
      const project = {
        id: 7,
        name: body.name,
        mode: body.mode,
        provider: body.provider,
        rule_id: "deadlock-v0.3",
      };
      list = [project];
      return respond(project, 201);
    }
    if (path === "/api/projects") return respond(list);
    if (path === "/api/projects/7" && req.method() === "PATCH") {
      const body = req.postDataJSON();
      writes.push([path, body]);
      list = [{ ...list[0], provider: body.provider }];
      return respond(list[0]);
    }
    if (path === "/api/projects/7/sessions")
      return respond({ items: [], total: 0, offset: 0, limit: 20 });
    return respond({ detail: "Not found" }, 404);
  });
  await page.goto("/#projects");
  const picker = page.getByLabel("LLM 제공자");
  await expect(picker).toHaveValue("openai");
  await expect(picker.locator("optgroup")).toHaveCount(3);
  expect(
    await picker.locator("optgroup").evaluateAll((g) => g.map((x) => x.label)),
  ).toEqual(["해외", "국내", "팀 데모"]);
  await expect(picker.locator('option[value="kmu"]')).toBeDisabled();
  await picker.selectOption("anthropic");
  await page.getByLabel("프로젝트 이름").fill("제공자 실험");
  await page
    .getByRole("button", { name: "프로젝트 만들기", exact: true })
    .click();
  const item = page.getByRole("button", { name: /제공자 실험/ });
  await expect(item).toContainText("Anthropic (Claude)");
  await item.click();
  const change = page.getByLabel("바꿀 제공자");
  await expect(change).toHaveValue("anthropic");
  const save = page.getByRole("button", { name: "제공자 저장", exact: true });
  await expect(save).toBeDisabled();
  await change.selectOption("upstage");
  await save.click();
  await expect(page.getByRole("button", { name: /제공자 실험/ })).toContainText(
    "Upstage (Solar)",
  );
  expect(writes).toEqual([
    [
      "/api/projects",
      {
        name: "제공자 실험",
        mode: "observe",
        provider: "anthropic",
        csrf_token: user.csrf_token,
      },
    ],
    ["/api/projects/7", { provider: "upstage", csrf_token: user.csrf_token }],
  ]);
  const html = await page.content();
  expect(html).not.toContain("https://api.");
  await page.goto("/?preview=1#guide");
  await expect(
    page.getByText("MAG 는 그 키를 저장하지 않고 전달만", { exact: false }),
  ).toBeVisible();
});
