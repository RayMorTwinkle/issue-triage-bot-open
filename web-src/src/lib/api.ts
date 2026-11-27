const TOKEN_KEY = "botToken";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}

let tokenDeclined = false;   // 用户取消过输入 → 本轮会话不再反复弹窗

export function setToken(t: string): void {
  localStorage.setItem(TOKEN_KEY, t);
  tokenDeclined = false;
}

export function promptToken(): string {
  if (tokenDeclined) return "";
  const t = window.prompt(
    "需要访问令牌：见 .env 的 BOT_TOKEN（或服务启动日志中的「访问令牌」）"
  );
  if (t !== null && t !== "") {
    setToken(t);
    return t;
  }
  tokenDeclined = true;
  return "";
}

// URL ?token= 自动吸收后从地址栏抹掉
(() => {
  try {
    const m = new URLSearchParams(location.search).get("token");
    if (m) {
      setToken(m);
      history.replaceState(null, "", location.pathname + location.hash);
    }
  } catch {
    /* noop */
  }
})();

async function request(path: string, method: string, body?: unknown): Promise<any> {
  const headers: Record<string, string> = { "X-Bot-Token": getToken() };
  const init: RequestInit = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const r = await fetch(path, init);
  if (r.status === 401) {
    const t = promptToken();
    if (t) return request(path, method, body);
    throw new Error("unauthorized");
  }
  return r.json();
}

export const api = {
  get: (p: string) => request(p, "GET"),
  post: (p: string, body?: unknown) => request(p, "POST", body ?? {}),
};
