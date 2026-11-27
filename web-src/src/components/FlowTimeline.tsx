import { cn, fmtAge } from "../lib/utils";
import type { Issue } from "../lib/types";

export const FLOW: [string, string][] = [
  ["audit", "问题审核"],
  ["triage", "分诊"],
  ["fix", "修复"],
  ["gate", "门禁"],
  ["review", "审查"],
  ["final", "终审"],
  ["reply", "回复"],
  ["execute", "执行"],
];

export function stageStatus(
  entry: Issue,
  key: string
): "done" | "failed" | "active" | "pending" | "skipped" {
  const hist = ((entry as any).history || []).filter((x: any) => x.stage === key);
  if (hist.length) {
    const st = hist[hist.length - 1].status;
    return st === "done" ? "done" : st === "failed" ? "failed" : "active";
  }
  if (key === "audit" && !(entry as any).audit) return "skipped";
  if (key === "triage" && entry.complexity === "simple") return "skipped";
  if (entry.stage === "done") return "done";
  if (entry.stage === "rejected") return "skipped";
  const order = FLOW.map((f) => f[0]);
  const idx = order.indexOf(key);
  const cur = order.indexOf(entry.stage);
  if (cur < 0) return "pending";
  if (cur > idx) return "done";
  if (cur === idx) return "active";
  return "pending";
}

export function FlowTimeline({ entry }: { entry: Issue }) {
  return (
    <div className="flex items-center overflow-x-auto py-1">
      {FLOW.map(([k, label], i) => {
        const s = stageStatus(entry, k);
        return (
          <div key={k} className="flex items-center">
            <div className="flex min-w-[62px] flex-col items-center gap-1.5 text-center">
              <span
                className={cn(
                  "h-3 w-3 rotate-45 border-2",
                  s === "done" && "border-ok bg-ok",
                  s === "failed" && "border-err bg-err",
                  s === "active" && "pulse-dot border-acc",
                  s === "skipped" && "border-dashed border-line2 opacity-40",
                  s === "pending" && "border-line2"
                )}
              />
              <span
                className={cn(
                  "text-[11px] whitespace-nowrap",
                  s === "active" ? "text-acc" : s === "done" ? "text-fg" : "text-dim",
                  s === "skipped" && "line-through opacity-60"
                )}
              >
                {label}
              </span>
            </div>
            {i < FLOW.length - 1 && (
              <span className={cn("mx-1 h-[2px] w-5 shrink-0", s === "done" ? "bg-ok" : "bg-line2")} />
            )}
          </div>
        );
      })}
    </div>
  );
}

export function ProgressHint({ progress }: { progress: any }) {
  if (!progress) return null;
  const l = progress.last || {};
  const active = (progress.age_sec || 99999) < 90;
  return (
    <span className={active ? "text-ok" : "text-dim"}>
      ⏱ 最后活动 {fmtAge(progress.age_sec)}前
      <span className="text-dim">
        （{l.stage || ""}
        {l.type ? ` · ${l.type}` : ""}
        {l.tool ? ` · ${l.tool}` : ""}）
      </span>
    </span>
  );
}
