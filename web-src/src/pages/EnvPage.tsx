import { useEffect, useState } from "react";
import { Save } from "lucide-react";
import { api } from "../lib/api";
import { Button, Card, Input, KV, Switch } from "../components/ui";
import { PageHeader } from "../components/AppShell";
import { useToast } from "../components/toast";

export function EnvPage() {
  const { push } = useToast();
  const [env, setEnv] = useState<Record<string, string>>({});
  const [authEnabled, setAuthEnabled] = useState(true);
  const [authNow, setAuthNow] = useState(true);
  const [busy, setBusy] = useState(false);

  function load() {
    api.get("/api/env").then((d) => {
      setEnv(d.env || {});
      setAuthEnabled((d.env?.BOT_AUTH_ENABLED || "1") === "1");
      setAuthNow(!!d.auth_enabled_now);
    });
  }
  useEffect(load, []);

  function set(k: string, v: string) {
    setEnv((e) => ({ ...e, [k]: v }));
  }

  async function save() {
    setBusy(true);
    try {
      // 打码值（含 *）只是回显占位，不得回传——否则后端会把掩码当真凭据写入 .env
      const cred = (v?: string) => (v && !v.includes("*") ? v : undefined);
      const r = await api.post("/api/env/save", {
        BOT_MODEL: env.BOT_MODEL || "",
        BOT_TOKEN: cred(env.BOT_TOKEN),
        BOT_AUTH_ENABLED: authEnabled,
        LLM_API_BASE: env.LLM_API_BASE || "",
        LLM_API_KEY: cred(env.LLM_API_KEY),
        LLM_MODEL: env.LLM_MODEL || "",
        GH_TOKEN: cred(env.GH_TOKEN),
      });
      push({
        kind: r.ok ? "ok" : "err",
        title: r.ok ? "已写入 .env" : "保存失败",
        body: r.ok ? "重启服务后生效：bash scripts/install_autostart.sh" : r.error || "",
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <PageHeader title="环境与凭据" subtitle=".env 优先于 config.json · 保存后需重启服务生效" />

      <Card title="直连 API（问题审核 / 回复 / 发布等廉价 agent）" className="mb-4" pad>
        <div className="grid items-center gap-3 md:grid-cols-[170px_1fr]">
          <span className="role-meta">LLM_API_BASE</span>
          <Input value={env.LLM_API_BASE || ""} onChange={(e) => set("LLM_API_BASE", e.target.value)} placeholder="https://ark.cn-beijing.volces.com/api/coding/v3" />
          <span className="role-meta">LLM_API_KEY</span>
          <Input value={env.LLM_API_KEY || ""} onChange={(e) => set("LLM_API_KEY", e.target.value)} placeholder="已设置（输入新值覆盖）" />
          <span className="role-meta">LLM_MODEL</span>
          <Input value={env.LLM_MODEL || ""} onChange={(e) => set("LLM_MODEL", e.target.value)} placeholder="glm-5.3-flash" />
        </div>
      </Card>

      <Card title="oc-run2 侧（修复 / 审查等深度 agent）" className="mb-4" pad>
        <div className="grid items-center gap-3 md:grid-cols-[170px_1fr]">
          <span className="role-meta">BOT_MODEL</span>
          <Input value={env.BOT_MODEL || ""} onChange={(e) => set("BOT_MODEL", e.target.value)} placeholder="volcengine/glm-5.3-flash" />
          <span className="role-meta">BOT_TOKEN</span>
          <Input value={env.BOT_TOKEN || ""} onChange={(e) => set("BOT_TOKEN", e.target.value)} placeholder="已设置（输入新值覆盖）" />
          <span className="role-meta">BOT_AUTH_ENABLED</span>
          <Switch checked={authEnabled} onChange={setAuthEnabled} label="开启令牌鉴权（关闭后局域网内任何人可操作）" />
        </div>
      </Card>

      <Card title="GitHub 认证" className="mb-4" pad>
        <div className="grid items-center gap-3 md:grid-cols-[170px_1fr]">
          <span className="role-meta">GH_TOKEN</span>
          <Input
            value={env.GH_TOKEN || ""}
            onChange={(e) => set("GH_TOKEN", e.target.value)}
            placeholder="未设置（优先使用 gh auth 已登录状态；无 gh 认证的机器可在此配置令牌）"
          />
        </div>
        <div className="mt-2 text-xs text-dim">
          若本机已执行 <code>gh auth login</code>，此处可留空。GH_TOKEN 会注入 gh CLI（子进程环境变量）。
        </div>
      </Card>

      <Card pad>
        <div className="flex items-center gap-3">
          <Button size="sm" onClick={save} disabled={busy}>
            <Save size={12} /> 保存全部（.env）
          </Button>
          <Button size="sm" variant="ghost" onClick={load}>重新加载</Button>
          <span className="role-meta">
            当前鉴权：{authNow ? <span className="text-ok">已开启</span> : <span className="text-err">已关闭</span>}
          </span>
        </div>
        <ul className="mt-3 space-y-1 text-xs text-dim">
          <li>· 凭据优先级：.env &gt; config.json；.env 已加入 .gitignore，不会进入仓库</li>
          <li>· 打码值（如 ****）不会覆盖真实凭据；输入新值才生效</li>
          <li>· 保存后重启服务：<code>bash scripts/install_autostart.sh</code></li>
        </ul>
      </Card>
    </div>
  );
}
