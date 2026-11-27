import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, Check, CircleDot, GitPullRequest, Layers3, X } from "lucide-react";
import { column, lanes, stages, type CardData, type Gate, type Review } from "./model";
import "./workbench.css";

export function Capacity({ gate, mode }: { gate: Gate; mode: string }) {
  return <div className="capacity">
    <span><i className="lamp" />权限 <strong>{mode.toUpperCase()}</strong></span>
    <span><Layers3 size={15} />并发 <strong>{gate.active} / {gate.limit}</strong>
      <meter min={0} max={Math.max(gate.active, gate.limit)} value={gate.active} aria-label="已占用并发名额" /></span>
    <span>排队 <strong>{gate.waiting}</strong></span>
    <span>累计调用 <strong>{gate.total_acquired}</strong></span>
  </div>;
}

export function Board({ cards, selected, onSelect }: {
  cards: CardData[]; selected?: string; onSelect: (card: CardData) => void;
}) {
  return <div className="work-board">{["待处理", "处理中", "已结束"].map((name, i) => {
    const group = cards.filter(card => column(card.stage) === i);
    return <section className={'work-column c' + i} key={name}><h3><i />{name}<small>{group.length}</small></h3>
      {group.map(card => <button key={card.key} className={'work-ticket ' + (selected === card.key ? "chosen" : "")}
        aria-pressed={selected === undefined ? undefined : selected === card.key} onClick={() => onSelect(card)}>
        <span className="ticket-id">#{card.number}<ArrowUpRight size={14} /></span>
        <strong>{card.title}</strong><span className="ticket-foot"><span>{stages[card.stage] || card.stage}</span>{card.meta}</span>
      </button>)}
      {!group.length && <div className="board-empty">此列暂无工单</div>}
    </section>;
  })}</div>;
}

function DiffView({ diff }: { diff: string }) {
  const [view, setView] = useState("unified");
  const lines = diff.trimEnd().split("\n");
  return <div className="patch-view"><div className="drawer-heading"><h3>补丁对比</h3>
    <div className="view-switch">{[["unified", "统一"], ["before", "修改前"], ["after", "修改后"]].map(([id, name]) =>
      <button key={id} aria-pressed={view === id} onClick={() => setView(id)}>{name}</button>)}</div></div>
    {diff ? <pre aria-label="Diff 片段">{lines.map((line, i) => {
      const kind = line.startsWith("+++") || line.startsWith("---") ? "file" :
        line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "context";
      if ((view === "before" && kind === "add") || (view === "after" && kind === "del")) return null;
      return <code key={i} className={kind}><b aria-hidden="true">{i + 1}</b>{line}</code>;
    })}</pre> : <p className="quiet">此阶段还没有可查看的补丁。</p>}
  </div>;
}

export function ReviewDrawer({ number, title, diff, reviews, errors = {}, onClose, truncated }: {
  number: number; title: string; diff: string; reviews: Record<string, Review>; errors?: Record<string, string[]>; onClose: () => void; truncated?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current!;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  const keys = reviews.review_simple ? ["review_simple"] : Object.keys(lanes);
  return <dialog className="review-drawer" ref={ref} aria-labelledby="review-title" onCancel={onClose}
    onClick={e => { if (e.target === ref.current) {
      const rect = ref.current.getBoundingClientRect();
      if (e.clientX < rect.left || e.clientX > rect.right) onClose();
    } }}>
    <header><span><GitPullRequest size={16} /> REVIEW / #{number}</span>
      <button aria-label="关闭审阅" onClick={onClose}><X size={18} /></button></header>
    <h2 id="review-title">{title}</h2><div className="drawer-heading"><h3>独立审查意见</h3>
      <small>{Object.keys(reviews).length} / {keys.length}</small></div>
    <div className="lane-list">{keys.map(key => {
      const review = reviews[key];
      const failed = errors[key];
      return <article key={key}><div><strong>{lanes[key] || "简单线审查"}</strong>
        <span className={review ? review.pass ? "pass" : "fail" : failed ? "fail" : "pending"}>
          {review ? review.pass ? <Check size={12} /> : <X size={12} /> : <CircleDot size={12} />}
          {review ? review.pass ? "通过" : "需修改" : failed ? "调用失败" : "待返回"}</span></div>
        {failed && <p>{failed.join(" · ")}</p>}
        {review?.notes && <p>{review.notes}</p>}
        {review?.issues?.map((issue, i) => <p key={i}>{issue.desc}</p>)}
      </article>;
    })}</div><DiffView diff={diff} />{truncated && <p className="quiet">仅显示前 160,000 个字符。</p>}</dialog>;
}
