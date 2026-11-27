import { useEffect, useState } from "react";
import { RotateCcw, Save } from "lucide-react";
import { api } from "../lib/api";
import { cn } from "../lib/utils";
import { Button, Card, ConfirmDialog, Empty, Select, Textarea } from "../components/ui";
import { PageHeader } from "../components/AppShell";
import { useToast } from "../components/toast";

const L1 = [
  ["audit", "问题审核", "直连"],
  ["fix_simple", "修复", "oc2"],
  ["review_simple", "审核代码", "oc2"],
  ["reply", "回复", "直连"],
];
const L2 = [
  ["audit", "问题审核", "直连"],
  ["triage", "分诊", "oc2"],
  ["fix", "修复", "oc2"],
  ["review", "三路审查", "oc2"],
  ["final", "终审", "oc2"],
  ["reply", "回复", "直连"],
];
const V3 = [
  ["merge_fix", "merge 修复", "oc2"],
  ["release_run", "发布执行", "oc2"],
  ["release_fix", "发布修错", "oc2"],
  ["init_project", "初始化", "oc2"],
];
const NODE_FILES: Record<string, string[]> = {
  audit: ["audit.system.md", "audit.user.md"],
  fix_simple: ["fix_simple.system.md", "fix_simple.user.md"],
  review_simple: ["review_simple.system.md", "review_simple.user.md"],
  reply: ["reply.system.md", "reply.user.md"],
  triage: ["triage.system.md", "triage.user.md"],
  fix: ["fix.system.md", "fix.user.md"],
  review: [
    "review_bug.system.md", "review_bug.user.md",
    "review_minimal.system.md", "review_minimal.user.md",
    "review_style.system.md", "review_style.user.md",
  ],
  final: ["final.system.md", "final.user.md"],
  merge_fix: ["merge_fix.system.md", "merge_fix.user.md"],
  release_run: ["release_run.system.md", "release_run.user.md"],
  release_fix: ["release_fix.system.md", "release_fix.user.md"],
  init_project: ["init_project.system.md", "init_project.user.md"],
  shared: ["_shared.json"],
};

export function PromptsPage() {
  const { push } = useToast();
  const [files, setFiles] = useState<Record<string, string>>({});
  const [customized, setCustomized] = useState<Set<string>>(new Set());
  const [version, setVersion] = useState(0);
  const [node, setNode] = useState("");
  const [file, setFile] = useState("");
  const [content, setContent] = useState("");
  const [dirty, setDirty] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [busy, setBusy] = useState(false);

  async function load(keep?: { node: string; file: string }) {
    const d = await api.get("/api/prompts");
    setFiles(d.files || {});
    setCustomized(new Set(d.customized || []));
    setVersion(d.version);
    if (keep?.node) {
      selectNode(keep.node, d.files, keep.file);
    }
  }
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function selectNode(key: string, fs?: Record<string, string>, keepFile?: string) {
    const src = fs || files;
    setNode(key);
    const list = NODE_FILES[key] || [];
    const want = keepFile && list.includes(keepFile) ? keepFile : list[0] || "";
    setFile(want);
    setContent(src[want] || "");
    setDirty(false);
  }

  async function save() {
    if (!file) return;
    const r = await api.post("/api/prompts", { name: file, content });
    if (r.ok) {
      push({ kind: "ok", title: `已保存 ${file}`, body: `提示词版本 v${r.version} · 热加载生效` });
      setDirty(false);
      load({ node, file });
    } else {
      push({ kind: "err", title: "保存失败", body: r.error || "" });
    }
  }

  const withArrow = (nodes: (string[])[]) =>
    nodes.map(([k, l, e], i) => (
      <span key={k} className="flex items-center gap-1.5">
        <button
          onClick={() => selectNode(k)}
          className={cn(
            "flex cursor-pointer flex-col items-center border px-3 py-1.5 text-xs transition-colors",
            node === k ? "border-acc bg-accsoft shadow-[var(--shadow)]" : "border-line2 bg-bg2 shadow-[var(--inset)] hover:border-acc"
          )}
        >
          <span>
            {l}
            {customized.has((NODE_FILES[k] || [])[0]) && <span className="ml-1 text-warn">●</span>}
          </span>
          <span className="role-meta">{e}</span>
        </button>
        {i < nodes.length - 1 && <span className="text-dim">→</span>}
      </span>
    ));

  return (
    <div>
      <PageHeader
        title="提示词"
        subtitle={`v${version} · 点击节点编辑 · 保存即热加载（进行中的 issue 不受影响）`}
        actions={
          <Button variant="ghost" size="sm" onClick={() => setConfirmReset(true)}>
            <RotateCcw size={12} /> 恢复出厂
          </Button>
        }
      />

      <Card className="mb-4" pad>
        <div className="mb-2 flex flex-wrap items-center gap-1.5">
          <span className="role-meta w-14">简单线</span>
          {withArrow(L1)}
        </div>
        <div className="mb-2 flex flex-wrap items-center gap-1.5">
          <span className="role-meta w-14">复杂线</span>
          {withArrow(L2)}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="role-meta w-14">v3 环节</span>
          {withArrow(V3)}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <span className="role-meta w-14">共享</span>
          <button
            onClick={() => selectNode("shared")}
            className={cn(
              "cursor-pointer border px-3 py-1.5 text-xs transition-colors",
              node === "shared" ? "border-acc bg-accsoft shadow-[var(--shadow)]" : "border-line2 bg-bg2 shadow-[var(--inset)] hover:border-acc"
            )}
          >
            ⚙ 共享组件（披露头 / 铁律模板）{customized.has("_shared.json") && <span className="ml-1 text-warn">●</span>}
          </button>
        </div>
      </Card>

      <Card pad>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium">{node ? NODE_FILES[node] ? `${node} 环节` : "共享组件" : "← 请点击上方节点"}</span>
          {node && (
            <Select
              className="text-xs"
              value={file}
              onChange={(e) => {
                setFile(e.target.value);
                setContent(files[e.target.value] || "");
                setDirty(false);
              }}
            >
              {(NODE_FILES[node] || []).map((f) => (
                <option key={f} value={f}>{f}</option>
              ))}
            </Select>
          )}
          <span className="ml-auto text-xs text-dim">
            {content ? `${content.length} 字符` : ""}
            {dirty && <span className="ml-2 text-warn">● 未保存</span>}
          </span>
        </div>
        {node ? (
          <>
            <Textarea
              className="h-[380px] w-full"
              value={content}
              spellCheck={false}
              onChange={(e) => {
                setContent(e.target.value);
                setDirty(true);
              }}
            />
            <div className="mt-3 flex items-center gap-2">
              <Button size="sm" onClick={save} disabled={!dirty}>
                <Save size={12} /> 保存（热加载）
              </Button>
              <span className="text-xs text-dim">保存后新任务立即使用新版本；正在进行的不受影响</span>
            </div>
          </>
        ) : (
          <Empty>点击上方流程节点开始编辑</Empty>
        )}
      </Card>

      <ConfirmDialog
        open={confirmReset}
        danger
        title="恢复出厂提示词？"
        confirmText="恢复"
        busy={busy}
        onClose={() => setConfirmReset(false)}
        onConfirm={async () => {
          setBusy(true);
          try {
            await api.post("/api/prompts/reset", {});
            push({ kind: "ok", title: "已恢复出厂提示词" });
            setConfirmReset(false);
            setNode("");
            load();
          } finally {
            setBusy(false);
          }
        }}
      >
        所有自定义提示词将被默认版本覆盖（含共享组件）。此操作不可撤销。
      </ConfirmDialog>
    </div>
  );
}
