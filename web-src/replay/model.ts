export const stages: Record<string, string> = {
  pending: "排队中", audit: "报告审核", triage: "分诊定位", fix: "沙箱修复", gate: "构建门禁",
  review: "独立审查", final: "终审", rework: "返工", done: "已完成", rejected: "已退回",
  reply: "生成回复", execute: "执行", needs_human: "待人工介入", waiting_info: "等待补充", paused: "已暂停",
};
export const lanes: Record<string, string> = {
  review_bug: "正确性", review_minimal: "最小改动", review_style: "代码规范",
};
export const modes = ["dry", "semi", "full", "yolo", "paused"] as const;
export type Mode = typeof modes[number];
export interface Gate { active: number; limit: number; waiting: number; total_acquired: number }
export interface Review { pass: boolean; notes?: string; issues?: { desc: string }[] }
export interface Ticket { number: number; title: string; file: string }
export interface Trace {
  issue: number; stage: string; note: string; gate: Gate; diff?: string;
  revision?: number; passed?: boolean; lane?: string;
}
export interface Run { mode: Mode; issues: Ticket[]; events: Trace[] }
export interface CardData { key: string; number: number; title: string; meta: string; stage: string }
export function column(stage: string) {
  if (["pending", "paused", "waiting_info", "needs_human"].includes(stage)) return 0;
  if (["done", "rejected"].includes(stage)) return 2;
  return 1;
}

// A new candidate invalidates every opinion on the preceding diff.
export function evidence(trace: Trace[]) {
  let diff = "";
  let revision = 0;
  let reviews: Record<string, Review> = {};
  for (const event of trace) {
    if (event.diff !== undefined) {
      diff = event.diff;
      revision = event.revision ?? revision + 1;
      reviews = {};
    }
    if (event.lane && event.passed !== undefined)
      reviews[event.lane] = { pass: event.passed, notes: event.note };
  }
  return { diff, revision, reviews };
}
