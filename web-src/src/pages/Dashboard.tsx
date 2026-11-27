import { lazy, Suspense, useRef, useState, type ReactNode } from "react";
import { CheckCircle2, Pause, RefreshCcw } from "lucide-react";
import { api } from "../lib/api";
import type { AttentionItem, DashboardResp, InFlightItem, Stats } from "../lib/types";
import { cn, fmtAgoTs, fmtMoney, fmtTokens, repoSlug } from "../lib/utils";
import { navigate } from "../lib/router";
import { Badge, Button, Card, ConfirmDialog, Input } from "../components/ui";
import { PageHeader, usePoll } from "../components/AppShell";
import { ActivityStream } from "../components/ActivityStream";
import { Board, Capacity } from "../../replay/Workbench";
import { useToast } from "../components/toast";

const TrendChart = lazy(() => import("../components/TrendChart").then(m => ({ default: m.TrendChart })));

const MODES = ["dry", "semi", "full", "yolo", "paused"] as const;
const MODE_LABEL: Record<string, string> = {
  dry: "🧪 DRY_RUN",
  semi: "🟠 SEMI",
  full: "🚀 FULL",
  yolo: "☠️ YOLO",
  paused: "⏸ 暂停",
};

function SecTitle({ n, tone, hint, children }: {
  n: number; tone?: "err" | "acc"; hint?: string; children: ReactNode;
}) {
  return (
    <div className="mb-3 mt-7 flex items-baseline gap-2.5">
      <h2 className="role-section">{children}</h2>
      <span className={cn("role-section",
        tone === "acc" ? "text-acc" : "text-err")}>{n}</span>
      {hint && <span className="role-meta">{hint}</span>}
    </div>
  );
}

const RAIL: Record<string, string> = { err: "bg-err", warn: "bg-warn", info: "bg-info" };

export function Dashboard() {
  const { push } = useToast();
  const [dash, setDash] = useState<DashboardResp | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [act, setAct] = useState<DashboardResp["agent_gate"]>(
    { limit: 3, active: 0, waiting: 0, total_acquired: 0 });
  const [events, setEvents] = useState<any[]>([]);
  const [sec, setSec] = useState("600");
  const [confirmYolo, setConfirmYolo] = useState(false);
  const loadedRef = useRef(false);

  usePoll(async () => {
    try {
      const [d, a] = await Promise.all([
        api.get("/api/dashboard"),
        api.get("/api/activity?limit=80"),
      ]);
      setDash(d);
      setAct(d.agent_gate);
      setEvents(a.events || []);
      if (!loadedRef.current) {
        loadedRef.current = true;
        setSec(String(d.poll_interval_sec ?? 600));
      }
    } catch {
      /* 未授权等场景静默 */
    }
  }, 4000);

  usePoll(async () => {
    try {
      setStats(await api.get("/api/stats?days=14"));
    } catch {
      /* noop */
    }
  }, 60000);

  const mode = dash?.global_mode || "dry";
  const attention = dash?.attention || [];

  const today = stats?.days?.length ? stats.days[stats.days.length - 1] : null;
  const totals = stats?.totals;

  async function setMode(m: string) {
    if (m === "yolo") {
      setConfirmYolo(true);
      return;
    }
    await api.post("/api/mode", { mode: m });
  }

  async function pollNow() {
    const r = await api.post("/api/poll", {});
    if (r.ok) push({ kind: "info", title: "已触发轮询", body: "后台正在检查各项目的新 issue" });
    else push({ kind: "err", title: "轮询未触发", body: "上一轮轮询仍在进行中，请稍候" });
  }

  function gotoIssue(repo: string, n: number) {
    navigate(`#/i/${repoSlug(repo)}/${n}`);
  }

  async function rerun(it: AttentionItem | InFlightItem) {
    const r = await api.post("/api/rerun", { issue: it.number, repo: it.repo });
    push(r.ok || r.triggered
      ? { kind: "info", title: `已重跑 #${it.number}` }
      : { kind: "err", title: "重跑未触发", body: r.error || "该 issue 正在处理中" });
  }

  async function publish(it: AttentionItem) {
    const r = await api.post("/api/publish", { issue: it.number, repo: it.repo });
    push(r.ok
      ? { kind: "info", title: `补发已启动 #${it.number}`, body: "后台创建分支与 PR" }
      : { kind: "err", title: "补发被拒", body: r.error || "请查看详情页" });
  }

  function blockedTag(updated_at?: string | null): string | null {
    if (!updated_at) return null;
    const t = Date.parse(String(updated_at).replace(" ", "T"));
    if (!t) return null;
    const d = (Date.now() - t) / 86400000;
    return d >= 1 ? `已阻塞 ${Math.floor(d)} 天` : `阻塞于 ${fmtAgoTs(updated_at)}`;
  }

  return (
    <div>
      <PageHeader
        title="分诊工作台"
        subtitle={
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span>档位 {MODE_LABEL[mode] || mode} · 模型 {dash?.model || "—"}</span>
            <span className="text-line2">·</span>
            <span className="inline-flex items-center gap-1">
              轮询
              <Input
                className="w-[64px] !py-0 text-xs"
                type="number"
                min={0}
                value={sec}
                onChange={(e) => setSec(e.target.value)}
              />
              秒
              <button
                className="cursor-pointer text-acc hover:underline"
                onClick={() => api.post("/api/interval", { sec: parseInt(sec || "0") })}
              >
                应用
              </button>
            </span>
          </span>
        }
        actions={
          <>
            {MODES.map((m) => (
              <Button
                key={m}
                size="sm"
                variant={mode === m ? (m === "yolo" || m === "paused" ? "danger" : "primary") : "subtle"}
                onClick={() => setMode(m)}
              >
                {MODE_LABEL[m]}
              </Button>
            ))}
            <a className="text-xs underline" href="/replay/index.html">离线回放</a>
            <Button variant="ghost" size="sm" onClick={pollNow}>
              <RefreshCcw size={12} /> 轮询
            </Button>
          </>
        }
      />

      {dash && <>
        <Capacity gate={act} mode={mode} />
        <div className="section-head"><h2>任务队列</h2><span>最近 {dash.board.length} 个工单</span></div>
        <Board cards={dash.board.map(it => ({ key: it.repo + "#" + it.number,
          number: it.number, title: it.title, stage: it.stage, meta: it.repo }))}
          onSelect={card => gotoIssue(card.meta, card.number)} />
      </>}

      {mode === "paused" && (
        <Card className="mb-2 border-err/60" pad>
          <div className="flex items-center gap-2 text-sm text-err">
            <Pause size={14} />
            <span>
              已暂停：调度、轮询、LLM agent、发布与所有外写全部停止（零 token 消耗）。
              切回任意档位即可恢复。
            </span>
          </div>
        </Card>
      )}

      {/* ============ ① 需要你处理 ============ */}
      <SecTitle n={attention.length} hint="按阻塞时间排序 · 这些是 bot 干不下去、在等你的事">
        需要你处理
      </SecTitle>
      <div className="space-y-2.5">
        {attention.map((it) => {
          const blk = it.kind === "waiting" && it.ttl_sec
            ? (it.waiting_since
                ? `已等 ${fmtAgoTs(new Date(it.waiting_since * 1000).toISOString())} / ${Math.round(it.ttl_sec / 86400)} 天上限`
                : "追问已发出")
            : blockedTag(it.updated_at);
          return (
          <div key={`${it.repo}#${it.number}`} className="glass card-hover flex">
            <div className={cn("w-1 shrink-0", RAIL[it.tone] || "bg-line2")} />
            <div className="min-w-0 flex-1 px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <button
                  className="cursor-pointer font-mono text-sm font-bold hover:underline"
                  onClick={() => gotoIssue(it.repo, it.number)}
                >
                  #{it.number}
                </button>
                <span className="font-medium">{it.title}</span>
                <Badge tone={it.tone}>{it.tag}</Badge>
                <Badge tone="dim">{it.repo.split("/")[1] || it.repo}</Badge>
                {blk && <Badge tone="dim">{blk}</Badge>}
              </div>
              <div className="mt-1 text-xs text-dim">{it.reason}</div>
            </div>
            <div className="flex shrink-0 flex-col justify-center gap-1.5 border-l border-dashed border-line px-4 py-3">
              <Button size="sm" variant="outline" onClick={() => gotoIssue(it.repo, it.number)}>
                {it.kind === "waiting" ? "查看追问" : it.kind === "publishable" ? "查看 diff" : "查看"}
              </Button>
              {it.kind === "publishable" || it.tag === "补发失败" ? (
                <Button size="sm" onClick={() => publish(it)}>
                  {it.tag === "补发失败" ? "重试补发" : "补发 PR"}
                </Button>
              ) : (
                <Button size="sm" variant="ghost" onClick={() => rerun(it)}>重跑</Button>
              )}
            </div>
          </div>
          );
        })}
        {dash && !attention.length && (
          <div className="glass flex items-center justify-center gap-2 py-5 text-sm text-ok">
            <CheckCircle2 size={15} /> 没有需要你处理的事 — bot 运转正常
          </div>
        )}
      </div>

      {/* ============ ③ 概数 + 活动 ============ */}
      <div className="mt-7 grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <Card title="近 14 天" pad>
          <div className="mb-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div>
              <div className="font-display text-2xl font-bold">{today ? today.done : "—"}</div>
              <div className="sec-label">今日完成</div>
            </div>
            <div>
              <div className="font-display text-2xl font-bold text-ok">
                {totals ? totals.merged : "—"}
              </div>
              <div className="sec-label">累计合并</div>
            </div>
            <div>
              <div className="font-display text-2xl font-bold">
                {totals ? fmtMoney(totals.cost) : "—"}
              </div>
              <div className="sec-label">累计成本</div>
            </div>
            <div>
              <div className="font-display text-2xl font-bold">
                {totals ? fmtTokens(totals.tokens) : "—"}
              </div>
              <div className="sec-label">累计 tokens</div>
            </div>
          </div>
          {stats ? <Suspense fallback={<p>正在载入趋势图…</p>}><TrendChart days={stats.days} /></Suspense> : <div className="py-10 text-center text-xs text-dim">加载中…</div>}
          <div className="role-meta mt-1 flex gap-4">
            <span>━ 成本（左轴）</span>
            <span className="text-ok">━ 完成</span>
            <span className="text-warn">┄ 新增</span>
          </div>
        </Card>
        <Card title="活动流" right={<span className="role-meta">agent 施工现场</span>}>
          <ActivityStream events={events} height={430} />
        </Card>
      </div>

      <ConfirmDialog
        open={confirmYolo}
        title="切换到 YOLO 档位？"
        danger
        confirmText="我确认，切到 YOLO"
        onClose={() => setConfirmYolo(false)}
        onConfirm={async () => {
          await api.post("/api/mode", { mode: "yolo" });
          setConfirmYolo(false);
          push({ kind: "err", title: "已切到 YOLO", body: "PR 创建后将自动合并并关闭 issue（不可逆）" });
        }}
      >
        此档位会 <b>自动合并 PR 并删除分支</b>，且直接关闭对应 issue——
        仅建议在你完全信任当前提示词与流程时使用。
      </ConfirmDialog>
    </div>
  );
}
