import { useEffect, useMemo, useState } from "react";
import { RefreshCcw, Search } from "lucide-react";
import { api } from "../lib/api";
import type { Issue, Project } from "../lib/types";
import { fmtAgoTs, fmtMoney, fmtTokens, repoSlug, selRepo, setSelRepo } from "../lib/utils";
import { navigate } from "../lib/router";
import { Badge, Button, Card, Chip, ConfirmDialog, Empty, Input, Select, StageBadge, STAGE_LABEL } from "../components/ui";
import { PageHeader } from "../components/AppShell";
import { useToast } from "../components/toast";

const STAGES = [
  "pending", "audit", "triage", "fix", "gate", "review", "final", "reply",
  "execute", "done", "rejected", "needs_human",
];


export function IssuesPage() {
  const { push } = useToast();
  const [projects, setProjects] = useState<Project[]>([]);
  const [repo, setRepo] = useState(selRepo());
  const [issues, setIssues] = useState<Issue[]>([]);
  const [stage, setStage] = useState("");
  const [q, setQ] = useState("");
  const [rerunTarget, setRerunTarget] = useState<Issue | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    async function tick() {
      try {
        const pd = await api.get("/api/projects");
        if (!alive) return;
        const ps: Project[] = pd.projects || [];
        setProjects(ps);
        let r = repo;
        if (!r && ps.length) {
          r = ps[0].repo;
          setRepo(r);
          setSelRepo(r);
        }
        if (r) {
          const d = await api.get(`/api/issues?repo=${encodeURIComponent(r)}`);
          if (alive) setIssues(d.issues || []);
        }
      } catch {
        /* noop */
      }
    }
    tick();
    const id = window.setInterval(tick, 5000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [repo]);

  const shown = useMemo(() => {
    let xs = issues;
    if (stage) xs = xs.filter((i) => i.stage === stage);
    if (q.trim()) {
      const k = q.trim().toLowerCase();
      xs = xs.filter(
        (i) => String(i.number).includes(k) || (i.title || "").toLowerCase().includes(k)
      );
    }
    return xs;
  }, [issues, stage, q]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const i of issues) c[i.stage] = (c[i.stage] || 0) + 1;
    return c;
  }, [issues]);

  return (
    <div>
      <PageHeader
        title="Issues"
        subtitle={repo ? `${repo} · ${issues.length} 个 issue` : "选择项目"}
        actions={
          <>
            <Select value={repo} onChange={(e) => { setRepo(e.target.value); setSelRepo(e.target.value); }}>
              {projects.map((p) => (
                <option key={p.repo} value={p.repo}>{p.repo}</option>
              ))}
            </Select>
            <Button variant="outline" size="sm" onClick={() => api.post("/api/poll", {}).then((r) => r.ok ? push({ kind: "info", title: "已触发轮询" }) : push({ kind: "err", title: "轮询未触发", body: "上一轮轮询仍在进行中，请稍候" }))}>
              <RefreshCcw size={12} /> 轮询
            </Button>
          </>
        }
      />

      <Card className="mb-4" pad>
        <div className="flex flex-wrap items-center gap-2">
          <Chip on={stage === ""} onClick={() => setStage("")}>
            全部 {issues.length}
          </Chip>
          {STAGES.filter((s) => counts[s]).map((s) => (
            <Chip key={s} on={stage === s} onClick={() => setStage(stage === s ? "" : s)}>
              {STAGE_LABEL[s] || s} {counts[s]}
            </Chip>
          ))}
          <span className="relative ml-auto">
            <Search size={12} className="absolute top-1/2 left-2 -translate-y-1/2 text-dim" />
            <Input
              className="w-[190px] pl-6"
              placeholder="搜索编号 / 标题"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </span>
        </div>
      </Card>

      <Card pad={false}>
        {shown.map((i) => (
          <div key={i.number} className="border-b border-line px-4 py-3 transition-colors last:border-b-0 hover:bg-accsoft/40">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs font-semibold tabular-nums">#{i.number}</span>
              <StageBadge stage={i.stage} />
              {i.complexity === "simple" && <Badge tone="dim">简单</Badge>}
              {i.complexity === "complex" && <Badge tone="dim">复杂</Badge>}
              {i.merged && <Badge tone="ok">已合并</Badge>}
              {i.publish_status === "running" && <Badge tone="warn">补发中…</Badge>}
              {i.merge_fix_status === "running" && <Badge tone="warn">merge修复中…</Badge>}
              {i.paused_from && <Badge tone="dim">暂停于{STAGE_LABEL[i.paused_from] || i.paused_from}</Badge>}
              {i.mode && i.mode !== "default" && <Badge tone="acc">{i.mode}</Badge>}
              <button
                className="min-w-0 flex-1 cursor-pointer truncate text-left hover:underline"
                onClick={() => navigate(`#/i/${repoSlug(repo)}/${i.number}`)}
                title={i.title}
              >
                {i.title || "(无标题)"}
              </button>
              <Button size="xs" variant="subtle" onClick={() => setRerunTarget(i)}>重跑</Button>
              <Button size="xs" variant="ghost" onClick={() => navigate(`#/i/${repoSlug(repo)}/${i.number}`)}>详情</Button>
            </div>
            {i.error && <div className="mt-1 text-xs text-err">⚠ {i.error}</div>}
            <div className="role-micro mt-1">
              成本 {fmtMoney(i.cost)} · tokens {fmtTokens(i.tokens)} · 更新 {fmtAgoTs(i.updated_at)}
              {i.pr_url && (
                <>
                  {" · "}
                  <a href={i.pr_url} target="_blank" rel="noreferrer">{i.pr_url}</a>
                </>
              )}
            </div>
          </div>
        ))}
        {!shown.length && <Empty>{issues.length ? "无匹配 issue" : "该项目暂无 issue 记录"}</Empty>}
      </Card>

      <ConfirmDialog
        open={!!rerunTarget}
        title={`重跑 #${rerunTarget?.number}？`}
        confirmText="重跑"
        busy={busy}
        onClose={() => setRerunTarget(null)}
        onConfirm={async () => {
          if (!rerunTarget) return;
          setBusy(true);
          try {
            const r = await api.post("/api/rerun", { issue: rerunTarget.number, repo });
            if (!r.ok)
              push({ kind: "err", title: "重跑失败", body: r.error || "" });
            else if (r.triggered === false)
              push({ kind: "err", title: `#${rerunTarget.number} 正在运行中`, body: "已重置为待处理，可在当前运行结束后再次重跑" });
            else
              push({ kind: "info", title: `#${rerunTarget.number} 已重新排队`, body: "重试预算已重置，将从头开始处理" });
            setRerunTarget(null);
          } finally {
            setBusy(false);
          }
        }}
      >
        将重置重试计数并<b>从头执行</b>全流程（问题审核 → 分诊 → 修复 → …）。
        当前 stage：{rerunTarget?.stage}
      </ConfirmDialog>
    </div>
  );
}
