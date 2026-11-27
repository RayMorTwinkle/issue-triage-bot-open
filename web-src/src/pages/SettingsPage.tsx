import { useEffect, useRef, useState } from "react";
import { Save, Sparkles } from "lucide-react";
import { api } from "../lib/api";
import { Button, Card, Chip, ConfirmDialog, Input, KV, Select, Switch } from "../components/ui";
import { PageHeader } from "../components/AppShell";
import { useToast } from "../components/toast";
import { navigate } from "../lib/router";

const MODES = ["dry", "semi", "full", "yolo", "paused"];
const MODE_LABEL: Record<string, string> = {
  dry: "🧪 DRY_RUN", semi: "🟠 SEMI", full: "🚀 FULL", yolo: "☠️ YOLO", paused: "⏸ 暂停",
};
const CLEAN_ROWS = [
  ["s1", "S1 修复/分诊"], ["s2", "S2 bug 审查"], ["s3", "S3 最小性"],
  ["s4", "S4 规范"], ["s5", "S5 终审"], ["s6", "S6 回复"],
];

export function SettingsPage() {
  const { push } = useToast();
  const [ov, setOv] = useState<any>({});
  const [settings, setSettings] = useState<any>({});
  const [limit, setLimit] = useState("3");
  const [confirmLimit, setConfirmLimit] = useState(false);
  const [confirmYolo, setConfirmYolo] = useState(false);
  const loaded = useRef(false);

  async function load() {
    const [a, b, c] = await Promise.all([
      api.get("/api/overview"), api.get("/api/settings"), api.get("/api/agent-limit"),
    ]);
    setOv(a);
    setSettings(b);
    if (!loaded.current) {
      loaded.current = true;
      setLimit(String(c.limit ?? 3));
    }
  }
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function saveSettings(patch?: any) {
    await api.post("/api/settings", patch || {
      disclosure_enabled: settings.disclosure_enabled,
      repo_intro: settings.repo_intro,
      oc_variant: settings.oc_variant,
    });
    push({ kind: "ok", title: "设置已保存" });
    load();
  }

  const cleanup: string[] = ov.cleanup_sessions || [];

  return (
    <div>
      <PageHeader
        title="设置"
        subtitle="全局默认档位 · 并发 · 清理策略 · 其余环境项见「环境」页"
        actions={
          <Button variant="ghost" size="sm" onClick={() => navigate("#/wizard")}>
            <Sparkles size={12} /> 重新运行向导
          </Button>
        }
      />

      <Card title="全局默认档位（新 issue / 新项目继承）" className="mb-4" pad>
        <div className="flex flex-wrap items-center gap-2">
          {MODES.map((m) => (
            <Button
              key={m}
              size="sm"
              variant={ov.global_mode === m ? (m === "paused" || m === "yolo" ? "danger" : "primary") : "subtle"}
              onClick={() => (m === "yolo" ? setConfirmYolo(true) : api.post("/api/mode", { mode: m }).then(load))}
            >
              {MODE_LABEL[m]}
            </Button>
          ))}
          <span className="ml-2 text-xs text-dim">
            当前：{ov.global_mode}（issue 可单独覆盖；项目可设中间层）
          </span>
        </div>
      </Card>

      <div className="mb-4 grid gap-3 lg:grid-cols-2">
        <Card title="轮询与并发" pad>
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <span className="role-meta w-28">轮询间隔（秒）</span>
              <Input
                className="w-[90px]"
                type="number"
                defaultValue={ov.poll_interval_sec}
                onBlur={(e) => api.post("/api/interval", { sec: parseInt(e.target.value || "0") }).then(load)}
              />
              <span className="text-xs text-dim">0 = 仅手动</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="role-meta w-28">agent 并发上限</span>
              <Input className="w-[90px]" type="number" min={1} max={99} value={limit} onChange={(e) => setLimit(e.target.value)} />
              <Button
                size="sm"
                variant="outline"
                onClick={() => (parseInt(limit) > 6 ? setConfirmLimit(true) : saveLimit())}
              >
                <Save size={12} /> 应用
              </Button>
            </div>
            <div className="role-meta">
              闸门：per-call 占用/释放（三路审查各占各放）· 建议 3；&gt;6 可能超出机器/接口承载
            </div>
            <KV k="闸门状态">
              {(() => { const g = ov.agent_gate || {}; return `运行 ${g.active ?? 0} / 上限 ${g.limit ?? 3} · 排队 ${g.waiting ?? 0} · 累计 ${g.total_acquired ?? 0}`; })()}
            </KV>
          </div>
        </Card>

        <Card title="Session 清理（issue 结束后自动删除，needs_human 保留）" pad>
          <div className="flex flex-wrap gap-2">
            {CLEAN_ROWS.map(([k, label]) => {
              const on = cleanup.includes(k);
              return (
                <Chip
                  key={k}
                  on={on}
                  onClick={async () => {
                    const next = on ? cleanup.filter((x) => x !== k) : [...cleanup, k];
                    const r = await api.post("/api/cleanup-sessions", { keys: next });
                    setOv((o: any) => ({ ...o, cleanup_sessions: r.cleanup_sessions }));
                  }}
                >
                  {on ? "✓ " : ""}{label}
                </Chip>
              );
            })}
          </div>
          <div className="role-meta mt-2">
            清理调用 opencode2 API 删除 session 数据；记录仍保留在 Sessions 页可追溯
          </div>
        </Card>
      </div>

      <Card title="其他" pad>
        <div className="space-y-3">
          <Switch
            checked={!!settings.disclosure_enabled}
            onChange={(v) => { setSettings((s: any) => ({ ...s, disclosure_enabled: v })); }}
            label="回复带 AI 披露头（🤖 由 AI 自动处理的声明）"
          />
          <div className="flex items-center gap-2">
            <span className="role-meta w-28">项目简介</span>
            <Input
              className="max-w-[560px] flex-1"
              value={settings.repo_intro || ""}
              onChange={(e) => setSettings((s: any) => ({ ...s, repo_intro: e.target.value }))}
              placeholder="供问题审核参考的一句话项目介绍（多项目时以项目设置为准）"
            />
          </div>
          <div className="flex items-center gap-2">
            <span className="role-meta w-28">oc-run2 推理强度</span>
            <Select
              className="text-xs"
              value={settings.oc_variant || "low"}
              onChange={(e) => setSettings((s: any) => ({ ...s, oc_variant: e.target.value }))}
            >
              <option value="low">low（快，推荐）</option>
              <option value="high">high</option>
              <option value="max">max（慢但强）</option>
              <option value="">不指定（极慢，不推荐）</option>
            </Select>
            <span className="text-xs text-dim">影响 opencode2 直连的推理档</span>
          </div>
          <Button size="sm" onClick={() => saveSettings()}>
            <Save size={12} /> 保存
          </Button>
        </div>
      </Card>

      <ConfirmDialog
        open={confirmLimit}
        title={`将并发上限设为 ${limit}？`}
        confirmText="仍然应用"
        onClose={() => setConfirmLimit(false)}
        onConfirm={async () => { await saveLimit(); setConfirmLimit(false); }}
      >
        超过 6 个并发 agent 可能压垮本机或触发接口限流（3 为推荐值）。
      </ConfirmDialog>

      <ConfirmDialog
        open={confirmYolo}
        danger
        title="切换到 YOLO 档位？"
        confirmText="我确认"
        onClose={() => setConfirmYolo(false)}
        onConfirm={async () => { await api.post("/api/mode", { mode: "yolo" }); setConfirmYolo(false); load(); }}
      >
        新 issue 将自动合并 PR 并关闭 —— 不可逆。
      </ConfirmDialog>
    </div>
  );

  async function saveLimit() {
    const r = await api.post("/api/agent-limit", { limit: parseInt(limit || "3") });
    push({ kind: "ok", title: `并发上限已设为 ${r.limit}` });
    load();
  }
}
