import { useEffect, useState } from "react";
import { Rocket, Save } from "lucide-react";
import { api } from "../lib/api";
import type { ReleaseStatus } from "../lib/types";
import { fmtAgoTs } from "../lib/utils";
import { Badge, Button, Card, ConfirmDialog, Empty, Input, KV, Select, Switch } from "../components/ui";
import { PageHeader } from "../components/AppShell";
import { useToast } from "../components/toast";

const MODES = ["follow", "dry", "semi", "full", "yolo"];
const MODE_LABEL: Record<string, string> = {
  follow: "跟随项目/全局",
  dry: "DRY_RUN（只出计划）",
  semi: "SEMI（PR，人工 tag）",
  full: "FULL（PR，人工 tag）",
  yolo: "YOLO（全自动发布）",
};

export function ReleasePage() {
  const { push } = useToast();
  const [rows, setRows] = useState<ReleaseStatus[]>([]);
  const [running, setRunning] = useState("");
  const [drafts, setDrafts] = useState<Record<string, any>>({});
  const [confirmRun, setConfirmRun] = useState<ReleaseStatus | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    async function tick() {
      try {
        const st = await api.get("/api/release/status");
        if (!alive) return;
        setRows(st.projects || []);
        setRunning(st.running || "");
      } catch {
        /* noop */
      }
    }
    tick();
    const id = window.setInterval(tick, 8000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  function merged(repo: string, row?: ReleaseStatus) {
    const cfg = drafts[repo] ||
      row?.cfg || {
        time_enabled: false, interval_hours: 24, count_enabled: false,
        count_threshold: 5, mode: "follow",
      };
    return cfg;
  }
  function patchDraft(repo: string, patch: any) {
    setDrafts((ds) => ({ ...ds, [repo]: { ...merged(repo, rows.find((r) => r.repo === repo)), ...patch } }));
  }

  async function save(repo: string) {
    const r = await api.post("/api/release/cfg", { repo, release_cfg: merged(repo, rows.find((x) => x.repo === repo)) });
    if (r.ok) push({ kind: "ok", title: "发布配置已保存", body: repo });
    else push({ kind: "err", title: "保存失败", body: r.error || "" });
  }

  async function runNow(row: ReleaseStatus) {
    setBusy(true);
    try {
      const r = await api.post("/api/release/run", { repo: row.repo });
      if (r.ok) push({ kind: "info", title: "发布已启动", body: `${row.repo} — 完成后请看日志/通知` });
      else push({ kind: "err", title: "发布未启动", body: r.error || "" });
      setConfirmRun(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <PageHeader
        title="Release"
        subtitle="条件触发（时间 OR 数量，独立开关）· workflow 优先 · 发布后双计数重置"
        actions={running ? <Badge tone="warn">发布运行中：{running}</Badge> : <Badge tone="dim">空闲</Badge>}
      />

      {rows.map((row) => {
        const d = merged(row.repo, row);
        const st = row.state || {};
        return (
          <Card key={row.repo} className="mb-4" pad>
            <div className="mb-3 flex flex-wrap items-center gap-2">
              <Rocket size={15} className="text-acc" />
              <span className="font-medium">{row.repo}</span>
              {row.due ? <Badge tone="warn">条件已达成</Badge> : <Badge tone="dim">未触发</Badge>}
              {row.reasons?.map((r, i) => <span key={i} className="text-xs text-dim">· {r}</span>)}
              <span className="ml-auto text-xs text-dim">
                自上次发布已合并 {row.merged_since ?? 0} 个 PR
              </span>
            </div>

            {running === row.repo && (
              <div className="panel2 mb-3 p-3 text-xs">
                <div className="sec-label mb-1">发布进行中（每 8 秒刷新）</div>
                {(row.progress?.steps || []).map((s, i) => <div key={i} className="text-dim">· {s}</div>)}
                {row.progress?.ts ? (
                  <div className="mt-1">
                    已进行 {Math.max(1, Math.round((Date.now() / 1000 - row.progress.ts) / 60))} 分钟
                    （完成后有系统通知，离开本页也会在任意页面收到 toast）
                  </div>
                ) : null}
              </div>
            )}

            <div className="mb-3 grid gap-3 md:grid-cols-2">
              {/* 触发条件 */}
              <div className="panel2 space-y-2 p-3">
                <div className="sec-label">触发条件（OR）</div>
                <div className="flex items-center gap-2">
                  <Switch
                    checked={!!d.time_enabled}
                    onChange={(v) => patchDraft(row.repo, { time_enabled: v })}
                    label="时间条件"
                  />
                  <Input
                    className="w-[64px]"
                    type="number"
                    min={1}
                    value={d.interval_hours}
                    onChange={(e) => patchDraft(row.repo, { interval_hours: parseInt(e.target.value || "24") })}
                  />
                  <span className="text-xs text-dim">小时（且需有新 commit）</span>
                </div>
                <div className="flex items-center gap-2">
                  <Switch
                    checked={!!d.count_enabled}
                    onChange={(v) => patchDraft(row.repo, { count_enabled: v })}
                    label="数量条件"
                  />
                  <Input
                    className="w-[64px]"
                    type="number"
                    min={1}
                    value={d.count_threshold}
                    onChange={(e) => patchDraft(row.repo, { count_threshold: parseInt(e.target.value || "5") })}
                  />
                  <span className="text-xs text-dim">个已合并 PR</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-dim">发布档位</span>
                  <Select
                    className="text-xs"
                    value={d.mode}
                    onChange={(e) => patchDraft(row.repo, { mode: e.target.value })}
                  >
                    {MODES.map((m) => (
                      <option key={m} value={m}>{MODE_LABEL[m]}</option>
                    ))}
                  </Select>
                </div>
                <div className="flex items-center gap-2 pt-1">
                  <Button size="sm" variant="outline" onClick={() => save(row.repo)}>
                    <Save size={12} /> 保存配置
                  </Button>
                  <Button
                    size="sm"
                    variant={d.mode === "yolo" ? "danger" : "primary"}
                    onClick={() => setConfirmRun(row)}
                    disabled={!!running}
                  >
                    <Rocket size={12} /> 立即发布
                  </Button>
                </div>
              </div>

              {/* 状态 */}
              <div className="panel2 space-y-1.5 p-3">
                <div className="sec-label">上次发布</div>
                {st.last_ts ? (
                  <>
                    <KV k="时间">{new Date(st.last_ts * 1000).toLocaleString()}</KV>
                    <KV k="SHA">{st.last_sha ? String(st.last_sha).slice(0, 10) : "—"}</KV>
                  </>
                ) : (
                  <div className="text-xs text-dim">尚未发布过（首次达到条件即触发）</div>
                )}
                {st.last_result && !st.last_result.ok ? (
                  <div className="mt-1 text-xs text-err">
                    ❌ 上次发布失败：{st.last_result.error || st.last_error || "未知原因"}
                    {st.last_result.ts ? <span className="ml-1 text-dim">（{fmtAgoTs(new Date(st.last_result.ts * 1000).toISOString())}）</span> : null}
                  </div>
                ) : st.last_result?.ok && !st.last_result.dry ? (
                  <div className="mt-1 text-xs text-ok">
                    ✅ 上次发布成功{st.last_result.tag ? `：${st.last_result.tag}` : ""}
                    {st.last_result.ts ? <span className="ml-1 text-dim">（{fmtAgoTs(new Date(st.last_result.ts * 1000).toISOString())}）</span> : null}
                  </div>
                ) : st.last_error ? (
                  <div className="mt-1 text-xs text-err">
                    ⚠ 上次发布失败：{st.last_error}
                    {st.last_error_ts ? <span className="ml-1 text-dim">（{fmtAgoTs(new Date(st.last_error_ts * 1000).toISOString())}）</span> : null}
                  </div>
                ) : null}
                <div className="sec-label pt-2">检测到的发布 workflow</div>
                {row.workflows?.length ? (
                  <div className="flex flex-wrap gap-1.5">
                    {row.workflows.map((w) => <Badge key={w} tone="acc">{w}</Badge>)}
                  </div>
                ) : (
                  <div className="text-xs text-dim">无 workflow —— 将走 Release.md 路径（缺 Release.md 时提示初始化）</div>
                )}
                {row.error && <div className="text-xs text-err">⚠ {row.error}</div>}
              </div>
            </div>
          </Card>
        );
      })}
      {!rows.length && (
        <Card>
          <Empty>暂无项目</Empty>
        </Card>
      )}

      <ConfirmDialog
        open={!!confirmRun}
        danger={merged(confirmRun?.repo || "").mode === "yolo"}
        title={`立即发布 ${confirmRun?.repo}？`}
        confirmText="开始发布"
        busy={busy}
        onClose={() => setConfirmRun(null)}
        onConfirm={() => confirmRun && runNow(confirmRun)}
      >
        按当前配置（档位 {merged(confirmRun?.repo || "").mode}）立即执行一次发布流程。
        {merged(confirmRun?.repo || "").mode === "yolo"
          ? " YOLO 档位将全自动创建 tag 与 GitHub Release。"
          : " 非 YOLO 档位只到 PR / 计划。"
        }
      </ConfirmDialog>
    </div>
  );
}
