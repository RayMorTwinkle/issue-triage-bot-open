import { useEffect, useRef, useState } from "react";
import { ArrowLeft, Download, GitMerge, Play, Send, Upload } from "lucide-react";
import { api, getToken } from "../lib/api";
import { fmtAgoTs, fmtMoney, fmtTokens, copyText, slugToRepo } from "../lib/utils";
import {
  Badge, Button, Card, Chip, ConfirmDialog, Dialog, Empty, Select, STAGE_LABEL,
} from "../components/ui";
import { PageHeader } from "../components/AppShell";
import { FlowTimeline, ProgressHint } from "../components/FlowTimeline";
import { useToast } from "../components/toast";
import { navigate } from "../lib/router";

import { ReviewDrawer } from "../../replay/Workbench";

const MODES = ["default", "dry", "semi", "full", "yolo", "paused"];

export function IssueDetail({ slug, n }: { slug: string; n: string }) {
  const repo = slugToRepo(slug);
  const num = parseInt(n || "0");
  const { push } = useToast();
  const [d, setD] = useState<any>(null);
  const [drawer, setDrawer] = useState(false);
  const [snapshot, setSnapshot] = useState({ diff: "", reviews: {}, truncated: false });
  const [preview, setPreview] = useState<any>(null);
  const [doRerun, setDoRerun] = useState(false);
  const [doMerge, setDoMerge] = useState(false);
  const [doPublish, setDoPublish] = useState(false);
  const [busy, setBusy] = useState(false);
  const prevPub = useRef<string | null>(null);
  const prevMerge = useRef<string | null>(null);

  useEffect(() => {
    let alive = true;
    async function tick() {
      try {
        const r = await api.get(`/api/issue/${num}?repo=${encodeURIComponent(repo)}`);
        if (!alive) return;
        setD(r);
        const e = r.entry || {};
        // 补发/merge 完成通知
        if (prevPub.current === "running" && e.publish_status && e.publish_status !== "running") {
          const ok = e.publish_status === "done";
          push({
            kind: ok ? "ok" : "err",
            title: ok ? `#${num} 补发${ok ? "成功" : "失败"}` : `#${num} 补发失败`,
            body: e.publish_result?.error || (e.publish_result?.steps || []).slice(-1)[0] || "",
          });
        }
        prevPub.current = e.publish_status || null;
        if (prevMerge.current === "running" && e.merge_fix_status && e.merge_fix_status !== "running") {
          const ok = e.merge_fix_status === "done";
          push({
            kind: ok ? "ok" : "err",
            title: ok ? `#${num} 合并修复成功` : `#${num} 合并修复失败`,
            body: e.merge_fix_result?.error || "",
          });
        }
        prevMerge.current = e.merge_fix_status || null;
      } catch {
        /* noop */
      }
    }
    tick();
    const id = window.setInterval(tick, 4000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [num, repo]);

  const e = d?.entry || {};
  const p = d?.parsed || {};
  const running = ["pending", "audit", "triage", "fix", "gate", "review", "final", "reply", "execute"].includes(e.stage) && e.stage !== "pending";

  async function openReview() {
    try {
      const result = await api.get(`/api/issue/${num}?repo=${encodeURIComponent(repo)}&review=1`);
      if (result.error) throw new Error(result.error);
      setSnapshot(result);
      setDrawer(true);
    } catch (err) {
      push({ kind: "err", title: "读取补丁失败", body: String(err) });
    }
  }

  async function openPreview() {
    try {
      const pv = await api.get(`/api/publish/preview/${num}?repo=${encodeURIComponent(repo)}`);
      setPreview(pv);
    } catch (err: any) {
      push({ kind: "err", title: "补发预览失败", body: String(err) });
    }
  }

  return (
    <div>
      <PageHeader
        title={
          <span className="flex items-center gap-2">
            <button className="cursor-pointer text-dim hover:text-fg" onClick={() => navigate("#/issues")}>
              <ArrowLeft size={16} />
            </button>
            #{num} {e.title || "…"}
          </span>
        }
        subtitle={`${repo} · ${e.stage || ""}${e.complexity ? ` · ${e.complexity}` : ""}`}
        actions={
          <>
            <Button size="sm" variant="outline" onClick={openReview}>审查意见与 Diff</Button>
            <Select
              className="!py-1 text-xs"
              value={e.mode || "default"}
              onChange={(ev) => api.post("/api/issue-mode", { issue: num, mode: ev.target.value, repo })}
            >
              {MODES.map((m) => (
                <option key={m} value={m}>{m === "default" ? "继承项目档位" : m}</option>
              ))}
            </Select>
            <Button variant="subtle" size="sm" onClick={() => setDoRerun(true)}>
              <Play size={12} /> 重跑
            </Button>
            {e.pr_url && !e.merged && (
              <Button variant="subtle" size="sm" onClick={() => setDoMerge(true)} disabled={e.merge_fix_status === "running"}>
                <GitMerge size={12} /> {e.merge_fix_status === "running" ? "修复中…" : "merge 修复"}
              </Button>
            )}
            <Button
              size="sm"
              variant={e.stage === "done" && !e.pr_url ? "primary" : "subtle"}
              onClick={openPreview}
              disabled={e.publish_status === "running"}
            >
              <Send size={12} /> {e.publish_status === "running" ? "补发中…" : "发布…"}
            </Button>
          </>
        }
      />

      {drawer && <ReviewDrawer number={num} title={e.title} {...snapshot} onClose={() => setDrawer(false)} />}
      <Card className="mb-3" pad>
        <FlowTimeline entry={{ ...e, history: e.history } as any} />
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-dim">
          <span>成本 {fmtMoney(e.cost)}</span>
          <span>tokens {fmtTokens(e.tokens)}</span>
          {e.branch && <span>分支 <code>{e.branch}</code></span>}
          {e.pr_url && (
            <a href={e.pr_url} target="_blank" rel="noreferrer">{e.pr_url}</a>
          )}
          {e.merged && <span className="text-ok">✅ 已合并</span>}
          {running && <span className="text-acc">● 正在处理</span>}
          <span className="ml-auto"><ProgressHint progress={d?.progress} /></span>
        </div>
        {e.error && <div className="mt-1 text-xs text-err">⚠ {e.error}</div>}
        {e.paused_from && <div className="mt-1 text-xs text-warn">⏸ 已暂停（环节边界，原环节：{STAGE_LABEL[e.paused_from] || e.paused_from}）——切回档位后自动恢复</div>}
        {e.stage === "waiting_info" && (
          <div className="mt-1 text-xs text-warn">
            ⏳ 等待作者补充信息{e.retries?.info_rounds ? `（第 ${e.retries.info_rounds} 轮追问）` : ""}
            ——作者回复后将自动重审
            {e.info_posted === false && "（DRY：追问尚未发出，切到 semi/full/yolo 后自动补发）"}
          </div>
        )}
        {e.publish_result && (
          <div className={`mt-1 text-xs ${e.publish_result.ok ? "text-ok" : "text-err"}`}>
            补发结果：{e.publish_result.ok ? "成功" : e.publish_result.error}
            {(e.publish_result.steps || []).length > 0 && (
              <span className="text-dim"> · {(e.publish_result.steps || []).join(" → ")}</span>
            )}
          </div>
        )}
        {e.merge_fix_result && (
          <div className={`mt-1 text-xs ${e.merge_fix_result.ok ? "text-ok" : "text-err"}`}>
            merge 修复：{e.merge_fix_result.ok ? "成功" : e.merge_fix_result.error}
          </div>
        )}
      </Card>

      <div className="mb-3 grid gap-3 lg:grid-cols-2">
        <Card title="① 问题审核（直连）" pad>
          {p.audit ? (
            <>
              <div className="flex items-center gap-2">
                <Badge tone={p.audit.verdict === "ok" ? "ok" : "err"}>{p.audit.verdict}</Badge>
                <Badge tone="dim">{p.audit.category}</Badge>
                {p.audit.complexity && (
                  <Badge tone="dim">{p.audit.complexity === "simple" ? "简单" : "复杂"}</Badge>
                )}
              </div>
              <div className="mt-1.5 text-sm">{p.audit.reason}</div>
              {p.audit.reject_reply && <div className="mt-1 text-xs text-dim">拒绝回复：{p.audit.reject_reply}</div>}
            </>
          ) : (
            <Empty>无数据（旧流程 issue）</Empty>
          )}
        </Card>
        <Card title="② 分诊" pad>
          {p.triage ? (
            <>
              <div className="flex items-center gap-2">
                <Badge tone={p.triage.verdict === "accept" ? "ok"
                  : p.triage.verdict === "need_info" ? "warn" : "err"}>
                  {p.triage.verdict}
                </Badge>
                <Badge tone="dim">{p.triage.issue_type}</Badge>
                <span className="text-xs text-dim">置信度 {p.triage.confidence}%</span>
              </div>
              <ul className="mt-1 list-inside list-disc text-xs text-dim">
                {(p.triage.reasons || []).map((r: string, i: number) => <li key={i}>{r}</li>)}
              </ul>
              {(p.triage.questions || []).length > 0 && (
                <div className="mt-1.5 text-xs">
                  <span className="text-warn">待补充：</span>
                  <ol className="mt-0.5 list-inside list-decimal text-dim">
                    {(p.triage.questions || []).map((q: string, i: number) => <li key={i}>{q}</li>)}
                  </ol>
                </div>
              )}
              {p.triage.minimal_plan && <div className="mt-1 text-xs text-dim">方案：{p.triage.minimal_plan}</div>}
            </>
          ) : e.complexity === "simple" ? (
            <Empty>（简单模式：跳过）</Empty>
          ) : (
            <Empty>无数据</Empty>
          )}
        </Card>
      </div>

      <Card title="③ 修复" className="mb-3" pad>
        {p.fix ? (
          <>
            <div className="flex flex-wrap gap-1.5">
              {(p.fix.changed_files || []).map((f: string) => (
                <Badge key={f} tone="dim">{f}</Badge>
              ))}
            </div>
            <div className="mt-1.5 text-sm">{p.fix.summary}</div>
            <div className="mt-1 text-xs text-dim">自检：{p.fix.self_check}</div>
          </>
        ) : (
          <Empty>无数据</Empty>
        )}
      </Card>

      <Card title="④ 审查" className="mb-3" pad>
        {p.reviews && Object.keys(p.reviews).length ? (
          <div className="grid gap-3 md:grid-cols-3">
            {Object.entries(p.reviews).map(([k, r]: [string, any]) => (
              <div key={k} className="panel2 p-3">
                <div className="mb-1 flex items-center justify-between">
                  <b className="text-xs">{k}</b>
                  <Badge tone={r.pass ? "ok" : "err"}>{r.pass ? "通过" : "未通过"}</Badge>
                </div>
                <div className="text-xs text-dim">{r.notes}</div>
                {(r.issues || []).map((x: any, i: number) => (
                  <div key={i} className="mt-1 text-xs text-err">▸ [{x.severity}] {x.desc}</div>
                ))}
              </div>
            ))}
          </div>
        ) : (
          <Empty>无数据</Empty>
        )}
      </Card>

      <div className="mb-3 grid gap-3 lg:grid-cols-2">
        <Card title="⑤ 终审" pad>
          {p.final ? (
            <>
              <Badge tone={p.final.verdict === "pass" ? "ok" : "err"}>{p.final.verdict}</Badge>
              {p.final.suggested_label && <Badge tone="dim" className="ml-2">{p.final.suggested_label}</Badge>}
              {(p.final.fail_reasons || []).length > 0 && (
                <ul className="mt-1 list-inside list-disc text-xs text-err">
                  {p.final.fail_reasons.map((x: string, i: number) => <li key={i}>{x}</li>)}
                </ul>
              )}
            </>
          ) : e.complexity === "simple" ? (
            <Empty>（简单模式：跳过）</Empty>
          ) : (
            <Empty>无数据</Empty>
          )}
        </Card>
        <Card title="⑥ 回复预览" pad>
          {p.reply ? (
            <div className="max-h-[220px] overflow-y-auto text-sm whitespace-pre-wrap">{p.reply}</div>
          ) : (
            <Empty>无数据</Empty>
          )}
        </Card>
      </div>

      <Card title="⑦ Sessions 与直连调用" className="mb-3" pad>
        <table className="w-full border-collapse text-xs">
          <tbody>
            {Object.entries(e.session_ids || {}).filter(([k]) => k.startsWith("s")).map(([k, v]) => (
              <tr key={k} className="border-b border-line">
                <td className="py-1.5 pr-3 text-dim">{k.toUpperCase()}</td>
                <td className="role-micro py-1.5 pr-3 break-all">{String(v || "—")}</td>
                <td className="py-1.5 text-right">
                  {v ? <Button size="xs" variant="ghost" onClick={() => copyText(String(v))}>复制</Button> : null}
                </td>
              </tr>
            ))}
            {(e.direct_calls || []).map((c: any, i: number) => (
              <tr key={`d${i}`} className="border-b border-line">
                <td className="py-1.5 pr-3 text-dim">直连·{c.stage}</td>
                <td className="py-1.5 pr-3 text-dim" colSpan={2}>
                  {c.tokens} tokens · {c.elapsed}s · {c.ok ? "成功" : "失败"}
                </td>
              </tr>
            ))}
            {!Object.keys(e.session_ids || {}).length && !(e.direct_calls || []).length && (
              <tr><td><Empty>无</Empty></td></tr>
            )}
          </tbody>
        </table>
      </Card>

      <Card
        title="⑧ 档案文件"
        pad
        right={
          <a className="inline-flex items-center gap-1 text-xs text-acc hover:underline"
             href={`/api/issue-zip/${num}?repo=${encodeURIComponent(repo)}&token=${encodeURIComponent(getToken())}`}>
            <Download size={12} /> 打包下载
          </a>
        }
      >
        <div className="flex flex-wrap gap-1.5">
          {(d?.files || []).map((f: any) => (
            <Chip
              key={f.path}
              onClick={async () => {
                const c = await api.get(`/api/file?path=${encodeURIComponent(f.path)}`);
                push({ kind: "info", title: f.path, body: (c.content || "(空)").slice(0, 1600) });
              }}
            >
              {f.path}
            </Chip>
          ))}
          {!(d?.files || []).length && <Empty>无档案文件</Empty>}
        </div>
      </Card>

      {/* 重跑确认 */}
      <ConfirmDialog
        open={doRerun}
        title={`重跑 #${num}？`}
        confirmText="重跑"
        busy={busy}
        onClose={() => setDoRerun(false)}
        onConfirm={async () => {
          setBusy(true);
          try {
            const r = await api.post("/api/rerun", { issue: num, repo });
            if (!r.ok)
              push({ kind: "err", title: "重跑失败", body: r.error || "" });
            else if (r.triggered === false)
              push({ kind: "err", title: `#${num} 正在运行中`, body: "已重置为待处理，可在当前运行结束后再次重跑" });
            else
              push({ kind: "info", title: `#${num} 已重新排队` });
            setDoRerun(false);
          } finally {
            setBusy(false);
          }
        }}
      >
        将重置重试计数并从头执行全流程。当前 stage：{e.stage}
      </ConfirmDialog>

      {/* merge 修复确认 */}
      <ConfirmDialog
        open={doMerge}
        title={`对 #${num} 执行 merge 修复？`}
        confirmText="开始修复"
        busy={busy}
        onClose={() => setDoMerge(false)}
        onConfirm={async () => {
          setBusy(true);
          try {
            await api.post("/api/merge-fix", { issue: num, repo });
            push({ kind: "info", title: "merge 修复已启动", body: "冲突将由 merge agent 处理并重试合并" });
            setDoMerge(false);
          } finally {
            setBusy(false);
          }
        }}
      >
        将 fetch 修复分支、合并主分支（触发冲突）→ 由 merge agent 解决 → 推送并重试合并。
      </ConfirmDialog>

      {/* 补发流程 */}
      <Dialog
        open={!!preview}
        onClose={() => setPreview(null)}
        title={`补发 #${num}`}
        width={560}
        footer={
          preview?.can_publish ? (
            <>
              <Button variant="ghost" onClick={() => setPreview(null)}>取消</Button>
              <Button
                variant={preview.mode === "yolo" ? "danger" : "primary"}
                disabled={busy}
                onClick={() => setDoPublish(true)}
              >
                {busy ? "执行中…" : "确认补发"}
              </Button>
            </>
          ) : (
            <Button variant="ghost" onClick={() => setPreview(null)}>关闭</Button>
          )
        }
      >
        {preview && (
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Badge tone="dim">档位 {preview.mode}</Badge>
              <Badge tone={preview.can_publish ? "ok" : "err"}>
                {preview.can_publish ? "可补发" : "不可补发"}
              </Badge>
            </div>
            <div className="text-xs text-dim">{preview.hint}</div>
            <div className="panel2 p-3">
              <div className="mb-1 text-xs text-dim">产物清单</div>
              {Object.entries(preview.artifacts || {}).map(([k, v]) => (
                <div key={k} className="flex justify-between text-xs">
                  <span className="font-mono">{k}</span>
                  <span className={v ? "text-ok" : "text-dim"}>{v ? "✓" : "—"}</span>
                </div>
              ))}
            </div>
            {!preview.can_publish && (
              <div className="text-xs text-warn">
                提示：需切到 semi/full/yolo 且存在 fix.patch（DRY 执行产物）。若缺少 patch，请先「重跑」。
              </div>
            )}
          </div>
        )}
      </Dialog>

      <ConfirmDialog
        open={doPublish}
        danger={preview?.mode === "yolo"}
        title={`确认补发 #${num}（${preview?.mode}）？`}
        confirmText="执行补发"
        busy={busy}
        onClose={() => setDoPublish(false)}
        onConfirm={async () => {
          setBusy(true);
          try {
            await api.post("/api/publish", { issue: num, repo });
            push({ kind: "info", title: "补发已启动", body: "完成后将自动通知" });
            setDoPublish(false);
            setPreview(null);
          } finally {
            setBusy(false);
          }
        }}
      >
        将执行真实动作：分诊评论 → patch 重放（含门禁复验）→ 创建 PR
        {preview?.mode === "yolo" ? " → 自动合并" : "（合并需人工）"} → 回复评论。
      </ConfirmDialog>
    </div>
  );
}
