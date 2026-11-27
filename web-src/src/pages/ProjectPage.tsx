import { useEffect, useState } from "react";
import { ArrowLeft, Rocket, Save, Wand2 } from "lucide-react";
import { api } from "../lib/api";
import type { Project } from "../lib/types";
import { fmtAgoTs, slugToRepo } from "../lib/utils";
import { navigate } from "../lib/router";
import { Badge, Button, Card, ConfirmDialog, Empty, Input, Select, StageBadge, Textarea } from "../components/ui";
import { PageHeader } from "../components/AppShell";
import { useToast } from "../components/toast";

const MODES = ["default", "dry", "semi", "full", "yolo", "paused"];
const LANDINGS = [
  ["pr", "走 PR（推荐，等你审查合并）"],
  ["main", "直接推送 main（快，无审查）"],
  ["pr_auto", "PR + 自动合并（全自动）"],
];

export function ProjectPage({ slug }: { slug: string }) {
  const repo = slugToRepo(slug);
  const { push } = useToast();
  const [p, setP] = useState<Project | null>(null);
  const [intro, setIntro] = useState("");
  const [mode, setMode] = useState("default");
  const [agentLimit, setAgentLimit] = useState("0");
  const [mainBranch, setMainBranch] = useState("main");
  const [buildCmd, setBuildCmd] = useState("");
  const [buildEnv, setBuildEnv] = useState("");
  const [landing, setLanding] = useState("pr");
  const [confirmInit, setConfirmInit] = useState(false);
  const [initStatus, setInitStatus] = useState<any>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    const d = await api.get("/api/projects");
    const found = (d.projects || []).find((x: Project) => x.repo === repo);
    setP(found || null);
    if (found) {
      setIntro(found.intro || "");
      setMode(found.mode || "default");
      setAgentLimit(String(found.agent_limit || 0));
      setMainBranch(found.main_branch || "main");
      setBuildCmd((found.build_cmd || []).join("\n"));
      setBuildEnv(JSON.stringify(found.build_env || {}, null, 2));
    }
    try {
      const st = await api.get(`/api/init-status?repo=${encodeURIComponent(repo)}`);
      setInitStatus(st);
    } catch {
      /* noop */
    }
  }
  useEffect(() => {
    load();
    const id = window.setInterval(load, 8000);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo]);

  async function save() {
    let envObj: any = {};
    try {
      envObj = buildEnv.trim() ? JSON.parse(buildEnv) : {};
    } catch {
      push({ kind: "err", title: "build_env 不是合法 JSON" });
      return;
    }
    const r = await api.post("/api/project/update", {
      repo,
      intro,
      mode,
      agent_limit: parseInt(agentLimit || "0"),
      main_branch: mainBranch,
      build_cmd: buildCmd.split("\n").map((x) => x.trim()).filter(Boolean),
      build_env: envObj,
    });
    if (r.ok) push({ kind: "ok", title: "项目配置已保存" });
    else push({ kind: "err", title: "保存失败", body: r.error || "" });
  }

  async function runInit() {
    setBusy(true);
    try {
      const r = await api.post("/api/init-project", { repo, landing });
      if (r.ok) {
        push({ kind: "info", title: "初始化 agent 已启动", body: "生成 AGENTS.md + 发布方案（约 3-10 分钟）" });
        setConfirmInit(false);
        load();
      } else {
        push({ kind: "err", title: "启动失败", body: r.error || "" });
      }
    } finally {
      setBusy(false);
    }
  }

  if (!p) {
    return (
      <div>
        <PageHeader title="项目设置" />
        <Card><Empty>项目不存在或加载中…</Empty></Card>
      </div>
    );
  }

  const issues = p.issues || [];
  const initRes = initStatus?.result;

  return (
    <div>
      <PageHeader
        title={
          <span className="flex items-center gap-2">
            <button className="cursor-pointer text-dim hover:text-fg" onClick={() => navigate("#/projects")}>
              <ArrowLeft size={16} />
            </button>
            {p.repo}
          </span>
        }
        subtitle={`${issues.length} 个 issue · 最近活动 ${fmtAgoTs(issues[0]?.updated_at)}`}
        actions={
          <>
            <Button size="sm" variant="ghost" onClick={() => navigate("#/issues")}>看板</Button>
            <Button size="sm" variant="ghost" onClick={() => navigate("#/release")}>
              <Rocket size={12} /> 发布
            </Button>
          </>
        }
      />

      <div className="grid gap-3 lg:grid-cols-2">
        <Card title="基本配置" pad>
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <span className="role-meta w-24">项目档位</span>
              <Select className="text-xs" value={mode} onChange={(e) => setMode(e.target.value)}>
                {MODES.map((m) => (
                  <option key={m} value={m}>{m === "default" ? "跟随全局" : m}</option>
                ))}
              </Select>
            </div>
            <div className="flex items-center gap-2">
              <span className="role-meta w-24">并发上限</span>
              <Input className="w-[80px]" type="number" min={0} max={99} value={agentLimit} onChange={(e) => setAgentLimit(e.target.value)} />
              <span className="role-meta">0 = 跟随全局</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="role-meta w-24">主分支</span>
              <Input className="w-[140px]" value={mainBranch} onChange={(e) => setMainBranch(e.target.value)} />
            </div>
            <div className="flex items-start gap-2">
              <span className="role-meta w-24 pt-1">项目简介</span>
              <Input className="flex-1" value={intro} onChange={(e) => setIntro(e.target.value)} placeholder="供问题审核参考" />
            </div>
            <div className="flex items-start gap-2">
              <span className="role-meta w-24 pt-1">构建命令</span>
              <Textarea className="h-[80px] flex-1" value={buildCmd} onChange={(e) => setBuildCmd(e.target.value)} placeholder={"每行一段\n./gradlew assembleDebug --console=plain"} />
            </div>
            <div className="flex items-start gap-2">
              <span className="role-meta w-24 pt-1">构建环境</span>
              <Textarea className="h-[80px] flex-1" value={buildEnv} onChange={(e) => setBuildEnv(e.target.value)} placeholder='{"ANDROID_HOME": "..."}' />
            </div>
            <Button size="sm" onClick={save}>
              <Save size={12} /> 保存
            </Button>
          </div>
        </Card>

        <Card title="初始化 agent（AGENTS.md + 发布方案）" pad>
          <div className="space-y-3">
            <div className="role-meta">
              通读仓库，生成符合实际情况的 AGENTS.md 与 release workflow（若 workflow 不合适则生成 Release.md）。
            </div>
            <div className="flex items-center gap-2">
              <span className="role-meta w-24">产物落地</span>
              <Select className="flex-1 text-xs" value={landing} onChange={(e) => setLanding(e.target.value)}>
                {LANDINGS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </Select>
            </div>
            <div className="flex items-center gap-2">
              <Button size="sm" variant={landing === "pr_auto" ? "danger" : "primary"} onClick={() => setConfirmInit(true)} disabled={initStatus?.status === "running"}>
                <Wand2 size={12} /> {initStatus?.status === "running" ? "初始化进行中…" : "运行初始化 agent"}
              </Button>
              {initStatus?.status && <Badge tone={initStatus.status === "done" ? "ok" : initStatus.status === "error" ? "err" : "warn"}>{initStatus.status}</Badge>}
            </div>
            {initRes && (
              <div className="panel2 p-3 text-xs">
                <div className={initRes.ok ? "text-ok" : "text-err"}>
                  {initRes.ok ? "✅ 初始化完成" : `⚠ ${initRes.error}`}
                </div>
                {(initRes.steps || []).map((s: string, i: number) => (
                  <div key={i} className="mt-1 text-dim">· {s}</div>
                ))}
                {initRes.pr_url && (
                  <a className="mt-1 inline-block" href={initRes.pr_url} target="_blank" rel="noreferrer">
                    {initRes.pr_url}
                  </a>
                )}
              </div>
            )}
            <div className="role-meta">
              每次运行都会新建分支/PR（不覆盖已有文件）；重复运行前请先处理上一个 PR。
            </div>
          </div>
        </Card>
      </div>

      <Card title={`最近 issues（${issues.length}）`} className="mt-3" pad>
        {issues.slice(0, 8).map((i) => (
          <div key={i.number} className="flex items-center gap-2 border-b border-line py-1.5 text-xs last:border-b-0">
            <span className="role-micro">#{i.number}</span>
            <StageBadge stage={i.stage} />
            <button
              className="min-w-0 flex-1 cursor-pointer truncate text-left hover:underline"
              onClick={() => navigate(`#/i/${slug}/${i.number}`)}
            >
              {i.title}
            </button>
            <span className="text-dim">{fmtAgoTs(i.updated_at)}</span>
          </div>
        ))}
        {!issues.length && <Empty>暂无 issue</Empty>}
      </Card>

      <ConfirmDialog
        open={confirmInit}
        danger={landing === "pr_auto"}
        title={`运行初始化 agent？（${landing === "main" ? "直推 main" : landing === "pr_auto" ? "PR+自动合并" : "走 PR"}）`}
        confirmText="开始"
        busy={busy}
        onClose={() => setConfirmInit(false)}
        onConfirm={runInit}
      >
        将 fresh clone {repo} → agent 生成文件 → commit 并{landing === "main" ? "直接推送 main" : "创建 PR"}。
        {landing === "pr_auto" && " PR 会被自动合并 —— 请确认文件内容质量可控。"}
      </ConfirmDialog>
    </div>
  );
}
