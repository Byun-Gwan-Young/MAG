export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export async function request(path, { signal, ...options } = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    signal: signal
      ? AbortSignal.any([signal, AbortSignal.timeout(12000)])
      : AbortSignal.timeout(12000),
    ...options,
    headers: { Accept: "application/json", ...options.headers },
  });
  if (!response.ok) {
    const messages = {
      401: "로그인이 필요하거나 세션이 만료되었습니다.",
      403: "접근 권한 또는 계정 상태를 확인해 주세요.",
      404: "요청한 기록을 찾을 수 없습니다.",
      503: "서버 또는 DB에 연결할 수 없습니다.",
    };
    throw new ApiError(
      messages[response.status] || `요청에 실패했습니다 (${response.status}).`,
      response.status,
    );
  }
  if (!response.headers.get("content-type")?.includes("application/json")) {
    throw new ApiError(
      "API 대신 HTML이 반환됐습니다. 서버 연결과 로그인 상태를 확인해 주세요.",
      response.redirected ? 401 : 502,
    );
  }
  return response.json();
}

export const post = (path, body) =>
  request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

// C-19-8K: PATCH /api/projects/{id} (project mode).
export const patch = (path, body) =>
  request(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

// C-19-10 (DM-114): sign out with the per-session CSRF token /api/auth/me returned.
// The token stays in memory only; never copy it to storage or logs.
export async function logout(csrfToken) {
  await post("/api/auth/signout", { csrf_token: csrfToken });
  location.assign("/login");
}
