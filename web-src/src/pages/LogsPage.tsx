import { useEffect, useRef, useState } from "react";
import { RefreshCcw } from "lucide-react";
import { api } from "../lib/api";
import { cn } from "../lib/utils";
import { Button, Card, Chip, Input } from "../components/ui";
import { PageHeader } from "../components/AppShell";

const LEVELS = [
  ["", "全部"],
  ["info", "信息"],
  ["warn", "警告"],
  ["error", "错误"],
];

function LogLine({ ln }: { ln: string }) {
  try {
    const d = JSON.parse(ln);
    const lvl = d.level || "info";
    return (
      <div className="border-b border-line/60 py-0.5 role-micro break-all whitespace-pre-wrap">
        <span className={cn(lvl === "error" ? "text-err" : lvl === "warn" ? "text-warn" : "text-dim")}>
          {d.ts} [{String(lvl).toUpperCase()}]
        </span>{" "}
        <b>{d.scope}</b> {d.event}
        {d.issue ? ` #${d.issue}` : ""}
        {d.detail ? <span className="text-dim"> {JSON.stringify(d.detail)}</span> : null}
      </div>
    );
  } catch {
    return <div className="border-b border-line/60 py-0.5 role-micro break-all whitespace-pre-wrap">{ln}</div>;
  }
}

export function LogsPage() {
  const [lines, setLines] = useState<string[]>([]);
  const [level, setLevel] = useState("");
  const [issue, setIssue] = useState("");
  const [q, setQ] = useState("");
  const [auto, setAuto] = useState(true);
  const boxRef = useRef<HTMLDivElement>(null);

  async function load() {
    try {
      const qs = new URLSearchParams({ level, issue, q, limit: "500" });
      const d = await api.get(`/api/log?${qs.toString()}`);
      setLines(d.lines || []);
    } catch {
      /* noop */
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [level]);

  useEffect(() => {
    if (!auto) return;
    const id = window.setInterval(load, 5000);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auto, level, issue, q]);

  return (
    <div>
      <PageHeader
        title="日志"
        subtitle="统一结构化日志 · logs/bot-YYYYMMDD.jsonl（保留期内按天滚动）"
        actions={
          <>
            {LEVELS.map(([v, l]) => (
              <Chip key={v} on={level === v} onClick={() => setLevel(v)}>{l}</Chip>
            ))}
            <Button size="sm" variant="ghost" onClick={load}>
              <RefreshCcw size={12} /> 刷新
            </Button>
          </>
        }
      />
      <Card pad>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <span className="text-xs text-dim">issue</span>
          <Input className="w-[70px]" value={issue} onChange={(e) => setIssue(e.target.value)} placeholder="全部" />
          <Input className="w-[200px]" value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索关键词…" />
          <Button size="sm" variant="outline" onClick={load}>查询</Button>
          <label className="flex cursor-pointer items-center gap-1.5 text-xs text-dim">
            <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> 自动刷新
          </label>
          <span className="ml-auto text-xs text-dim">{lines.length} 行</span>
        </div>
        <div ref={boxRef} className="panel2 max-h-[640px] overflow-y-auto p-3">
          {lines.map((ln, i) => <LogLine key={i} ln={ln} />)}
          {!lines.length && <div className="py-8 text-center text-xs text-dim">无匹配日志</div>}
        </div>
      </Card>
    </div>
  );
}
