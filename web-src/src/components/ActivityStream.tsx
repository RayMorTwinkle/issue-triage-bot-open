import {
  AlertTriangle, CheckCircle2, FileText, Info, Play, Wrench, Zap,
} from "lucide-react";
import type { ActivityEvent } from "../lib/types";
import { cn, fmtClock } from "../lib/utils";

function agentDesc(e: ActivityEvent) {
  switch (e.type) {
    case "step_start":
      return { icon: Play, text: "开始推理", cls: "text-dim" };
    case "tool_use":
      return { icon: Wrench, text: `工具 ${e.tool || ""}`, cls: "text-acc" };
    case "text":
      return { icon: FileText, text: `输出 ${e.chars || 0} 字符`, cls: "text-dim" };
    case "step_finish":
      return { icon: CheckCircle2, text: "一步完成", cls: "text-ok" };
    default:
      return { icon: Info, text: e.type || "?", cls: "text-dim" };
  }
}

const BOT_LABEL: Record<string, string> = {
  mode_change: "切换全局档位",
  new_issue: "发现新 issue",
  done: "issue 完成",
  poll: "轮询",
  job_crash: "任务异常",
  needs_human: "需要人工介入",
  release_start: "发布开始",
  release_finish: "发布结束",
  release_due: "发布条件达成",
  publish_start: "补发开始",
  publish_finished: "补发结束",
  merge_fixed: "合并修复完成",
  merge_retry_via_agent: "合并失败 → 调 merge agent",
  merged: "已自动合并",
  init_start: "初始化开始",
  init_finished: "初始化结束",
  paused_at_boundary: "环节边界暂停",
  session_cleanup: "清理 session",
  rerun: "手动重跑",
  started: "服务启动",
};

function botDesc(e: ActivityEvent) {
  const lvl = e.level || "info";
  const icon =
    lvl === "error" || lvl === "warn" ? AlertTriangle : Zap;
  return {
    icon,
    text: BOT_LABEL[e.event || ""] || e.event || "事件",
    cls: lvl === "error" ? "text-err" : lvl === "warn" ? "text-warn" : "text-dim",
  };
}

export function ActivityStream({ events, height = 430 }: { events: ActivityEvent[]; height?: number }) {
  const evs = [...events].reverse();
  return (
    <div className="overflow-y-auto pr-1" style={{ maxHeight: height }}>
      {evs.map((e, i) => {
        const d = e.source === "agent" ? agentDesc(e) : botDesc(e);
        const Icon = d.icon;
        const tag = e.repo
          ? `${e.repo.split("/")[1] || e.repo}${e.issue ? `#${e.issue}` : ""}`
          : e.scope || "bot";
        return (
          <div key={`${e.ts || ""}-${i}`} className="evrow fadeup">
            <span className="evtime">{fmtClock(e.ts)}</span>
            <span className="evtag">{tag}</span>
            <span className={cn("flex min-w-0 flex-1 items-center gap-1.5", d.cls)}>
              <Icon size={11} className="shrink-0" />
              <span className="truncate">
                {e.source === "agent" ? `${e.stage || ""} · ${d.text}` : d.text}
              </span>
              {e.source === "bot" && e.detail && (
                <span className="role-micro shrink-0">
                  {JSON.stringify(e.detail).slice(0, 90)}
                </span>
              )}
            </span>
          </div>
        );
      })}
      {!evs.length && (
        <div className="py-10 text-center text-xs text-dim">暂无活动 — 等待 agent 施工…</div>
      )}
    </div>
  );
}
