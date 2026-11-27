import { useEffect, useMemo, useState } from "react";
import { RefreshCcw, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import type { SessionRow } from "../lib/types";
import { copyText, fmtAgoTs, repoSlug } from "../lib/utils";
import { Badge, Button, Card, ConfirmDialog, Empty, Select, STAGE_LABEL } from "../components/ui";
import { PageHeader } from "../components/AppShell";
import { useToast } from "../components/toast";
import { navigate } from "../lib/router";

export function SessionsPage() {
  const { push } = useToast();
  const [rows, setRows] = useState<SessionRow[]>([]);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [repoF, setRepoF] = useState("");
  const [stageF, setStageF] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const d = await api.get("/api/sessions");
      setRows(d.sessions || []);
    } catch {
      /* noop */
    }
  }
  useEffect(() => {
    load();
    const id = window.setInterval(load, 10000);
    return () => window.clearInterval(id);
  }, []);

  const repos = useMemo(() => [...new Set(rows.map((r) => r.repo).filter(Boolean))] as string[], [rows]);
  const stages = useMemo(() => [...new Set(rows.map((r) => r.stage).filter(Boolean))] as string[], [rows]);
  const shown = useMemo(
    () =>
      rows.filter(
        (r) => (!repoF || r.repo === repoF) && (!stageF || r.stage === stageF)
      ),
    [rows, repoF, stageF]
  );

  function toggle(id: string) {
    setSel((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  async function del() {
    setBusy(true);
    try {
      const ids = [...sel];
      const r = await api.post("/api/sessions/delete", { session_ids: ids });
      push({
        kind: r.ok ? "ok" : "err",
        title: r.ok ? `已删除 ${ids.length} 个 session` : "删除失败",
        body: r.ok ? `opencode2 数据清理 ${r.deleted.filter((x: any) => x.oc2_deleted).length}/${ids.length}` : r.error || "",
      });
      setSel(new Set());
      setConfirm(false);
      load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <PageHeader
        title="Sessions"
        subtitle={`${rows.length} 个 agent session · 批量删除会同时清理 opencode2 侧数据`}
        actions={
          <>
            <Select className="text-xs" value={repoF} onChange={(e) => setRepoF(e.target.value)}>
              <option value="">全部项目</option>
              {repos.map((r) => <option key={r} value={r}>{r}</option>)}
            </Select>
            <Select className="text-xs" value={stageF} onChange={(e) => setStageF(e.target.value)}>
              <option value="">全部阶段</option>
              {stages.map((s) => <option key={s} value={s}>{s}</option>)}
            </Select>
            <Button variant="ghost" size="sm" onClick={load}>
              <RefreshCcw size={12} /> 刷新
            </Button>
            <Button
              variant="danger"
              size="sm"
              disabled={!sel.size}
              onClick={() => setConfirm(true)}
            >
              <Trash2 size={12} /> 删除所选（{sel.size}）
            </Button>
          </>
        }
      />

      <Card pad={false}>
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr className="border-b border-line bg-bg2 text-left text-dim">
              <th className="w-8 px-3 py-2">
                <input
                  type="checkbox"
                  checked={!!shown.length && shown.every((r) => sel.has(r.session_id))}
                  onChange={(e) => {
                    const n = new Set(sel);
                    shown.forEach((r) => (e.target.checked ? n.add(r.session_id) : n.delete(r.session_id)));
                    setSel(n);
                  }}
                />
              </th>
              <th className="px-3 py-2 font-normal">项目</th>
              <th className="px-3 py-2 font-normal">Issue</th>
              <th className="px-3 py-2 font-normal">环节</th>
              <th className="px-3 py-2 font-normal">Session ID</th>
              <th className="px-3 py-2 font-normal">创建</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.session_id} className="border-b border-line hover:bg-accsoft/40">
                <td className="px-3 py-2">
                  <input
                    type="checkbox"
                    checked={sel.has(r.session_id)}
                    onChange={() => toggle(r.session_id)}
                  />
                </td>
                <td className="px-3 py-2 text-dim">{r.repo || "—"}</td>
                <td className="px-3 py-2">
                  {r.repo && r.issue_number ? (
                    <button
                      className="cursor-pointer text-acc hover:underline"
                      onClick={() => navigate(`#/i/${repoSlug(r.repo!)}/${r.issue_number}`)}
                    >
                      #{r.issue_number}
                    </button>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="px-3 py-2">
                  <Badge tone="dim">{STAGE_LABEL[r.stage || ""] || r.stage || r.key || "—"}</Badge>
                </td>
                <td className="px-3 py-2">
                  <span className="role-micro break-all">{r.session_id}</span>
                  <button
                    className="ml-2 cursor-pointer text-dim hover:text-fg"
                    onClick={() => copyText(r.session_id)}
                    title="复制"
                  >
                    ⧉
                  </button>
                </td>
                <td className="px-3 py-2 text-dim">{fmtAgoTs(r.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!shown.length && <Empty>{rows.length ? "无匹配 session" : "暂无 session 记录（issue 处理后自动入库）"}</Empty>}
      </Card>

      <ConfirmDialog
        open={confirm}
        danger
        title={`删除 ${sel.size} 个 session？`}
        confirmText="删除"
        busy={busy}
        onClose={() => setConfirm(false)}
        onConfirm={del}
      >
        将从数据库移除记录，并调用 <code>opencode2 api delete</code> 清理对应 session 数据。
        已完成的 issue 不受影响（session 仅用于追溯）。
      </ConfirmDialog>
    </div>
  );
}
