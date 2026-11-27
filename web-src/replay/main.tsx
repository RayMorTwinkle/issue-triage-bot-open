import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Activity, ArrowRight, ChevronRight, GitBranch, GitPullRequest, ListTree, Pause, Play, RotateCcw, StepForward } from "lucide-react";
import { Board, Capacity, ReviewDrawer } from "./Workbench";
import { evidence, modes, stages, type Mode, type Run } from "./model";

function Playback({ run, switchMode }: { run: Run; switchMode: (mode: Mode) => void }) {
  const split = run.events.findIndex(e => e.stage === "rework");
  const [cursor, setCursor] = useState(split < 0 ? run.events.length : split + 1);
  const [selected, setSelected] = useState(152);
  const [playing, setPlaying] = useState(false);
  const [opened, setOpened] = useState(false);
  const atEnd = cursor >= run.events.length;
  useEffect(() => {
    if (!playing || atEnd) return;
    const id = window.setTimeout(() => setCursor(value => value + 1), 750);
    return () => window.clearTimeout(id);
  }, [playing, cursor, atEnd]);
  function seek(value: number) { setPlaying(false); setCursor(value); }
  const visible = run.events.slice(0, cursor);
  const latest = visible[visible.length - 1];
  const gate = latest?.gate || { limit: 2, active: 0, waiting: 0, total_acquired: 0 };
  const cards = run.issues.map(issue => {
    const history = visible.filter(e => e.issue === issue.number);
    return { ...issue, key: String(issue.number), meta: issue.file || "report",
      stage: history[history.length - 1]?.stage || "pending" };
  });
  const item = cards.find(card => card.number === selected)!;
  const trace = visible.filter(e => e.issue === selected);
  const current = evidence(trace);
  return <><header className="station-nav"><div><span className="station-mark"><GitPullRequest size={19} /></span>
    <strong>Issue Triage</strong><i>/</i><span>工作台</span></div><span className="offline"><i /> 本地回放</span></header>
    <main className="station"><div className="work-title"><div><small><GitBranch size={14} /> local / triage-samples</small><h1>分诊工作台</h1></div>
      <label>回放档位 <select aria-label="回放档位" value={run.mode} onChange={e => switchMode(e.target.value as Mode)}>
        {modes.map(m => <option value={m} key={m}>{m.toUpperCase()}</option>)}</select></label></div>
      <Capacity gate={gate} mode={run.mode} />
      <div className="section-head"><h2><ListTree size={18} />任务队列</h2><span>{cards.length} 个工单</span></div>
      <Board cards={cards} selected={item.key} onSelect={card => setSelected(card.number)} />
      <section className="transport" aria-label="事件回放">
        <button className="play-btn" aria-label={playing && !atEnd ? "暂停回放" : "播放回放"} onClick={() => {
          if (atEnd) { setCursor(0); setPlaying(true); } else setPlaying(!playing);
        }}>{playing && !atEnd ? <Pause size={16} /> : <Play size={16} />}</button>
        <button aria-label="重置回放" onClick={() => seek(0)}><RotateCcw size={16} /></button>
        <button aria-label="下一事件" disabled={atEnd} onClick={() => seek(cursor + 1)}><StepForward size={17} /></button>
        <input aria-label="事件进度" type="range" min="0" max={run.events.length} value={cursor} onChange={e => seek(+e.target.value)} />
        <output>{cursor} / {run.events.length}</output>
      </section>
      <section className="inspector"><div className="inspect-title"><small>ISSUE / {item.number}</small><h2>{item.title}</h2>
        <p>{item.file || "报告审核"} <ChevronRight size={13} /> {stages[item.stage]}{current.revision > 0 && <span> · 补丁 v{current.revision}</span>}</p>
        <button className="inspect-action" onClick={() => { setPlaying(false); setOpened(true); }}>
          <GitPullRequest size={15} />审查意见与 Diff <ArrowRight size={15} /></button></div>
        <div className="event-list"><h3><Activity size={16} /> 状态时间线</h3>
          {trace.length ? <ol>{trace.slice().reverse().map((e, i) =>
            <li key={trace.length-i}><span className="event-index">{String(trace.length-i).padStart(2, "0")}</span>
              <strong>{stages[e.stage]}</strong><span>{e.note}</span></li>)}</ol> : <p className="quiet">工单在队列中等待审核。</p>}</div>
      </section>
    </main>{opened && <ReviewDrawer number={selected} title={item.title} reviews={current.reviews} diff={current.diff} onClose={() => setOpened(false)} />}</>;
}


function App() {
  const [data, setData] = useState<Record<Mode, Run>>();
  const [error, setError] = useState("");
  const [mode, setMode] = useState<Mode>("dry");
  useEffect(() => {
    fetch("./recordings.json").then(r => {
      if (!r.ok) throw new Error(String(r.status));
      return r.json();
    }).then(setData).catch(e => setError(String(e)));
  }, []);
  if (!data) return <main className="station"><h1>{error ? "回放读取失败" : "正在载入回放…"}</h1>
    {error && <p>{error} <button onClick={() => location.reload()}>重新载入</button></p>}</main>;
  return <Playback key={mode} run={data[mode]} switchMode={setMode} />;
}

createRoot(document.getElementById("root")!).render(<App />);
