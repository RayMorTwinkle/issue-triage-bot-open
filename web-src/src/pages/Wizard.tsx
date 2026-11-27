import { useEffect, useState } from "react";
import {
  CheckCircle2, ChevronLeft, ChevronRight, Copy, KeyRound, RefreshCcw,
  ScrollText, SkipForward, Sparkles, TerminalSquare, Wand2,
} from "lucide-react";
import { api } from "../lib/api";
import type { Project } from "../lib/types";
import { copyText } from "../lib/utils";
import { Badge, Button, Card, Empty, Input, Select } from "../components/ui";
import { useToast } from "../components/toast";

const STEPS = ["欢迎", "GitHub 认证", "接入项目", "opencode2", "LLM 配置", "完成"];

export function Wizard({ onDone }: { onDone: () => void }) {
  const { push } = useToast();
  const [step, setStep] = useState(0);
  const [gh, setGh] = useState<any>(null);
  const [oc, setOc] = useState<any>(null);
  const [env, setEnv] = useState<Record<string, string>>({});
  const [projects, setProjects] = useState<Project[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [ghToken, setGhToken] = useState("");
  const [newRepo, setNewRepo] = useState("");
  const [newIntro, setNewIntro] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try {
      const [g, o, e, p] = await Promise.all([
        api.get("/api/wizard/gh"),
        api.get("/api/wizard/opencode"),
        api.get("/api/env"),
        api.get("/api/projects"),
      ]);
      setGh(g); setOc(o); setEnv(e.env || {}); setProjects(p.projects || []);
    } catch {
      /* noop */
    }
  }
  useEffect(() => {
    refresh();
  }, []);

  async function finish() {
    await api.post("/api/wizard/complete", {});
    onDone();
  }

  async function saveLlm() {
    setBusy(true);
    try {
      const cred = (v?: string) => (v && !v.includes("*") ? v : undefined);
      const r = await api.post("/api/env/save", {
        LLM_API_BASE: env.LLM_API_BASE || "",
        LLM_API_KEY: cred(env.LLM_API_KEY),
        LLM_MODEL: env.LLM_MODEL || "",
      });
      push({
        kind: r.ok ? "ok" : "err",
        title: r.ok ? "LLM 配置已保存（.env）" : "保存失败",
        body: r.ok ? "重启服务后生效" : r.error || "",
      });
    } finally {
      setBusy(false);
    }
  }

  async function fetchModels() {
    const r = await api.get("/api/models");
    if (r.ok) {
      setModels(r.models || []);
      push({ kind: "ok", title: `拉取到 ${r.models.length} 个模型` });
    } else {
      setModels([]);
      push({ kind: "err", title: "拉取失败", body: r.error || "" });
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-10">
      <div className="w-full max-w-[720px]">
        <div className="mb-4 flex items-center gap-2">
          <Sparkles size={18} className="text-acc" />
          <span className="font-display text-lg font-bold">初始化向导</span>
          <span className="ml-auto flex items-center gap-1">
            {STEPS.map((s, i) => (
              <button
                key={s}
                onClick={() => setStep(i)}
                className={`h-1.5 w-8 cursor-pointer transition-colors ${i <= step ? "bg-acc" : "bg-line"}`}
                title={s}
              />
            ))}
          </span>
        </div>

        <Card className="fadeup" pad>
          <div className="sec-label mb-1">
            步骤 {step + 1} / {STEPS.length} · {STEPS[step]}
          </div>

          {step === 0 && (
            <div className="space-y-3">
              <h2 className="font-display text-xl font-bold">欢迎使用 issue-triage-bot 👋</h2>
              <p className="text-sm text-dim">
                本向导带你完成四件事：GitHub 认证 → 接入被监控项目 → 检测 opencode2 → 配置 LLM。
                全部可跳过，之后可在「设置」里重新运行。
              </p>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="panel2 p-3">
                  <div className="text-dim">GitHub 认证</div>
                  <div className={gh?.authed ? "text-ok" : "text-warn"}>
                    {gh?.authed ? `✅ 已登录 ${gh.user || ""}` : "⚠ 未检测到（可配 GH_TOKEN）"}
                  </div>
                </div>
                <div className="panel2 p-3">
                  <div className="text-dim">opencode2</div>
                  <div className={oc?.installed ? "text-ok" : "text-warn"}>
                    {oc?.installed ? `✅ ${oc.version || "已安装"}` : "⚠ 未安装"}
                  </div>
                </div>
                <div className="panel2 p-3">
                  <div className="text-dim">LLM 配置</div>
                  <div className={env.LLM_API_BASE && env.LLM_API_KEY ? "text-ok" : "text-warn"}>
                    {env.LLM_API_BASE && env.LLM_API_KEY ? "✅ 已配置" : "⚠ 未配置"}
                  </div>
                </div>
                <div className="panel2 p-3">
                  <div className="text-dim">已接入项目</div>
                  <div className={projects.length ? "text-ok" : "text-warn"}>
                    {projects.length ? `✅ ${projects.length} 个` : "⚠ 暂无"}
                  </div>
                </div>
              </div>
            </div>
          )}

          {step === 1 && (
            <div className="space-y-3">
              <h2 className="font-display text-lg font-bold">GitHub 认证</h2>
              {gh?.authed ? (
                <div className="border border-ok/60 p-3 text-sm">
                  <CheckCircle2 size={14} className="mr-1 inline text-ok" />
                  已检测到 gh 认证（{gh.user || "已登录"}）—— 可以跳过本步。
                </div>
              ) : (
                <>
                  <p className="text-sm text-dim">
                    未检测到 <code>gh auth login</code>。两种方式任选：
                  </p>
                  <div className="panel2 p-3 text-xs">
                    <div className="mb-1 text-dim">方式 A（终端）</div>
                    <code>gh auth login</code>
                  </div>
                  <div className="panel2 p-3 text-xs">
                    <div className="mb-1 text-dim">方式 B（推荐无 gh 的机器）：配置 GH_TOKEN</div>
                    <div className="flex gap-2">
                      <Input
                        className="flex-1"
                        type="password"
                        placeholder="ghp_… 或 github_pat_…"
                        value={ghToken}
                        onChange={(e) => setGhToken(e.target.value)}
                      />
                      <Button
                        size="sm"
                        onClick={async () => {
                          const r = await api.post("/api/env/save", { GH_TOKEN: ghToken.trim() });
                          if (r.ok) {
                            push({ kind: "ok", title: "GH_TOKEN 已写入 .env", body: "重启服务后生效" });
                            setGhToken("");
                            refresh();
                          }
                        }}
                      >
                        <KeyRound size={12} /> 保存
                      </Button>
                    </div>
                    <div className="mt-1 text-dim">写入后重启服务生效（install 脚本会自动重启）</div>
                  </div>
                </>
              )}
              <Button size="sm" variant="ghost" onClick={refresh}>
                <RefreshCcw size={12} /> 重新检测
              </Button>
            </div>
          )}

          {step === 2 && (
            <div className="space-y-3">
              <h2 className="font-display text-lg font-bold">接入被监控项目</h2>
              {projects.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {projects.map((p) => <Badge key={p.repo} tone="ok">{p.repo}</Badge>)}
                </div>
              )}
              <div className="flex gap-2">
                <Input className="w-[260px]" placeholder="owner/name" value={newRepo} onChange={(e) => setNewRepo(e.target.value)} />
                <Input className="flex-1" placeholder="一句话简介（可选）" value={newIntro} onChange={(e) => setNewIntro(e.target.value)} />
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={async () => {
                    if (!newRepo.includes("/")) {
                      push({ kind: "err", title: "格式：owner/name" });
                      return;
                    }
                    setBusy(true);
                    try {
                      const r = await api.post("/api/projects/add", { repo: newRepo.trim(), intro: newIntro.trim() });
                      if (r.ok) {
                        push({ kind: "ok", title: "已接入", body: r.repo });
                        setNewRepo(""); setNewIntro("");
                        refresh();
                      } else push({ kind: "err", title: "失败", body: r.error || "" });
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  接入
                </Button>
              </div>
              <p className="text-xs text-dim">
                构建命令等可稍后在「项目设置」完善；接入后建议运行「初始化 agent」生成 AGENTS.md 与发布方案。
              </p>
            </div>
          )}

          {step === 3 && (
            <div className="space-y-3">
              <h2 className="font-display text-lg font-bold">opencode2 检测</h2>
              {oc?.installed ? (
                <div className="border border-ok/60 p-3 text-sm">
                  <CheckCircle2 size={14} className="mr-1 inline text-ok" />
                  已安装：<code>{oc.path}</code>（{oc.version || "版本未知"}）
                </div>
              ) : (
                <>
                  <p className="text-sm text-dim">未检测到 opencode2 —— 深度 agent（修复/审查/发布）需要它：</p>
                  <div className="panel2 flex items-center gap-2 p-3 text-xs">
                    <TerminalSquare size={14} className="text-acc" />
                    <code>npm i -g @opencode-ai/cli@beta</code>
                    <Button size="xs" variant="ghost" className="ml-auto" onClick={() => copyText("npm i -g @opencode-ai/cli@beta")}>
                      <Copy size={11} /> 复制
                    </Button>
                  </div>
                  <Button size="sm" variant="ghost" onClick={refresh}>
                    <RefreshCcw size={12} /> 重新检测
                  </Button>
                </>
              )}
            </div>
          )}

          {step === 4 && (
            <div className="space-y-3">
              <h2 className="font-display text-lg font-bold">LLM 配置（方舟 / OpenAI 兼容）</h2>
              <div className="grid gap-2">
                <Input placeholder="LLM_API_BASE（如 https://ark.cn-beijing.volces.com/api/coding/v3）" value={env.LLM_API_BASE || ""} onChange={(e) => setEnv((x) => ({ ...x, LLM_API_BASE: e.target.value }))} />
                <Input placeholder="LLM_API_KEY" type="password" value={env.LLM_API_KEY || ""} onChange={(e) => setEnv((x) => ({ ...x, LLM_API_KEY: e.target.value }))} />
                <div className="flex gap-2">
                  <Select className="flex-1" value={env.LLM_MODEL || ""} onChange={(e) => setEnv((x) => ({ ...x, LLM_MODEL: e.target.value }))}>
                    <option value="">（先拉取模型列表，或手填）</option>
                    {models.map((m) => <option key={m} value={m}>{m}</option>)}
                  </Select>
                  <Button size="sm" variant="outline" onClick={fetchModels}>
                    <RefreshCcw size={12} /> 拉取模型
                  </Button>
                </div>
                {!models.length && (
                  <Input placeholder="LLM_MODEL（手填，如 glm-5.3-flash）" value={env.LLM_MODEL || ""} onChange={(e) => setEnv((x) => ({ ...x, LLM_MODEL: e.target.value }))} />
                )}
              </div>
              <div className="flex items-center gap-2">
                <Button size="sm" onClick={saveLlm} disabled={busy}>
                  <ScrollText size={12} /> 保存到 .env
                </Button>
                <span className="text-xs text-dim">保存后重启服务生效</span>
              </div>
            </div>
          )}

          {step === 5 && (
            <div className="space-y-3">
              <h2 className="font-display text-lg font-bold">🎉 全部就绪</h2>
              <p className="text-sm text-dim">
                向导完成。之后可在左侧「设置 → 重新运行向导」再次打开此页面。
                建议第一步：到「项目」页运行「初始化 agent」，为仓库生成 AGENTS.md 与发布方案。
              </p>
            </div>
          )}

          <div className="mt-5 flex items-center gap-2 border-t border-line pt-4">
            <Button variant="ghost" size="sm" onClick={() => setStep((s) => Math.max(0, s - 1))} disabled={step === 0}>
              <ChevronLeft size={13} /> 上一步
            </Button>
            {step < STEPS.length - 1 ? (
              <Button size="sm" onClick={() => setStep((s) => s + 1)}>
                下一步 <ChevronRight size={13} />
              </Button>
            ) : (
              <Button size="sm" onClick={finish}>
                <Wand2 size={13} /> 进入后台
              </Button>
            )}
            <Button variant="ghost" size="sm" className="ml-auto" onClick={finish}>
              <SkipForward size={12} /> 跳过向导
            </Button>
          </div>
        </Card>
      </div>
    </div>
  );
}
