"""流水线状态机：分诊 → 修复 → 硬门禁 → 三路审查 → 终审 → 回复审查 → 按档位执行。

设计要点（对应 PLAN.md / docs/flow-*.md）：
- S1 分诊+修复同 session 续跑；S2/S3/S4 并行独立；S5/S6 新 session。
- 重试预算（PLAN §6）：JSON 纠偏 ≤3、编译返工 ≤3、终审返工 ≤2、全程默认 4h（issue_time_budget_sec）。
- 超限 → needs_human（label + macOS 通知）。
- DRY_RUN 对 GitHub 零写操作，产物全部进 outbox/。
- 审查前后做 diff 防篡改比对（oc-run2 --auto 下 agent 理论上有写权限）。

并发约定：调度器按项目与工单去重，每个工作线程独占一个 Pipeline。
"""
from functools import partial
from subprocess import TimeoutExpired
import json
import os
import time

import github as gh
import llm
import llm_direct
import prompts_cfg
import sandbox
import state as state_mod
from util import append_log, atomic_write_json, log_event, notify, render, Timer

from workflow import Decision, ReworkLimit, REVIEW_STAGES, verified_revision, writable
from resilience import CircuitOpen, repo_read_circuit
NEEDS_INFO_LABEL = "triage:needs-info"
MAX_INFO_ROUNDS = 2   # 追问轮数上限：超过 → needs_human（人工介入）


def _validate_audit(data: dict) -> list:
    """问题审核输出的 schema 校验（中文错误列表）。"""
    err = []
    if data.get("verdict") not in ("ok", "reject"):
        err.append("verdict 必须为 ok/reject")
    if data.get("category") not in ("safe", "injection", "malicious",
                                    "spam", "meaningless", "irrelevant"):
        err.append("category 取值不合法")
    if data.get("complexity") not in ("simple", "complex"):
        err.append("complexity 必须为 simple/complex")
    if not isinstance(data.get("reason"), str):
        err.append("reason 应为字符串")
    if not isinstance(data.get("reject_reply"), str):
        err.append("reject_reply 应为字符串")
    if data.get("verdict") == "reject" and not (data.get("reject_reply") or "").strip():
        err.append("verdict=reject 时 reject_reply 不能为空")
    return err


class Pipeline:
    def __init__(self, cfg: dict, project: dict = None, log_dir: str = None):
        self.cfg = cfg
        self.project = project or {}
        self.base = os.path.dirname(os.path.abspath(__file__))
        self.log_dir = log_dir or os.path.join(self.base, "logs")

    # ---- 项目级配置（动态读取：projects 表优先 → 全局 cfg fallback）----

    @property
    def repo(self) -> str:
        return self.project.get("repo") or self.cfg.get("repo", "")

    @property
    def repo_url(self) -> str:
        return self.project.get("repo_url") or self.cfg.get("repo_url", "")

    @property
    def main_branch(self) -> str:
        return self.project.get("main_branch") or self.cfg.get("main_branch", "main")

    @property
    def build_cmd(self) -> list:
        return self.project.get("build_cmd") or self.cfg.get("build_cmd", ["true"])

    @property
    def build_env(self):
        return self.project.get("build_env") or self.cfg.get("build_env") or None

    @property
    def project_mode(self):
        return self.project.get("mode")   # 三层档位中间层（default/None=跟随全局）

    # ---------------- 基础设施 ----------------

    def _sandbox_path(self, n):
        root = self.cfg.get("sandbox_root", "~/.cache/issue-triage-bot/sandbox")
        # Repository names are scoped directories so equal issue numbers cannot collide.
        from hashlib import sha256
        project_key = sha256(self.repo.encode()).hexdigest()[:24]
        return sandbox.sandbox_path(os.path.join(root, project_key), n)

    def _transition(self, st, n, entry, stage):
        previous = entry.get("stage", "pending")
        entry["stage"] = stage
        state_mod.save_issue(st, n, repo=self.repo, worker=True)
        if previous != stage:
            self._log(n, event="stage_transition", previous=previous, stage=stage)

    def _issue_dir(self, n: int) -> str:
        d = os.path.join(self.log_dir, "projects",
                         state_mod.project_slug(self.repo), "issues", str(n))
        os.makedirs(d, exist_ok=True)
        return d

    def _outbox(self, n: int) -> str:
        d = os.path.join(self._issue_dir(n), "outbox")
        os.makedirs(d, exist_ok=True)
        return d

    def _log(self, n: int, **rec) -> None:
        """issue 档案日志（项目分域：logs/projects/<slug>/issues/<n>/run.jsonl）
        + 统一日志（bot-*.jsonl 按天，带级别）。n<=0 的轮询类日志入 poll.jsonl。"""
        if isinstance(n, int) and n > 0:
            append_log(os.path.join(self._issue_dir(n), "run.jsonl"), rec)
        else:
            d = os.path.join(self.log_dir, "projects", state_mod.project_slug(self.repo))
            os.makedirs(d, exist_ok=True)
            append_log(os.path.join(d, "poll.jsonl"), rec)
        ev = rec.get("event", "?")
        level = "info"
        if ev in ("needs_human", "poll_error", "error"):
            level = "error"
        elif ev == "audit_reject" or rec.get("ok") is False:
            level = "warn"
        log_event(level, "pipeline", ev,
                  issue=(n if isinstance(n, int) and n > 0 else None),
                  log_dir=self.log_dir, repo=self.repo,
                  **{k: v for k, v in rec.items() if k != "event"})

    def _progress_cb(self, n: int):
        """返回 (context, ev) 事件回调：直连 agent 的事件流实时落盘到 issue 档案
        （agent-progress.jsonl —— 前端"最后活动"与事后诊断的数据源）。"""
        path = os.path.join(self._issue_dir(n), "agent-progress.jsonl")

        def cb(context: str, ev: dict):
            etype = (ev or {}).get("type") or "?"
            part = (ev or {}).get("part") or {}
            rec = {"stage": context, "type": etype}
            if etype == "tool_use":
                rec["tool"] = part.get("tool")
            elif etype == "text":
                rec["chars"] = len(part.get("text") or "")
            append_log(path, rec)
        return cb

    def _needs_human(self, st, n: int, reason: str):
        entry = state_mod.issue_entry(st, n)
        entry["error"] = reason
        self._transition(st, n, entry, "needs_human")
        self._log(n, event="needs_human", reason=reason)
        mode = self._mode_now(n, entry)
        if writable(mode):
            try:
                gh.add_label(self.repo, n, "triage:needs-human")
            except (RuntimeError, OSError, TimeoutExpired) as exc:
                self._log(n, event="label_failed", kind=type(exc).__name__)
        notify("issue-triage-bot", f"#{n} 需人工介入: {reason[:80]}")

    def _agents_md(self, sandbox_dir: str) -> str:
        try:
            with open(os.path.join(sandbox_dir, "AGENTS.md"), encoding="utf-8") as f:
                return f.read()[:6000]
        except OSError:
            return "(仓库未提供 AGENTS.md)"

    def _repo_tree(self, sandbox_dir: str) -> str:
        rc, out = sandbox.run_build(["git", "ls-tree", "-r", "--name-only", "HEAD"],
                                    sandbox_dir, timeout=30)
        return "\n".join((out or "").splitlines()[:120])

    def _disclosure(self) -> str:
        """AI 披露头；config.disclosure_enabled=false 时关闭（可选项）。"""
        if not self.cfg.get("disclosure_enabled", True):
            return ""
        return prompts_cfg.load()["shared"].get("disclosure_header", "")

    def _add_cost(self, entry, r: "llm.LLMResult"):
        entry["cost"] = round(entry.get("cost", 0) + r.cost, 6)
        entry["tokens"] = entry.get("tokens", 0) + r.tokens

    def _mark(self, entry, stage: str, status: str):
        """阶段历史（前端流程图数据源）。status: done|failed"""
        from util import now_ts
        entry.setdefault("history", []).append(
            {"stage": stage, "status": status, "ts": now_ts()})

    def _add_direct_usage(self, entry, stage_label: str, r: "llm_direct.DirectResult"):
        """直连调用记录：tokens 计入统一口径 + direct_calls 明细（前端展示用）。"""
        tok = int((r.usage or {}).get("total_tokens") or 0)
        entry["tokens"] = entry.get("tokens", 0) + tok
        entry.setdefault("direct_calls", []).append(
            {"stage": stage_label, "tokens": tok,
             "elapsed": round(r.elapsed, 1), "ok": r.ok})

    # ---------------- ★ 问题审核（直连·廉价，clone 之前） ----------------

    def _audit(self, st, n, entry, issue):
        """安全筛查 + 复杂度判定。返回 audit dict；失败返回 None（已置 needs_human）。"""
        self._transition(st, n, entry, "audit")
        if not llm_direct.available(self.cfg):
            self._mark(entry, "audit", "failed")
            self._needs_human(st, n, "直连 LLM 未配置——请在环境页配置 "
                                     "LLM_API_BASE / LLM_API_KEY / LLM_MODEL 后重跑")
            return None

        prompts = prompts_cfg.load()
        shared = prompts["shared"]
        inputs = {"issue_fulltext": gh.issue_text(issue),
                  "repo": self.repo,
                  "repo_intro": self.project.get("intro") or self.cfg.get("repo_intro") or "(未配置项目简介)"}
        system, user = llm.assemble_prompt("audit", prompts, inputs)
        fmt = llm.format_spec("audit", prompts)

        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        last = None
        for attempt in (1, 2):  # 首发 + 一次纠偏
            r = llm_direct.chat_json(messages, self.cfg, parse=llm.parse_json,
                                     validate=_validate_audit, max_tokens=4096)
            self._add_direct_usage(entry, "audit", r)
            self._log(n, event="audit_call", attempt=attempt, ok=r.ok,
                      elapsed=round(r.elapsed, 1),
                      tokens=(r.usage or {}).get("total_tokens"),
                      errors=r.errors)
            if r.ok:
                self._mark(entry, "audit", "done")
                return r.data
            last = r
            if attempt == 1:
                corr = render(shared.get("correction_template", ""), {
                    "last_output": (r.content or "")[:4000],
                    "errors": "\n".join(f"- {e}" for e in r.errors),
                    "format_spec": fmt})
                messages = [{"role": "system", "content": system},
                            {"role": "user", "content": user},
                            {"role": "assistant", "content": r.content or "(空输出)"},
                            {"role": "user", "content": corr}]
        self._mark(entry, "audit", "failed")
        self._needs_human(st, n, f"问题审核失败: {'; '.join(last.errors if last else ['未知'])}")
        return None

    def _execute_reject(self, st, n, entry, mode, audit):
        """审核拒绝：发/落盘"不处理"回复 + 标签；不关闭 issue。"""
        reply = (audit.get("reject_reply") or "").strip() or \
            "感谢反馈。经自动审核，该内容不在本项目处理范围内，此 issue 暂不进一步处理。"
        disclosure = self._disclosure()
        head = f"{disclosure}\n\n" if disclosure else ""
        path = os.path.join(self._outbox(n), "final-reply.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{head}{reply[:2400]}")
        if writable(self._mode_now(n, entry)):
            gh.comment_file(self.repo, n, path)
            self._labels(n, entry, add=("triage:rejected",),
                         remove=("triage:needs-human", NEEDS_INFO_LABEL, "triage:pending", "triage:accepted"))
        self._transition(st, n, entry, "rejected")
        self._log(n, event="audit_reject", category=audit.get("category"), mode=mode)

    # ---------------- ★ 回复生成（直连·廉价，简单/复杂线共用） ----------------

    def _make_reply(self, st, n, entry, issue_text, diff, budget):
        """生成作者回复；失败返回 None（已置 needs_human）。"""
        if self._over_budget(budget):
            self._needs_human(st, n, "超过单 issue 时间预算")
            return None
        self._transition(st, n, entry, "reply")
        if self._paused_guard(st, n, entry):
            return None
        if not llm_direct.available(self.cfg):
            self._mark(entry, "reply", "failed")
            self._needs_human(st, n, "直连 LLM 未配置，无法生成回复")
            return None
        prompts = prompts_cfg.load()
        shared = prompts["shared"]
        inputs = {"issue_fulltext": issue_text,
                  "diff": diff,
                  "review_summary": json.dumps(entry.get("review_json") or {},
                                               ensure_ascii=False, indent=1),
                  "final_summary": json.dumps(entry.get("final_json") or {},
                                              ensure_ascii=False),
                  "fix_summary": entry.get("summary", ""),
                  "max_reply_chars": str(shared.get("max_reply_chars", 1500))}
        system, user = llm.assemble_prompt("reply", prompts, inputs)
        r = llm_direct.chat([{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                            self.cfg, max_tokens=4096)
        self._add_direct_usage(entry, "reply", r)
        self._log(n, event="reply_call", ok=r.ok, elapsed=round(r.elapsed, 1),
                  tokens=(r.usage or {}).get("total_tokens"), errors=r.errors)
        if not r.ok:
            self._mark(entry, "reply", "failed")
            self._needs_human(st, n, f"回复生成失败: {'; '.join(r.errors)}")
            return None
        self._mark(entry, "reply", "done")
        return r.content.strip()

    # ---------------- ★ 简单模式主链路（2 个 oc-run2 agent） ----------------

    def _run_simple_flow(self, st, n, entry, mode, issue, sandbox_dir, issue_dir, budget):
        """修复 → 门禁 → 审核代码（1 个）→ 返工 ≤2 → 回复 → 执行。"""
        issue_text = gh.issue_text(issue)
        ok, diff = self._fix_and_gate(st, n, entry, sandbox_dir, issue_dir,
                                     "", budget, issue_text=issue_text)
        if not ok:
            return
        ok, diff = self._review_simple_loop(st, n, entry, sandbox_dir, issue_dir,
                                            issue_text, diff, budget)
        if not ok:
            return
        reply = self._make_reply(st, n, entry, issue_text, diff, budget)
        if reply is None:
            return
        self._execute(st, n, entry, mode, reply, diff)

    def _review_simple_loop(self, st, n, entry, sandbox_dir, issue_dir,
                            issue_text, diff, budget) -> tuple:
        """审核代码（会话 s2）；不过 → 带反馈返工 ≤2；超限 needs_human。返回 (ok, diff)。"""
        for loop in range(1, int(self.cfg.get("max_rework_loops", 2)) + 2):
            if self._over_budget(budget):
                self._needs_human(st, n, "超过单 issue 时间预算")
                return False, None
            if self._paused_guard(st, n, entry):
                return False, None
            self._transition(st, n, entry, "review")
            r = llm.run_stage("review_simple", sandbox_dir, issue_dir,
                              session_id=entry["session_ids"].get("s2"),
                              inputs={"issue_fulltext": issue_text, "diff": diff,
                                      "changed_files": json.dumps(
                                          (entry.get("fix_json") or {}).get("changed_files", []),
                                          ensure_ascii=False),
                                      "repo_root": sandbox_dir},
                              cfg=self.cfg, progress=self._progress_cb(n))
            entry["session_ids"]["s2"] = r.session_id
            self._add_cost(entry, r)
            self._log(n, event="review", mode="simple", loop=loop, ok=r.ok,
                      passed=(r.data or {}).get("pass"))
            if not r.ok:
                self._mark(entry, "review", "failed")
                self._needs_human(st, n, f"审核失败: {'; '.join(r.errors)}")
                return False, None
            entry["review_json"] = {"review_simple": r.data}
            self._archive_review(n, diff, entry["review_json"])
            if sandbox.diff_fingerprint(sandbox_dir) != getattr(self, "_fp1", None):
                self._needs_human(st, n, "审核期间工作区 diff 发生变化（防篡改检查），已中止")
                return False, None
            if r.data["pass"]:
                self._mark(entry, "review", "done")
                return True, diff
            c = state_mod.bump_retry(entry, "review_fail")
            self._mark(entry, "review", "failed")
            if c > int(self.cfg.get("max_rework_loops", 2)):
                self._needs_human(st, n, "审核返工超限: " + json.dumps(
                    r.data.get("issues", []), ensure_ascii=False))
                return False, None
            feedback = ("审核意见：\n" + "\n".join(
                f"- [{i.get('severity')}] {i.get('desc')}" for i in r.data.get("issues", []))
                + f"\n审核说明：{r.data.get('notes', '')}")
            ok, diff = self._fix_and_gate(st, n, entry, sandbox_dir, issue_dir,
                                         feedback, budget, issue_text=issue_text)
            if not ok:
                return False, None
        self._needs_human(st, n, "审核循环异常退出")
        return False, None

    def _over_budget(self, budget: Timer) -> bool:
        return budget.elapsed() > int(self.cfg.get("issue_time_budget_sec", 14400))

    # ---------------- 轮询 ----------------

    def poll(self) -> list:
        """发现新 issue（state 无记录的 open issue）。返回编号列表。"""
        st = state_mod.load_project(self.repo)
        if st.get("global_mode") == "paused" or self.project_mode == "paused":
            return []   # 总开关：连 gh list 调用都不发
        found = []
        try:
            with repo_read_circuit(self.repo).call():
                issues = gh.list_open_issues(self.repo)
        except (RuntimeError, OSError, TimeoutExpired, json.JSONDecodeError) as e:
            self._log(0, event="poll_error", error=str(e))
            return []
        for it in issues:
            key = str(it["number"])
            if key in st["issues"]:
                continue
            n = it["number"]
            e = state_mod.issue_entry(st, n)
            e["title"] = it.get("title", "")
            e["stage"] = "pending"
            if writable(self._mode_now(n, e)):
                gh.add_label(self.repo, n, "triage:pending")
            self._log(n, event="new_issue", title=it.get("title", ""))
            found.append(n)
        # 窄写：只 upsert 本轮新发现的 issue。st 是 poll 开始时的快照，
        # 全量 save_project 会把在跑流水线已推进的行用旧值回滚（lost update）。
        for n in found:
            state_mod.save_issue(st, n, repo=self.repo, worker=True)
        return found

    # ---------------- 单 issue 主流程 ----------------

    def process(self, n: int) -> str:
        """处理单个 issue，返回最终 stage。"""
        st = state_mod.load_project(self.repo)
        entry = state_mod.issue_entry(st, n)
        mode = state_mod.effective_mode(st, entry, self.project_mode)
        if mode == "paused":
            return "paused"
        if entry.get("stage") not in ("pending", "needs_human"):
            return entry.get("stage")  # 防重入：处理中/已完成直接跳过

        issue_dir = self._issue_dir(n)
        budget = Timer()
        prompts_cfg.snapshot_to(os.path.join(issue_dir, "prompts-snapshot"))

        # 断点续跑：paused_from 暂停恢复 / needs_human 重跑时，已产出的环节不重来
        # （不重复扣费、不重复发分诊评论）。audit/triage 结论已入库则直接复用。
        if entry.get("paused_from"):
            self._log(n, event="resumed", paused_from=entry["paused_from"])
            entry["paused_from"] = None   # 恢复即清除，不残留"暂停于 X"标记
            state_mod.save_issue(st, n, repo=self.repo, worker=True)

        # 读 issue（dry 模式也读真实 issue——只读不写，保证分析质量）
        try:
            with repo_read_circuit(self.repo).call():
                issue = gh.get_issue(self.repo, n)
        except (RuntimeError, OSError, TimeoutExpired, json.JSONDecodeError) as e:
            self._log(n, event="issue_read_failed", kind=type(e).__name__,
                      circuit_open=isinstance(e, CircuitOpen))
            self._needs_human(st, n, f"读取 issue 失败: {e}")
            return entry["stage"]

        # ★ 问题审核（直连·廉价；在 clone 之前——恶意/无关内容不进入后续流程）
        audit = entry.get("audit")
        if audit:
            self._log(n, event="resume_skip_audit")
        else:
            try:
                audit = self._audit(st, n, entry, issue)
            except Exception as e:
                self._needs_human(st, n, f"问题审核异常: {type(e).__name__}: {e}")
                return entry["stage"]
            if audit is None:
                return entry["stage"]  # 审核失败 → needs_human 已置
            entry["audit"] = audit
        entry["error"] = None
        entry["complexity"] = audit.get("complexity")
        state_mod.save_issue(st, n, repo=self.repo, worker=True)
        if self._paused_guard(st, n, entry):
            return entry["stage"]
        if audit["verdict"] == "reject":
            try:
                self._execute_reject(st, n, entry, mode, audit)
            except (RuntimeError, OSError, TimeoutExpired) as exc:
                self._needs_human(st, n, f"审核回复失败: {exc}")
            return entry["stage"]

        # 沙箱
        sandbox_dir = self._sandbox_path(n)
        branch = f"fix/issue-{n}"
        try:
            sandbox.clone(self.repo_url, sandbox_dir, self.main_branch)
            sandbox.create_branch(sandbox_dir, branch)
        except (RuntimeError, OSError) as e:
            sandbox.cleanup(sandbox_dir)
            self._needs_human(st, n, f"沙箱 clone 失败: {e}")
            return entry["stage"]
        entry["sandbox_dir"], entry["branch"] = sandbox_dir, branch
        # 起始 stage 按分流显示（简单线从 fix 开始；复杂线从 triage 开始）
        self._transition(st, n, entry, "fix" if audit.get("complexity") == "simple" else "triage")

        try:
            # ★ 按问题审核的复杂度自动分流
            if audit.get("complexity") == "simple":
                self._run_simple_flow(st, n, entry, mode, issue, sandbox_dir, issue_dir, budget)
            else:
                self._run_flow(st, n, entry, mode, issue, sandbox_dir, issue_dir, budget)
        except Exception as e:  # 兜底：任何异常不炸调度器
            self._needs_human(st, n, f"流水线异常: {type(e).__name__}: {e}")
        finally:
            sandbox.cleanup(sandbox_dir)
            if entry.get("stage") in ("done", "rejected"):
                self._cleanup_sessions(st, n, entry)
            state_mod.save_issue(st, n, repo=self.repo, worker=True)
        return entry["stage"]

    def _cleanup_sessions(self, st, n, entry):
        """issue 结束后按 config.cleanup_sessions 删除对应 opencode2 session。"""
        want = set(self.cfg.get("cleanup_sessions") or [])
        if not want:
            return
        for key, sid in (entry.get("session_ids") or {}).items():
            if key in want and sid:
                ok = llm.delete_session(sid)
                self._log(n, event="session_cleanup", key=key, sid=sid, ok=ok)

    # ---------------- 主链路 ----------------

    def _run_flow(self, st, n, entry, mode, issue, sandbox_dir, issue_dir, budget):
        issue_text = gh.issue_text(issue)

        # ① 分诊（S1 首发）。暂停恢复/重跑且已有 accept/reject 结论 → 跳过：
        # 不重复扣费、不重复发分诊评论（need_info 重审须重新分诊，不在此列）。
        v = entry.get("verdict") or {}
        if v.get("verdict") in ("accept", "reject"):
            self._log(n, event="resume_skip_triage")
        else:
            r = llm.run_stage("triage", sandbox_dir, issue_dir, inputs={
                "issue_fulltext": issue_text,
                "repo": self.repo,
                "repo_root": sandbox_dir,
                "agents_md": self._agents_md(sandbox_dir),
                "repo_tree": self._repo_tree(sandbox_dir),
            }, cfg=self.cfg, progress=self._progress_cb(n))
            entry["session_ids"]["s1"] = r.session_id
            self._add_cost(entry, r)
            self._log(n, event="triage", ok=r.ok, cost=r.cost, attempts=r.attempts)
            self._mark(entry, "triage", "done" if r.ok else "failed")
            if not r.ok:
                self._needs_human(st, n, f"分诊失败: {'; '.join(r.errors)}")
                return
            entry["verdict"] = r.data
            if self._paused_guard(st, n, entry):
                return
            if r.data["verdict"] == "need_info":
                self._enter_waiting_info(st, n, entry, mode, r.data, issue)
                return
            cpath = self._triage_comment_file(n, r.data)
            if writable(self._mode_now(n, entry)):
                gh.comment_file(self.repo, n, cpath)
                label = "triage:accepted" if r.data["verdict"] == "accept" else "triage:rejected"
                self._labels(n, entry, add=(label,))
        verdict_now = (entry.get("verdict") or {})
        if verdict_now.get("verdict") == "reject":
            self._transition(st, n, entry, "rejected")
            if writable(self._mode_now(n, entry)):
                self._labels(n, entry, remove=("triage:pending", "triage:accepted"))
            self._log(n, event="rejected", reasons=verdict_now.get("reasons"))
            return

        run = (st, n, entry, sandbox_dir, issue_dir)

        def ready():
            if self._over_budget(budget):
                self._needs_human(st, n, "超过单 issue 时间预算")
                return False
            return not self._paused_guard(st, n, entry)

        def repair(feedback):
            ok, diff = self._fix_and_gate(*run, feedback=feedback, budget=budget)
            return diff if ok else None

        def rework(feedback):
            count = state_mod.bump_retry(entry, "final_fail")
            self._log(n, event="revision_retry", attempt=count, stage="final")

        try:
            diff = verified_revision(
                repair=repair, review=partial(self._review, *run, issue_text),
                decide=partial(self._decide, *run, issue_text),
                ready=ready, on_rework=rework,
                retries_left=int(self.cfg.get("max_rework_loops", 2))
                             - entry["retries"].get("final_fail", 0))
        except ReworkLimit as exc:
            self._needs_human(st, n, f"终审返工超限: {exc}")
            return
        if diff is None:
            return

        # ⑥ 回复生成（直连·廉价，简单/复杂线共用）
        reply = self._make_reply(st, n, entry, issue_text, diff, budget)
        if reply is None:
            return

        if self._paused_guard(st, n, entry):
            return

        # ⑦ 执行（按档位）
        self._execute(st, n, entry, mode, reply, diff)

    # ---------------- ②③ 修复 + 门禁 ----------------

    def _gate_once(self, st, n, entry, sandbox_dir) -> tuple:
        """共享硬门禁：staging → 无改动/构建失败返回 (False, None, feedback)；通过 (True, diff, "")。"""
        self._transition(st, n, entry, "gate")
        sandbox.stage_all(sandbox_dir)
        if not sandbox.has_changes(sandbox_dir):
            self._log(n, event="gate", ok=False, reason="no_changes")
            return False, None, (
                f"门禁反馈：仓库根（{sandbox_dir}）内没有任何改动。"
                f"上一轮你可能把文件写到了仓库之外——磁盘上有同名/相似文件，"
                f"但它们不是本项目。请把修改写入仓库根 {sandbox_dir} 内的文件"
                f"（如 app.py），然后重新输出 JSON。")
        diff = sandbox.staged_diff(sandbox_dir)
        rc, out = sandbox.run_build(self.build_cmd, sandbox_dir,
                                    env_extra=self.build_env)
        # 指纹在构建【之后】取：构建产物（__pycache__ 等 untracked）被包含进
        # 基线，不会被误判为篡改；此后任何 agent 改动仍能检出
        self._fp1 = sandbox.diff_fingerprint(sandbox_dir)
        if rc == 0:
            entry["review_json"] = {}
            self._archive_review(n, diff, {})
            self._log(n, event="gate", ok=True, stat=sandbox.diff_stat(sandbox_dir))
            return True, diff, ""
        self._log(n, event="gate", ok=False, reason="build_failed", rc=rc)
        return False, None, (f"门禁反馈：构建命令失败。\n"
                             f"{sandbox.build_result_text(rc, out)}\n请修复后重新输出 JSON。")

    def _fix_and_gate(self, st, n, entry, sandbox_dir, issue_dir, feedback, budget,
                      issue_text=None) -> tuple:
        """Both repair routes share the same build gate and retry accounting."""
        stage = "fix_simple" if issue_text is not None else "fix"
        inputs = ({"issue_fulltext": issue_text, "repo": self.repo, "audit_result": entry["audit"]}
                  if issue_text is not None else {"triage_result": entry["verdict"]})
        for attempt in range(1, int(self.cfg.get("max_json_retries", 3)) + 2):
            if self._over_budget(budget):
                self._needs_human(st, n, "超过单 issue 时间预算")
                return False, None
            if self._paused_guard(st, n, entry):
                return False, None
            self._transition(st, n, entry, "fix")
            r = llm.run_stage(stage, sandbox_dir, issue_dir,
                              session_id=entry["session_ids"].get("s1"),
                              inputs={**inputs, "feedback": feedback,
                                      "repo_root": sandbox_dir},
                              cfg=self.cfg, progress=self._progress_cb(n))
            entry["session_ids"]["s1"] = r.session_id
            self._add_cost(entry, r)
            self._log(n, event="fix", attempt=attempt, ok=r.ok, cost=r.cost)
            self._mark(entry, "fix", "done" if r.ok else "failed")
            if not r.ok:
                self._needs_human(st, n, f"修复失败: {'; '.join(r.errors)}")
                return False, None
            entry["summary"] = r.data["summary"]
            entry["fix_json"] = r.data

            ok, diff, gate_feedback = self._gate_once(st, n, entry, sandbox_dir)
            if ok:
                self._mark(entry, "gate", "done")
                return True, diff
            feedback = gate_feedback
            state_mod.bump_retry(entry, "gate")
            self._mark(entry, "gate", "failed")
        self._needs_human(st, n, "门禁重试超限")
        return False, None

    # ---------------- ④ 三路审查 ----------------

    def _archive_review(self, n, diff, reviews, *, errors=None):
        # Readers observe one revision, even if a rework starts with the drawer open.
        atomic_write_json(os.path.join(self._issue_dir(n), "review-snapshot.json"),
                          {"diff": diff[:160000], "truncated": len(diff) > 160000,
                           "reviews": reviews, "errors": errors or {}})

    def _review_inputs(self, issue_text, diff, entry, sandbox_dir) -> dict:
        changed = json.dumps((entry.get("verdict") or {}).get("files_hint", []),
                             ensure_ascii=False)
        return {
            "review_bug": {"issue_fulltext": issue_text, "diff": diff, "changed_files": changed},
            "review_minimal": {"issue_fulltext": issue_text, "diff": diff, "changed_files": changed},
            "review_style": {"diff": diff, "changed_files": changed,
                             "agents_md": self._agents_md(sandbox_dir)},
        }

    def _review(self, st, n, entry, sandbox_dir, issue_dir, issue_text, diff) -> bool:
        self._transition(st, n, entry, "review")
        reviews = llm.run_reviews(list(REVIEW_STAGES), sandbox_dir, issue_dir,
                                  self._review_inputs(issue_text, diff, entry, sandbox_dir),
                                  cfg=self.cfg, progress=self._progress_cb(n))
        for i, s in enumerate(REVIEW_STAGES):
            entry["session_ids"][f"s{i+2}"] = reviews[s].session_id
            self._add_cost(entry, reviews[s])
        self._log(n, event="review", **{s: {"ok": reviews[s].ok,
                                            "pass": (reviews[s].data or {}).get("pass")}
                                        for s in REVIEW_STAGES})
        bad = {s: reviews[s].errors for s in REVIEW_STAGES if not reviews[s].ok}
        self._mark(entry, "review", "failed" if bad else "done")
        entry["review_json"] = {s: reviews[s].data for s in REVIEW_STAGES if reviews[s].ok}
        self._archive_review(n, diff, entry["review_json"], errors=bad)
        if bad:
            self._needs_human(st, n, f"审查失败: {bad}")
            return False
        state_mod.save_issue(st, n, repo=self.repo, worker=True)
        if sandbox.diff_fingerprint(sandbox_dir) != getattr(self, "_fp1", None):
            self._needs_human(st, n, "审查期间工作区 diff 发生变化（防篡改检查），已中止")
            return False
        return True

    # ---------------- ⑤ 终审（⑥ 回复生成见 _make_reply 直连环节） ----------------

    def _decide(self, st, n, entry, sandbox_dir, issue_dir, issue_text, diff):
        self._transition(st, n, entry, "final")
        r5 = llm.run_stage("final", sandbox_dir, issue_dir,
                           session_id=entry["session_ids"].get("s5"),
                           inputs={"issue_fulltext": issue_text, "diff": diff,
                                   "three_reviews": entry.get("review_json", {}),
                                   "build_result": "构建通过 ✅"},
                           cfg=self.cfg, progress=self._progress_cb(n))
        entry["session_ids"]["s5"] = r5.session_id
        self._add_cost(entry, r5)
        self._log(n, event="final", rework=entry["retries"].get("final_fail", 0) + 1, ok=r5.ok,
                  verdict=(r5.data or {}).get("verdict"))
        if r5.ok and r5.data:
            entry["final_json"] = r5.data
        self._mark(entry, "final", "done" if (r5.ok and r5.data and r5.data["verdict"] == "pass") else "failed")
        if not r5.ok:
            self._needs_human(st, n, f"终审失败: {'; '.join(r5.errors)}")
            return None
        if r5.data["verdict"] == "pass":
            # final 是有写权限的 agent：通过后再验一次工作区（终审后无环节兜底）
            if sandbox.diff_fingerprint(sandbox_dir) != getattr(self, "_fp1", None):
                self._needs_human(st, n, "终审期间工作区发生变化（防篡改检查），已中止")
                return None
            return Decision(True)
        return Decision(False, "终审反馈：\n" + "\n".join(r5.data["fail_reasons"]))

    def _mode_now(self, n: int, entry: dict) -> str:
        """重读最新有效档位（三层：issue > 项目 > 全局）。

        运行中切档/降档要能被感知：流水线启动时算的 mode 是快照，
        每个危险边界（暂停/外写）都用本方法取当下值。"""
        d = state_mod._db()
        p = d.get_project(self.repo)
        emode = entry.get("mode", "default")
        if p:
            row = d.get_issue(p["id"], n)
            if row and row.get("mode"):
                emode = row["mode"]
                entry["mode"] = emode   # 同步回内存：防 save_issue 把旧档位写回去
        pm = (p or self.project).get("mode")
        gm = d.kv_get("global_mode", "dry")
        return state_mod.effective_mode({"global_mode": gm}, {"mode": emode}, pm)

    def _paused_guard(self, st, n, entry) -> bool:
        """环节边界暂停检查：档位为 paused → 优雅停止（stage 回 pending 等待恢复）。

        - 不打断运行中的 agent；在环节边界处停下（v3 设计）
        - 重读最新状态：运行中把档位改成 paused 也能感知（含项目级 paused）
        返回 True 表示已暂停（调用方应 return）。
        """
        if self._mode_now(n, entry) != "paused":
            return False
        self._pause(st, n, entry)
        return True

    def _labels(self, n, entry, *, add=(), remove=()):
        for action, labels in ((gh.add_label, add), (gh.remove_label, remove)):
            for label in labels:
                if not writable(self._mode_now(n, entry)):
                    return
                action(self.repo, n, label)

    def _require_write(self, n, entry):
        mode = self._mode_now(n, entry)
        if not writable(mode):
            raise RuntimeError(f"运行期间档位变为 {mode}，已停止外写")

    def _pause(self, st, n, entry):
        from_stage = entry.get("stage")
        entry["paused_from"] = from_stage
        self._transition(st, n, entry, "pending")
        self._log(n, event="paused_at_boundary", from_stage=from_stage)

    # ---------------- ★ needs-info 追问（v3.1：信息不足 → 追问 → 等待 → 自动重审） ----------------

    def _needs_info_comment_file(self, n: int, triage: dict, author: str = "") -> str:
        """组装追问评论：已确认什么（reasons）+ 需要补充什么（questions）+ @作者。"""
        lines = [f"@{author} 感谢反馈。" if author else "感谢反馈。"]
        reasons = [r for r in (triage.get("reasons") or []) if str(r).strip()]
        if reasons:
            lines += ["", "**目前已确认：**"] + [f"- {r}" for r in reasons[:4]]
        lines += ["", "**还需要你补充以下信息，才能继续处理：**"]
        lines += [f"{i}. {q}" for i, q in enumerate(triage.get("questions") or [], 1)]
        lines += ["", "补充回复后，我会自动重新评估这个 issue（无需重新开单）。"]
        path = os.path.join(self._outbox(n), "needs-info.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{self._disclosure()}\n\n" + "\n".join(lines))
        return path

    def _enter_waiting_info(self, st, n: int, entry, mode: str,
                            triage: dict, issue: dict) -> None:
        """triage 判定信息不足：发（dry 则落盘）追问评论，置 waiting_info 等待作者回复。
        追问轮数超过 MAX_INFO_ROUNDS → needs_human。"""
        c = state_mod.bump_retry(entry, "info_rounds")
        if c > MAX_INFO_ROUNDS:
            self._needs_human(st, n, f"追问 {MAX_INFO_ROUNDS} 轮仍无有效进展，需人工介入")
            return
        author = (issue.get("author") or {}).get("login", "")
        self._needs_info_comment_file(n, triage, author)
        entry.setdefault("retries", {})["info_waiting_since"] = time.time()  # TTL 计时起点
        self._mark(entry, "waiting_info", "done")
        if writable(self._mode_now(n, entry)):
            gh.comment_file(self.repo, n,
                            os.path.join(self._outbox(n), "needs-info.md"))
            self._labels(n, entry, add=(NEEDS_INFO_LABEL,))
            entry["info_posted"] = True
            try:  # 基线 = 发出后的评论总数（含本条；等待期 bot 不会再评论）
                fresh = gh.get_issue(self.repo, n)
                entry["info_seen_comments"] = len(fresh.get("comments") or [])
            except (RuntimeError, OSError, TimeoutExpired, json.JSONDecodeError):
                entry["info_seen_comments"] = -1   # 未知基线：watcher 首查补记，不触发
            self._log(n, event="waiting_info", round=c,
                      questions=len(triage.get("questions") or []),
                      seen_comments=entry["info_seen_comments"])
        else:
            entry["info_posted"] = False
            self._log(n, event="waiting_info_dry", round=c,
                      questions=len(triage.get("questions") or []))
        self._transition(st, n, entry, "waiting_info")

    def check_waiting(self) -> list:
        """needs-info watcher（随 poll 周期调用，需在 poll_lock 内）：返回本轮重置为
        pending 的 issue 编号列表（调用方应立即 run_pipeline_once）。

        - waiting_info 未发布 + 非 dry → 切档补发追问（dry 期间的预览转正式）
        - waiting_info 已发布 → 评论数超基线（作者/人工回复）→ 重置 pending（retries 保留）
        - rejected / needs_human 是硬终态：本方法只看 waiting_info，永不自动复活
        """
        d = state_mod._db()
        gmode = d.kv_get("global_mode", "dry")
        if gmode == "paused":
            return []
        p = d.get_project(self.repo)
        if not p:
            return []
        reset = []
        for row in d.list_issues(p["id"]):
            if row.get("stage") != "waiting_info":
                continue
            n = row["number"]
            e = state_mod._row_to_entry(row)
            mode = state_mod.effective_mode({"global_mode": gmode}, e, self.project_mode)
            if mode in ("dry", "paused"):
                continue
            try:
                with repo_read_circuit(self.repo).call():
                    issue = gh.get_issue(self.repo, n)
            except (RuntimeError, OSError, TimeoutExpired, json.JSONDecodeError) as ex:
                self._log(n, event="waiting_check_error", error=str(ex))
                continue
            count = len(issue.get("comments") or [])
            if not e.get("info_posted"):
                if count > 0:
                    # dry 期间已有评论（作者/人工活动）→ 不补发过期追问，直接重审
                    d.upsert_issue(p["id"], n, stage="pending")
                    reset.append(n)
                    self._log(n, event="info_reply_detected_dry", comments=count)
                    continue
                triage = e.get("verdict") or {}
                author = (issue.get("author") or {}).get("login", "")
                try:
                    self._needs_info_comment_file(n, triage, author)
                    if not writable(self._mode_now(n, e)):
                        continue
                    gh.comment_file(self.repo, n,
                                    os.path.join(self._outbox(n), "needs-info.md"))
                    self._labels(n, e, add=(NEEDS_INFO_LABEL,))
                except (RuntimeError, OSError) as ex:
                    self._log(n, event="waiting_repost_error", error=str(ex))
                    continue
                try:  # 基线 = 补发成功后重抓的评论总数（含本条与竞态插入的新评论）
                    fresh = gh.get_issue(self.repo, n)
                    seen = len(fresh.get("comments") or [])
                except (RuntimeError, OSError, TimeoutExpired, json.JSONDecodeError):
                    seen = len(issue.get("comments") or []) + 1
                retries = dict(e.get("retries") or {})
                retries["info_waiting_since"] = time.time()   # 补发重置 TTL 起点
                d.upsert_issue(p["id"], n, info_posted=1, info_seen_comments=seen,
                               retries=retries)
                self._log(n, event="waiting_reposted", seen_comments=seen)
                continue
            seen = e.get("info_seen_comments")
            if seen is None or int(seen) < 0:
                d.upsert_issue(p["id"], n, info_seen_comments=count)  # 首查基线化，不触发
                self._log(n, event="waiting_baselined", seen_comments=count)
                continue
            if count <= int(seen):
                # TTL：追问发出后长时间无回复 → needs_human（不再无限等待；默认 7 天）
                ttl = float(self.cfg.get("waiting_info_ttl_sec", 7 * 24 * 3600)
                            or 7 * 24 * 3600)
                since = float((e.get("retries") or {}).get("info_waiting_since") or 0)
                if since and time.time() - since > ttl:
                    d.upsert_issue(p["id"], n, stage="needs_human")
                    notify("Issue 等待超时",
                           f"#{n} 追问 {ttl / 3600:g}h 无回复，已转人工")
                    self._log(n, event="waiting_info_timeout",
                              waited_h=round((time.time() - since) / 3600, 1))
                continue
            d.upsert_issue(p["id"], n, stage="pending")
            reset.append(n)
            self._log(n, event="info_reply_detected", comments=count, seen=int(seen))
        return reset

    # ---------------- ⑦ 执行 ----------------

    def _record_pr(self, st, n, pr_url, merged: bool = False):
        """写 prs / branches 表（关联查询/Session 管理器用；异常不阻塞主流程）。"""
        if not pr_url:
            return
        try:
            d = state_mod._db()
            p = d.get_project(self.repo)
            if not p:
                return
            entry = st["issues"].get(str(n), {})
            try:
                prnum = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
            except ValueError:
                prnum = None
            irow = d.get_issue(p["id"], n)
            issue_id = irow["id"] if irow else None
            d.add_pr(p["id"], issue_id, prnum, pr_url, entry.get("branch") or "")
            if merged:
                d.set_pr_merged(pr_url)
            if entry.get("branch"):
                d.add_branch(p["id"], issue_id, entry["branch"])
        except Exception:
            pass

    def _execute(self, st, n, entry, mode, reply: str, diff: str = None):
        self._transition(st, n, entry, "execute")
        # 外写前再读当下档位：运行中降档（yolo→dry / →paused）必须能止血
        mode_now = self._mode_now(n, entry)
        if mode_now == "paused":
            self._pause(st, n, entry)
            return
        mode = mode_now
        pr_url = None
        merged = False
        # DRY：把 staged diff 落盘（outbox/fix.patch + sha256）——补发（patch 重放）的前置能力
        if mode == "dry" and diff:
            try:
                import hashlib
                ppath = os.path.join(self._outbox(n), "fix.patch")
                with open(ppath, "w", encoding="utf-8") as f:
                    f.write(diff)
                with open(ppath + ".sha256", "w", encoding="utf-8") as f:
                    f.write(hashlib.sha256(diff.encode()).hexdigest())
                entry["patch_saved"] = True
                self._log(n, event="patch_saved", bytes=len(diff))
            except OSError as e:
                self._log(n, event="patch_save_error", error=str(e))
        if writable(mode):
            body_path = self._pr_body_file(n, entry)
            try:
                sandbox_dir = entry["sandbox_dir"]
                # 提交前最后一次完整性检查：final agent 之后的所有写权限环节已检，
                # 此处兜底（staging 之外的 unstaged/.git 篡改也在指纹覆盖内）
                if sandbox.diff_fingerprint(sandbox_dir) != getattr(self, "_fp1", None):
                    self._needs_human(st, n, "执行前工作区/元数据被改动（防篡改检查），已中止")
                    return
                sandbox.commit(sandbox_dir,
                               f"Fix #{n}: {entry.get('summary', '自动修复')}")
                self._require_write(n, entry)
                sandbox.push_branch(sandbox_dir, entry["branch"])
                self._require_write(n, entry)
                pr_url = gh.create_pr(self.repo, entry["branch"],
                                      f"Fix #{n}: {entry.get('summary', '自动修复')}",
                                      body_path)
                entry["pr_url"] = pr_url
                entry["merged"] = False
                # 先落盘再评论：评论失败也不能丢 PR 记录（否则 merge-fix 找不到 PR）
                state_mod.save_issue(st, n, repo=self.repo, worker=True)
            except (RuntimeError, OSError) as e:
                self._needs_human(st, n, f"PR 创建失败: {e}")
                return
            if mode == "yolo" and self._mode_now(n, entry) == "yolo":
                try:
                    pr_num = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
                    gh.merge_pr(self.repo, pr_num)
                    merged = True
                    entry["merged"] = True
                    state_mod.save_issue(st, n, repo=self.repo, worker=True)
                    self._log(n, event="merged", pr_url=pr_url, method="squash")
                except (RuntimeError, ValueError) as e:
                    # v3：先调 merge agent 修复（冲突/校验失败），成功则继续
                    self._log(n, event="merge_retry_via_agent", error=str(e))
                    fix_r = self.merge_fix(n, automatic=True)
                    if not fix_r.get("ok"):
                        self._needs_human(st, n,
                                          f"自动合并失败（含 merge agent 修复）: {fix_r.get('error')}")
                        return
                    merged = True
                    self._log(n, event="merged_after_fix", steps=fix_r.get("steps"))
        reply_path = self._reply_file(n, reply, pr_url, merged)
        if writable(mode) and writable(self._mode_now(n, entry)):
            try:
                gh.comment_file(self.repo, n, reply_path)
            except (RuntimeError, OSError) as e:
                self._log(n, event="reply_comment_error", error=str(e))
            self._labels(n, entry, add=("triage:done",),
                         remove=("triage:needs-human", NEEDS_INFO_LABEL, "triage:pending", "triage:accepted"))
        entry["merged"] = merged
        self._mark(entry, "execute", "done")
        self._transition(st, n, entry, "done")
        self._record_pr(st, n, pr_url, merged=merged)
        self._log(n, event="done", mode=mode, pr_url=pr_url, merged=merged,
                  cost=entry.get("cost", 0), tokens=entry.get("tokens", 0))

    # ---------------- ★ 补发（v3：DRY 产物以真实档位执行） ----------------

    def publish_preview(self, n: int) -> dict:
        """补发预览：列出 DRY 产物与可行性（供确认弹窗）。"""
        st = state_mod.load_project(self.repo)
        entry = state_mod.issue_entry(st, n)
        mode = state_mod.effective_mode(st, entry, self.project_mode)
        outbox = os.path.join(self._issue_dir(n), "outbox")
        arts = {}
        for fname in ("triage-comment.md", "final-reply.md", "fix.patch", "pr-body.md"):
            arts[fname] = os.path.isfile(os.path.join(outbox, fname))
        return {
            "issue": n,
            "stage": entry.get("stage"),
            "mode": mode,
            "already_published": bool(entry.get("pr_url")),
            "artifacts": arts,
            "can_publish": bool(entry.get("stage") == "done" and not entry.get("pr_url")
                                and writable(mode) and arts["fix.patch"]),
            "hint": ("将执行：分诊评论补发 → patch 重放（门禁复验）→ PR"
                     + ("（YOLO：自动合并）" if mode == "yolo" else "（合并人工）")
                     + " → 回复评论补发"),
        }

    def publish(self, n: int) -> dict:
        """补发执行：把 DRY 产物以当前档位真实发出（patch 重放 + 门禁复验）。

        步骤：①分诊评论 ②patch 重放（clone→apply→门禁→push→PR）③YOLO 合并 ④回复评论。
        返回 {ok, steps[], error?}。仅支持 done 且未发布的 issue。
        """
        st = state_mod.load_project(self.repo)
        entry = state_mod.issue_entry(st, n)
        mode = state_mod.effective_mode(st, entry, self.project_mode)
        steps = []
        if mode in ("dry", "paused"):
            return {"ok": False,
                    "error": f"当前档位为 {mode}——请先切换到 semi/full/yolo 再补发",
                    "steps": steps}
        if entry.get("stage") != "done":
            return {"ok": False, "error": f"仅支持 done 状态的 issue 补发（当前 {entry.get('stage')}）",
                    "steps": steps}
        if entry.get("pr_url"):
            return {"ok": False, "error": "该 issue 已发布（已有 PR），无需补发", "steps": steps}
        outbox = os.path.join(self._issue_dir(n), "outbox")
        patch_path = os.path.join(outbox, "fix.patch")
        if not os.path.isfile(patch_path):
            return {"ok": False,
                    "error": "未找到 fix.patch（该 issue 的 DRY 运行早于补发功能），请直接重跑",
                    "steps": steps}
        # patch 完整性：logs 目录对 agent 可写，补发重放前必须证明未被篡改。
        # sha256 副署缺失同样拒绝——删除副署也是篡改手段
        sha_path = patch_path + ".sha256"
        if not os.path.isfile(sha_path):
            return {"ok": False,
                    "error": "缺少 fix.patch.sha256 完整性副署（可能是旧产物或被删除），请重跑 DRY 生成",
                    "steps": steps}
        import hashlib
        try:
            with open(patch_path, "rb") as f:
                actual = hashlib.sha256(f.read()).hexdigest()
            with open(sha_path, encoding="utf-8") as f:
                expected = f.read().strip()
        except OSError as e:
            return {"ok": False, "error": f"patch 校验读取失败: {e}", "steps": steps}
        if actual != expected:
            self._log(n, event="publish_patch_tampered", expected=expected[:16],
                      actual=actual[:16])
            return {"ok": False,
                    "error": "fix.patch 完整性校验失败（sha256 不匹配，文件可能被篡改）",
                        "steps": steps}

        # ① 分诊评论（若存在）
        triage_path = os.path.join(outbox, "triage-comment.md")
        if os.path.isfile(triage_path):
            try:
                self._require_write(n, entry)
                gh.comment_file(self.repo, n, triage_path)
                self._labels(n, entry, add=("triage:accepted",))
                steps.append("分诊评论已发出")
            except (RuntimeError, OSError) as e:
                return {"ok": False, "error": f"评论补发失败: {e}", "steps": steps}

        # ② patch 重放（stage 始终保持 done：中途崩溃不留 "gate" 僵尸态）
        sandbox_dir = self._sandbox_path(n)
        branch = f"fix/issue-{n}"
        try:
            sandbox.clone(self.repo_url, sandbox_dir, self.main_branch)
            sandbox.create_branch(sandbox_dir, branch)
            sandbox.apply_patch(sandbox_dir, patch_path)
            steps.append("patch 应用成功")
            sandbox.stage_all(sandbox_dir)
            rc, out = sandbox.run_build(self.build_cmd, sandbox_dir,
                                        env_extra=self.build_env)
            if rc != 0:
                return {"ok": False,
                        "error": "门禁复验失败（main 可能已变化导致构建不过）——建议直接重跑",
                        "steps": steps + [sandbox.build_result_text(rc, out)]}
            steps.append("门禁复验通过")
            # 外写前再读当下档位：补发中途降 dry/paused 必须止血
            mid = self._mode_now(n, entry)
            if mid in ("dry", "paused"):
                return {"ok": False,
                        "error": f"运行期间档位变为 {mid}，已停止外写",
                        "steps": steps}
            sandbox.commit(sandbox_dir, f"Fix #{n}: {entry.get('summary', '自动修复')}")
            self._require_write(n, entry)
            sandbox.push_branch(sandbox_dir, branch)
            self._require_write(n, entry)
            body_path = self._pr_body_file(n, entry)
            pr_url = gh.create_pr(self.repo, branch,
                                  f"Fix #{n}: {entry.get('summary', '自动修复')}",
                                  body_path)
            entry["pr_url"] = pr_url
            entry["branch"] = branch
            state_mod.save_issue(st, n, repo=self.repo, worker=True)
            steps.append(f"PR 已创建: {pr_url}")
            # ③ YOLO 自动合并（合并前再看一眼当下档位）
            if self._mode_now(n, entry) == "yolo":
                try:
                    pr_num = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
                    gh.merge_pr(self.repo, pr_num)
                    entry["merged"] = True
                    steps.append("已自动合并")
                except (RuntimeError, ValueError) as e:
                    steps.append(f"自动合并失败（PR 已创建，等待人工）: {e}")
        except (RuntimeError, OSError) as e:
            return {"ok": False, "error": f"patch 重放失败: {e}", "steps": steps}
        finally:
            sandbox.cleanup(sandbox_dir)

        # ④ 回复评论（重写 tail 后发出）
        reply_path = os.path.join(outbox, "final-reply.md")
        if writable(self._mode_now(n, entry)) and os.path.isfile(reply_path):
            try:
                with open(reply_path, encoding="utf-8") as f:
                    reply_text = f.read()
                if entry.get("pr_url") and "🔗" not in reply_text \
                        and "✅ 修复已自动合并" not in reply_text:
                    tail = (f"\n\n---\n✅ 修复已自动合并：{entry['pr_url']}"
                            if entry.get("merged") else f"\n\n---\n🔗 修复 PR：{entry['pr_url']}")
                    reply_text += tail
                    with open(reply_path, "w", encoding="utf-8") as f:
                        f.write(reply_text)
                gh.comment_file(self.repo, n, reply_path)
                steps.append("回复评论已发出")
            except (RuntimeError, OSError) as e:
                steps.append(f"回复评论失败: {e}")

        if writable(self._mode_now(n, entry)):
            self._labels(n, entry, add=("triage:done",),
                         remove=("triage:needs-human", NEEDS_INFO_LABEL, "triage:pending", "triage:accepted"))
        self._transition(st, n, entry, "done")
        self._record_pr(st, n, entry.get("pr_url"), merged=bool(entry.get("merged")))
        self._log(n, event="published", mode=mode, pr_url=entry.get("pr_url"),
                  merged=entry.get("merged"), steps=steps)
        return {"ok": True, "steps": steps}

    # ---------------- ★ merge 修复（v3：合并失败时调用 agent） ----------------

    def merge_fix(self, n: int, *, automatic=False) -> dict:
        """merge 失败修复：fetch 分支 → merge main 触发冲突 → agent 解决 → push → 重试合并。

        返回 {ok, steps[], error?}。供 YOLO 自动触发与手动按钮共用。
        """
        st = state_mod.load_project(self.repo)
        entry = state_mod.issue_entry(st, n)
        pr_url = entry.get("pr_url")
        if not pr_url:
            return {"ok": False, "error": "该 issue 没有 PR，无需 merge 修复", "steps": []}
        # 档位检查（入口）：本函数做真实 push/merge，dry/paused 下禁止一切外写
        mode = self._mode_now(n, entry)
        if not writable(mode) or (automatic and mode != "yolo"):
            return {"ok": False,
                    "error": f"当前档位为 {mode}，merge 修复涉及 push/合并，已拒绝执行",
                    "steps": []}
        branch = entry.get("branch") or f"fix/issue-{n}"
        issue_text = ""
        try:
            issue_text = gh.issue_text(gh.get_issue(self.repo, n))
        except Exception:
            pass
        steps = []
        sandbox_dir = self._sandbox_path(n)
        try:
            sandbox.clone(self.repo_url, sandbox_dir, self.main_branch)
            sandbox.fetch_branch(sandbox_dir, branch)
            sandbox.checkout_tracking(sandbox_dir, branch)
            rc, out = sandbox.merge_branch(sandbox_dir, f"origin/{self.main_branch}")
            if rc == 0:
                steps.append("merge 直接成功（无冲突）")
            else:
                conflicts = sandbox.conflict_files(sandbox_dir)
                steps.append(f"检测到冲突（{len(conflicts)} 个文件），调用 merge agent")
                # 指纹在 agent 调用前一刻取：clone/fetch/checkout/merge 都会写
                # .git（tracking 配置、MERGE_HEAD 等），基线必须含这些合法变化
                meta_fp = sandbox.git_meta_fingerprint(sandbox_dir)
                r = llm.run_stage("merge_fix", sandbox_dir, self._issue_dir(n),
                                  inputs={"issue_fulltext": issue_text,
                                          "repo_root": sandbox_dir,
                                          "conflict_info": "\n".join(conflicts) or "(未知)"},
                                  cfg=self.cfg, progress=self._progress_cb(n))
                entry.setdefault("session_ids", {})["merge_fix"] = r.session_id
                self._add_cost(entry, r)
                self._log(n, event="merge_fix_agent", ok=r.ok,
                          errors=r.errors if not r.ok else None)
                if not r.ok:
                    return {"ok": False,
                            "error": f"merge agent 失败: {'; '.join(r.errors)}",
                            "steps": steps}
                # agent 有写权限：验证它没动 .git 元数据（remote/hooks/config）
                if sandbox.git_meta_fingerprint(sandbox_dir) != meta_fp:
                    return {"ok": False,
                            "error": "merge agent 改动了 .git 元数据（防篡改检查），已中止",
                            "steps": steps}
                mk_rc, mk_out = sandbox.check_merge_markers(sandbox_dir, conflicts)
                if mk_rc != 0:
                    return {"ok": False, "error": "仍有冲突标记残留，需人工处理",
                            "steps": steps + [mk_out[-300:]]}
                sandbox.stage_all(sandbox_dir)
                sandbox.commit(sandbox_dir,
                               f"Merge {self.main_branch} into {branch} (fix #{n})")
                steps.append("agent 已解决冲突并提交")
            # 外写前再看档位：agent 运行期间降 dry/paused → 不 push
            mid = self._mode_now(n, entry)
            if not writable(mid) or (automatic and mid != "yolo"):
                return {"ok": False,
                        "error": f"运行期间档位变为 {mid}，已停止外写",
                        "steps": steps}
            sandbox.push_branch(sandbox_dir, branch)
            steps.append("分支已更新（force-with-lease）")
        except (RuntimeError, OSError) as e:
            return {"ok": False, "error": f"merge 修复失败: {e}", "steps": steps}
        finally:
            sandbox.cleanup(sandbox_dir)
        # 重试合并（合并前最后一次档位确认）
        mid = self._mode_now(n, entry)
        if not writable(mid) or (automatic and mid != "yolo"):
            return {"ok": False,
                    "error": f"运行期间档位变为 {mid}，已停止合并",
                    "steps": steps}
        try:
            pr_num = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
            gh.merge_pr(self.repo, pr_num)
            entry["merged"] = True
            state_mod.save_issue(st, n, repo=self.repo, worker=True)
            steps.append("PR 已合并 ✅")
        except (RuntimeError, ValueError) as e:
            state_mod.save_issue(st, n, repo=self.repo, worker=True)
            return {"ok": False, "error": f"冲突已修复但合并仍失败: {e}", "steps": steps}
        self._record_pr(st, n, pr_url, merged=True)
        self._log(n, event="merge_fixed", pr_url=pr_url, steps=steps)
        return {"ok": True, "steps": steps}

    # ---------------- ★ 初始化 agent（v3：补全 AGENTS.md + 发布方案） ----------------

    def init_project(self, landing: str = "pr") -> dict:
        """生成 AGENTS.md + 发布方案（workflow 优先，Release.md 兜底）。

        landing: main（直推主分支）| pr（普通 PR）| pr_auto（PR+自动合并）。
        返回 {ok, steps[], files[], error?}。
        """
        # 总开关：全局/项目 paused 下不起初始化 agent（零 token 零外写）
        d = state_mod._db()
        if d.kv_get("global_mode") == "paused" or self.project_mode == "paused":
            return {"ok": False, "steps": [],
                    "error": "已暂停（paused）：初始化已拒绝，恢复档位后可重新触发"}
        issue_dir = os.path.join(self.log_dir, "projects",
                                 state_mod.project_slug(self.repo), "init")
        os.makedirs(issue_dir, exist_ok=True)
        sb_dir = os.path.join(
            os.path.expanduser(self.cfg.get("sandbox_root",
                                            "~/.cache/issue-triage-bot/sandbox")),
            f"init-{state_mod.project_slug(self.repo)}-{int(time.time())}")
        steps = []
        try:
            sandbox.clone(self.repo_url, sb_dir, self.main_branch)
            meta_fp = sandbox.git_meta_fingerprint(sb_dir)
            has_agents = os.path.isfile(os.path.join(sb_dir, "AGENTS.md"))
            wf_dir = os.path.join(sb_dir, ".github", "workflows")
            wf_list = os.listdir(wf_dir) if os.path.isdir(wf_dir) else []
            r = llm.run_stage("init_project", sb_dir, issue_dir,
                              inputs={"repo": self.repo, "repo_root": sb_dir,
                                      "has_agents_md": "已存在" if has_agents else "不存在",
                                      "workflow_list": ", ".join(wf_list) or "(无)",
                                      "repo_tree": self._repo_tree(sb_dir)},
                              cfg=self.cfg)
            if not r.ok:
                return {"ok": False,
                        "error": f"初始化 agent 失败: {'; '.join(r.errors)}",
                        "steps": steps}
            # 写权限 agent：确认 .git 元数据未被改动（remote/hooks/config）
            if sandbox.git_meta_fingerprint(sb_dir) != meta_fp:
                return {"ok": False,
                        "error": "初始化 agent 改动了 .git 元数据（防篡改检查），已中止",
                        "steps": steps}
            sandbox.stage_all(sb_dir)
            if not sandbox.has_changes(sb_dir):
                return {"ok": False,
                        "error": "agent 未产生任何文件改动（可能都已存在）",
                        "steps": steps}
            steps.append("生成: " + sandbox.diff_stat(sb_dir).replace("\n", " | ")[:300])
            scheme = (r.data or {}).get("release_scheme", "")
            summary = (r.data or {}).get("summary", "")
            msg = "chore: 初始化 AGENTS.md 与发布方案（issue-triage-bot）"
            if landing == "main":
                sandbox.commit(sb_dir, msg)
                sandbox.push_branch(sb_dir, self.main_branch)
                steps.append(f"已直推 {self.main_branch}")
                return {"ok": True, "landing": "main", "steps": steps,
                        "files": (r.data or {}).get("files", [])}
            branch = f"chore/init-bot-{int(time.time())}"
            sandbox.create_branch(sb_dir, branch)
            sandbox.commit(sb_dir, msg)
            sandbox.push_branch(sb_dir, branch)
            body = os.path.join(issue_dir, "init-pr-body.md")
            with open(body, "w", encoding="utf-8") as f:
                f.write("由**初始化 agent** 生成（AGENTS.md + 发布方案）：\n\n"
                        f"- 发布方案：{scheme}\n- 说明：{summary}\n\n"
                        "审查后合并即可生效。\n")
            pr_url = gh.create_pr(self.repo, branch,
                                  "chore: 初始化 AGENTS.md 与发布方案", body)
            steps.append(f"PR 已创建: {pr_url}")
            if landing == "pr_auto":
                try:
                    pr_num = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
                    gh.merge_pr(self.repo, pr_num)
                    steps.append("PR 已自动合并")
                except (RuntimeError, ValueError) as e:
                    steps.append(f"自动合并失败: {e}")
            return {"ok": True, "landing": landing, "pr_url": pr_url,
                    "steps": steps, "files": (r.data or {}).get("files", [])}
        except (RuntimeError, OSError) as e:
            return {"ok": False, "error": str(e), "steps": steps}
        finally:
            sandbox.cleanup(sb_dir)

    # ---------------- 文案组装 ----------------

    def _triage_comment_file(self, n: int, data: dict) -> str:
        if data["verdict"] == "accept":
            body = (f"### 分诊结论：接受 ✅\n\n"
                    f"**类型**：{data['issue_type']}（置信度 {data['confidence']}%）\n\n"
                    f"**理由**：\n" + "\n".join(f"- {x}" for x in data["reasons"]) +
                    f"\n\n**下一步**：将按以下最小方案修复并提交 PR，供人工审阅合并：\n\n"
                    f"> {data['minimal_plan']}")
        else:
            body = (f"### 分诊结论：暂不采纳\n\n"
                    f"**类型**：{data['issue_type']}（置信度 {data['confidence']}%）\n\n"
                    f"**理由**：\n" + "\n".join(f"- {x}" for x in data["reasons"]) +
                    "\n\n如你有补充信息，欢迎回复说明；维护者会人工复查。")
        path = os.path.join(self._outbox(n), "triage-comment.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{self._disclosure()}\n\n{body}")
        return path

    def _reply_file(self, n: int, reply: str, pr_url, merged: bool = False) -> str:
        tail = ""
        if pr_url:
            tail = (f"\n\n---\n✅ 修复已自动合并：{pr_url}" if merged
                    else f"\n\n---\n🔗 修复 PR：{pr_url}")
        disclosure = self._disclosure()
        head = f"{disclosure}\n\n" if disclosure else ""
        path = os.path.join(self._outbox(n), "final-reply.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{head}{reply[:2400]}{tail}")
        return path

    def _pr_body_file(self, n: int, entry) -> str:
        v = entry.get("verdict") or {}
        lines = [f"Closes #{n}", "", "## 改动摘要", entry.get("summary", ""), "",
                 "## 分诊结论",
                 f"- 类型：{v.get('issue_type')} · 置信度 {v.get('confidence')}%",
                 "- " + "；".join(v.get("reasons", [])[:3]), "",
                 "## 质量声明",
                 "- 硬门禁：构建通过",
                 "- 三路独立审查：bug/最小改动/规范",
                 "- 终审：通过",
                 "- 合并由人工完成"]
        path = os.path.join(self._outbox(n), "pr-body.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path
