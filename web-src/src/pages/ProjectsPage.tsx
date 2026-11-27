import { useEffect, useState } from "react";
import { FolderPlus, Plus } from "lucide-react";
import { api } from "../lib/api";
import type { Project } from "../lib/types";
import { fmtAgoTs, repoSlug } from "../lib/utils";
import { navigate } from "../lib/router";
import { Badge, Button, Card, Input, STAGE_LABEL, Textarea } from "../components/ui";
import { PageHeader } from "../components/AppShell";
import { useToast } from "../components/toast";

export function ProjectsPage() {
  const { push } = useToast();
  const [projects, setProjects] = useState<Project[]>([]);
  const [repo, setRepo] = useState("");
  const [intro, setIntro] = useState("");
  const [buildCmd, setBuildCmd] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    const d = await api.get("/api/projects");
    setProjects(d.projects || []);
  }
  useEffect(() => {
    load();
  }, []);

  async function add() {
    if (!repo.trim() || !repo.includes("/")) {
      push({ kind: "err", title: "repo 需为 owner/name 格式" });
      return;
    }
    setBusy(true);
    try {
      const r = await api.post("/api/projects/add", {
        repo: repo.trim(),
        intro: intro.trim(),
        build_cmd: buildCmd.trim()
          ? buildCmd.split("\n").map((x) => x.trim()).filter(Boolean)
          : undefined,
      });
      if (r.ok) {
        push({ kind: "ok", title: "项目已接入", body: r.repo });
        setRepo(""); setIntro(""); setBuildCmd("");
        load();
      } else {
        push({ kind: "err", title: "接入失败", body: r.error || "" });
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <PageHeader
        title="项目"
        subtitle={`${projects.length} 个被监控仓库 · 每个项目独立档位/构建/发布配置`}
      />

      <Card title="接入新项目" className="mb-4" pad>
        <div className="grid gap-3 md:grid-cols-2">
          <div className="space-y-2">
            <Input placeholder="owner/name（如 example-org/sample-project）" value={repo} onChange={(e) => setRepo(e.target.value)} />
            <Input placeholder="一句话项目简介（可选，供问题审核参考）" value={intro} onChange={(e) => setIntro(e.target.value)} />
            <Textarea
              className="h-[88px]"
              placeholder={"构建/门禁命令（每行一段，可选）\n如：./gradlew assembleDebug --console=plain"}
              value={buildCmd}
              onChange={(e) => setBuildCmd(e.target.value)}
            />
          </div>
          <div className="text-xs text-dim">
            <div className="mb-2 flex items-center gap-2 text-fg">
              <FolderPlus size={14} className="text-acc" /> 接入说明
            </div>
            <ul className="space-y-1.5">
              <li>· 仓库需可被 gh 访问（私有仓库用 gh auth / GH_TOKEN）</li>
              <li>· 构建命令用于「门禁」环节（在沙箱内执行）；留空默认 <code>true</code>（不构建）</li>
              <li>· 接入后建议到项目详情运行「初始化 agent」生成 AGENTS.md + 发布 workflow</li>
              <li>· 拉取修复模式的仓库地址默认 <code>https://github.com/&lt;owner/name&gt;.git</code></li>
            </ul>
            <Button className="mt-3" size="sm" onClick={add} disabled={busy}>
              <Plus size={12} /> 接入
            </Button>
          </div>
        </div>
      </Card>

      <div className="grid gap-3 lg:grid-cols-2">
        {projects.map((p) => {
          const issues = p.issues || [];
          const open = issues.filter((i) => !["done", "rejected"].includes(i.stage)).length;
          const last = issues[0];
          return (
            <Card key={p.repo} pad>
              <div className="flex items-center gap-2">
                <span className="font-medium">{p.repo}</span>
                <Badge tone={p.mode === "default" ? "dim" : p.mode === "paused" ? "dim" : "acc"}>
                  {p.mode === "default" ? "跟随全局" : p.mode}
                </Badge>
                <span className="ml-auto flex gap-1.5">
                  <Button size="xs" variant="ghost" onClick={() => navigate(`#/p/${repoSlug(p.repo)}`)}>设置</Button>
                  <Button size="xs" variant="ghost" onClick={() => navigate("#/release")}>发布</Button>
                </span>
              </div>
              <div className="mt-1.5 text-xs text-dim">
                {p.intro || "（无简介）"}
              </div>
              <div className="role-meta mt-1.5">
                进行 {open} · 全部 {issues.length}
                {last && <> · 最近 #{last.number} {last.title}（{STAGE_LABEL[last.stage] || last.stage}）· {fmtAgoTs(last.updated_at)}</>}
              </div>
              <div className="role-meta mt-2">
                构建命令：{(p.build_cmd || []).join(" ") || "true（不构建）"}
              </div>
            </Card>
          );
        })}
        {!projects.length && (
          <Card>
            <div className="py-6 text-center text-sm text-dim">暂无项目 —— 用上方表单接入第一个仓库</div>
          </Card>
        )}
      </div>
    </div>
  );
}
