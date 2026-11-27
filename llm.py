"""LLM 调度核心：oc-run2 封装、宽松 JSON 解析、schema 校验、纠偏续跑循环。

所有环节的 LLM 交互都走这里：
  run_stage()      —— 单 session（含纠偏续跑 ≤N 次）
  run_reviews()    —— 三路审查独立排队并行，不合格者单独续跑纠偏
输出统一为 LLMResult；原始输出与所用提示词随调用落盘到 issue 档案。
"""
import json
import os
import re
import signal
import subprocess
import threading
import time

import prompts_cfg
from gate import GATE
from util import render, Timer, valid_version_tag

OC_RUN2 = os.path.expanduser("~/.local/bin/oc-run2")
INJECTION_GUARD_KEYS = ("injection_guard", "output_discipline", "max_reply_chars")
# 写权限 stage：无论 prompts/active 如何漂移，注入护栏必须在（代码级兜底，
# 否则用户定制的旧版模板缺 {{injection_guard}} 占位符时防护静默失效）
WRITE_STAGES = ("fix", "fix_simple", "merge_fix", "release_run", "release_fix",
                "init_project")


def _oc_run2_bin(cfg: dict) -> str:
    """oc-run2 可执行文件路径；config.oc_run2_path 可指向非保护目录副本
    （launchd 进程执行 ~/Documents 下的脚本会被 macOS TCC 静默挂起）。"""
    p = (cfg.get("oc_run2_path") or "").strip()
    return os.path.expanduser(p) if p else OC_RUN2


class LLMResult:
    def __init__(self):
        self.ok = False
        self.data = None            # 校验通过的 JSON dict
        self.errors = []            # 最终失败原因（中文）
        self.session_id = None      # 该 stage 最终绑定的 session（续跑复用）
        self.attempts = 0
        self.cost = 0.0
        self.tokens = 0
        self.raws = []              # 每次尝试的原始输出
        self.elapsed = 0.0

    def to_dict(self):
        return {"ok": self.ok, "data": self.data, "errors": self.errors,
                "session_id": self.session_id, "attempts": self.attempts,
                "cost": self.cost, "tokens": self.tokens, "raws": self.raws}


# ---------------- 提示词组装 ----------------

def assemble_prompt(stage: str, prompts: dict, inputs: dict) -> tuple:
    """返回 (system, user)。system 渲染共享组件占位；user 渲染输入块。"""
    stg = prompts["stages"][stage]
    shared = prompts["shared"]
    sys_vars = {k: shared.get(k, "") for k in INJECTION_GUARD_KEYS if isinstance(shared.get(k, ""), str)}
    sys_vars["max_reply_chars"] = str(shared.get("max_reply_chars", 1500))
    system = render(stg["system"], sys_vars)
    # 写权限 stage 护栏兜底：模板缺占位符（active 漂移/旧定制版）时显式前置
    guard = shared.get("injection_guard") or ""
    if stage in WRITE_STAGES and guard and guard not in system:
        system = f"{guard}\n\n{system}"
    # issue 正文是不可信外部输入：`== user_issue ==` 标题可在正文内伪造，
    # 因此包显式结构边界（不依赖 active 模板是否跟上改动）
    safe_inputs = dict(inputs or {})
    text = safe_inputs.get("issue_fulltext")
    if isinstance(text, str) and text and "<issue_data>" not in text:
        safe_inputs["issue_fulltext"] = f"<issue_data>\n{text}\n</issue_data>"
    user = render(stg["user"], safe_inputs)
    return system, user


def format_spec(stage: str, prompts: dict) -> str:
    """纠偏模板里的 {format_spec}：system 中"输出 JSON 的格式"以后的段落。"""
    stg = prompts["stages"][stage]
    m = re.search(r"(输出 JSON 的格式.*)$", stg["system"], re.S)
    return m.group(1).strip() if m else stg["system"]


# ---------------- JSON 解析与校验 ----------------

def parse_json(text: str):
    """宽松解析：剥围栏 → 直接 loads → 截取 { 到 } 再 loads。失败返回 None。"""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    for candidate in (t, _brace_slice(t)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _brace_slice(t: str):
    i, j = t.find("{"), t.rfind("}")
    return t[i:j + 1] if 0 <= i < j else None


def _req(data, key, types, err):
    if key not in data:
        err.append(f"缺少必填字段「{key}」")
        return None
    v = data[key]
    if not isinstance(v, types):
        err.append(f"字段「{key}」类型应为 {types}，实际为 {type(v).__name__}")
    return v


def _enum(v, allowed, key, err):
    if v is None:
        return v   # 缺失/None 已由 _req 报告，不重复报错
    if not isinstance(v, str) or v not in allowed:
        err.append(f"字段「{key}」必须为 {'/'.join(allowed)} 之一，实际为「{v}」")
    return v


def validate_stage(stage: str, data) -> list:
    """schema 校验，返回中文错误列表（空 = 通过）。"""
    err = []
    if not isinstance(data, dict):
        return ["输出不是 JSON 对象"]

    if stage == "triage":
        _enum(_req(data, "verdict", str, err), ("accept", "reject", "need_info"), "verdict", err)
        _enum(_req(data, "issue_type", str, err),
              ("bug", "feature", "question", "duplicate", "invalid"), "issue_type", err)
        _req(data, "reasons", list, err)
        questions = data.get("questions")
        if questions is None:
            questions = []   # 可选字段：accept/reject 可省略
        if not isinstance(questions, list):
            err.append("字段「questions」类型应为 list")
            questions = None
        if isinstance(questions, list):
            for i, q in enumerate(questions):
                if not isinstance(q, str) or not q.strip():
                    err.append(f"questions[{i}] 应为非空字符串")
            if data.get("verdict") == "need_info" and not questions:
                err.append("verdict=need_info 时 questions 不能为空（给出 1-3 个具体问题）")
            if data.get("verdict") in ("accept", "reject") and questions:
                err.append("verdict 为 accept/reject 时 questions 应为空数组 []")
        _req(data, "minimal_plan", str, err)
        _req(data, "files_hint", list, err)
        c = _req(data, "confidence", int, err)
        if isinstance(c, bool):
            err.append("字段「confidence」类型应为 int，实际为 bool")
        elif type(c) is int and not (0 <= c <= 100):
            err.append("字段「confidence」应在 0-100")

    elif stage in ("fix", "fix_simple"):
        _req(data, "changed_files", list, err)
        _req(data, "summary", str, err)
        _req(data, "self_check", str, err)

    elif stage.startswith("review_"):
        p = _req(data, "pass", bool, err)
        issues = _req(data, "issues", list, err)
        if isinstance(issues, list):
            for i, it in enumerate(issues):
                if not isinstance(it, dict):
                    err.append(f"issues[{i}] 应为对象")
                    continue
                _enum(it.get("severity"), ("blocker", "major", "minor"), f"issues[{i}].severity", err)
                if not isinstance(it.get("desc"), str):
                    err.append(f"issues[{i}].desc 应为字符串")
        _req(data, "notes", str, err)

    elif stage == "final":
        v = _enum(_req(data, "verdict", str, err), ("pass", "fail"), "verdict", err)
        _req(data, "fail_reasons", list, err)
        # 注：回复已改由直连 reply 环节生成；此处不再要求 reply_to_author/suggested_label（兼容旧格式，可选）

    elif stage == "reply_review":
        _req(data, "pass", bool, err)
        _req(data, "feedback", list, err)
        _req(data, "revised_reply", str, err)

    elif stage == "merge_fix":
        _req(data, "resolved", bool, err)
        _req(data, "summary", str, err)
        _req(data, "files", list, err)

    elif stage == "release_run":
        v = _req(data, "version", str, err)
        if isinstance(v, str) and not valid_version_tag(v):
            err.append(f"字段「version」不是合法版本号/tag: {v[:60]!r}")
        _req(data, "summary", str, err)
        _req(data, "files", list, err)

    elif stage == "release_fix":
        _req(data, "fixed", bool, err)
        _req(data, "summary", str, err)
        _req(data, "files", list, err)

    elif stage == "init_project":
        _enum(_req(data, "agents_md", str, err), ("created", "skipped"),
              "agents_md", err)
        _enum(_req(data, "release_scheme", str, err), ("workflow", "release_md"),
              "release_scheme", err)
        _req(data, "files", list, err)
        _req(data, "summary", str, err)

    return err


# ---------------- oc-run2 调用 ----------------

def _oc_run2_base(cfg: dict) -> list:
    cmd = [_oc_run2_bin(cfg), "--json", "--model", cfg["model"]]
    variant = (cfg.get("oc_variant") or "").strip()
    if variant:
        cmd += ["--variant", variant]  # 推理强度（low/high/max）——实测 low 任务提速 ~12 倍
    return cmd


def _run_single(prompt: str, sandbox_dir: str, session_id, timeout: int, cfg: dict):
    """单任务调用（oc-run2 路径）；agent 并发闸门包装。"""
    with GATE.slot(float(cfg.get("agent_queue_timeout_sec", 300))):
        return _run_single_locked(prompt, sandbox_dir, session_id, timeout, cfg)


def _proc_wait(proc, timeout: float) -> bool:
    """等待进程退出；Mock/已回收对象视为已终结。"""
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False
    except (AttributeError, OSError):
        return True


def _kill_proc_group(proc, grace: float = 10) -> None:
    """TERM → 等待 grace 秒 → KILL 升级。保证进程组必死（不退则闸门额度泄漏）。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    if _proc_wait(proc, grace):
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    _proc_wait(proc, 5)


def _run_single_locked(prompt: str, sandbox_dir: str, session_id, timeout: int, cfg: dict):
    """单任务调用；返回 oc-run2 --json 的 tasks[0]。
    用 Popen + 独立进程组：超时强杀整组并回报末尾输出（诊断卡点）。"""
    cmd = _oc_run2_base(cfg) + ["--dir", sandbox_dir, "--timeout", str(timeout)]
    if session_id:
        cmd += ["--session", session_id]
    cmd += ["--prompt", prompt]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout + 90)
    except subprocess.TimeoutExpired:
        _kill_proc_group(proc)   # TERM→KILL：communicate 必须带超时，否则占死 GATE
        try:
            stdout, stderr = proc.communicate(timeout=5)   # 子进程可能仍持有输出管道
        except (subprocess.TimeoutExpired, OSError):
            stdout, stderr = "", ""
        tail = (((stderr or "") + (stdout or "")) or "无任何输出").strip()[-400:]
        raise RuntimeError(f"oc-run2 运行超时（{timeout + 90}s 强杀）；末尾输出: {tail}")
    out = None
    try:
        out = json.loads(stdout) if stdout.strip().startswith("{") else None
    except json.JSONDecodeError:
        out = None
    if not out or not out.get("tasks"):
        raise RuntimeError(f"oc-run2 输出异常: {(stderr or stdout)[-300:]}")
    return out["tasks"][0]


def _run_tasks(tasks: list, timeout: int, cfg: dict):
    """Give each independent review one bounded lease, even at limit one."""
    from concurrent.futures import ThreadPoolExecutor

    def run(task):
        try:
            result = _run_single(task["prompt"], task["dir"], None, timeout, cfg)
        except (subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
            result = {"status": "error", "stderr_tail": str(exc)}
        return {**result, "title": task["title"]}

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(tasks)))) as pool:
        return list(pool.map(run, tasks))


# ---------------- 直连 opencode2 调用（use_direct_oc2=true 时替代 oc-run2） ----------------
# 设计动机（2026-09-10）：oc-run2 是"跑完才出结果"的黑盒——超时/卡死时拿不到进度、
# 无法判断 session 是否还活着。直连 opencode2 run 的 --format json 是**流式 JSONL 事件流**
# （step_start / tool_use / text / step_finish），可以：实时落盘进度、检测"静默卡死"、
# 经官方 API 权威查询 session 状态。事件解析口径与 oc-run2 对齐，保证 tokens/cost/final 一致。

OC2_BIN = os.path.expanduser("~/.opencode/bin/opencode2")


def _model_with_variant(cfg: dict) -> str:
    m = cfg["model"]
    v = (cfg.get("oc_variant") or "").strip()
    return f"{m}#{v}" if v else m


def parse_events(text: str) -> list:
    """解析 opencode2 run --format json 输出的 JSONL 事件流，忽略非 JSON 行。"""
    events = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _summarize_events(title: str, dirp: str, events: list, returncode) -> dict:
    """从事件流提取汇总（与 oc-run2 summarize_stream 口径一致）。"""
    session_id = None
    actions = {}
    text_parts = []
    tokens_total = 0
    cost_total = 0.0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("sessionID"):
            session_id = session_id or ev["sessionID"]
        etype = ev.get("type")
        part = ev.get("part")
        if not isinstance(part, dict):
            part = {}
        if etype == "tool_use":
            tool = part.get("tool") or "?"
            actions[tool] = actions.get(tool, 0) + 1
        elif etype == "text":
            if part.get("text"):
                text_parts.append(part["text"])
        elif etype == "step_finish":
            tk = part.get("tokens") or {}
            tokens_total += sum(tk.get(k, 0) or 0 for k in ("input", "output", "reasoning"))
            try:
                cost_total += part.get("cost") or 0.0
            except (TypeError, ValueError):
                pass
    final_text = (text_parts[-1] if text_parts else "").strip()
    status = "ok" if (returncode == 0 and session_id) else "failed"
    return {"title": title, "dir": dirp, "status": status, "session_id": session_id,
            "actions": actions, "total_actions": sum(actions.values()),
            "final_result": final_text, "tokens": tokens_total,
            "cost": round(cost_total, 6), "exit_code": returncode, "stderr_tail": ""}


def session_stats(session_id: str):
    """经官方 API 查询 session 的权威 tokens/cost/time；失败返回 None。
    亦可用于诊断"某个 session 是否还在运行"（time 字段含 created/updated/idle）。"""
    if not session_id:
        return None
    try:
        p = subprocess.run([OC2_BIN, "api", "get", f"/api/session/{session_id}"],
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            return None
        data = json.loads(p.stdout)
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        if not isinstance(data, dict) or data.get("_tag"):
            return None
        out = {}
        tk = data.get("tokens") or {}
        total = sum(tk.get(k, 0) or 0 for k in ("input", "output", "reasoning"))
        if total:
            out["tokens"] = total
        if data.get("cost") is not None:
            out["cost"] = round(float(data["cost"]), 6)
        if data.get("time"):
            out["time"] = data["time"]
        return out or None
    except Exception:
        return None


def _exec_opencode2(prompt: str, sandbox_dir: str, session_id, title: str,
                    cfg: dict, progress=None) -> dict:
    """直连 opencode2（agent 并发闸门包装：超出上限则排队等待）。"""
    with GATE.slot(float(cfg.get("agent_queue_timeout_sec", 300))):
        return _exec_opencode2_locked(prompt, sandbox_dir, session_id, title, cfg, progress)


def _exec_opencode2_locked(prompt: str, sandbox_dir: str, session_id, title: str,
                           cfg: dict, progress=None) -> dict:
    """直连 opencode2 run：流式读事件流 + 双超时（墙钟/静默）+ 实时进度回调。

    - progress(ev)：每个事件回调一次（调用方用于落盘进度；异常被忽略）
    - 超时 → RuntimeError（附现场：事件数/最后事件/静默时长，可诊断）
    - 返回 oc-run2 兼容的 task dict
    """
    wall = int(cfg.get("agent_timeout_sec", 7200))
    idle = int(cfg.get("agent_idle_timeout_sec", 1200))
    cmd = [OC2_BIN, "run", "--format", "json", "--auto"]
    if session_id:
        cmd += ["--session", session_id]
    else:
        cmd += ["--title", title]
    cmd += ["--model", _model_with_variant(cfg), "--", prompt]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace", start_new_session=True,
                            cwd=sandbox_dir)
    events = []
    state = {"last_ts": time.time(), "count": 0, "last_type": "", "last_tool": ""}
    lock = threading.Lock()

    def _reader():
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                with lock:
                    events.append(ev)
                    state["last_ts"] = time.time()
                    state["count"] += 1
                    state["last_type"] = ev.get("type") or ""
                    if state["last_type"] == "tool_use":
                        part = ev.get("part") or {}
                        state["last_tool"] = part.get("tool") or ""
                if progress:
                    try:
                        progress(ev)
                    except Exception:
                        pass
        except (ValueError, OSError):
            pass

    th = threading.Thread(target=_reader, daemon=True)
    th.start()

    # stderr 同步排空：PIPE 不读会在 >64KB 时把子进程堵死（表现为静默假死占满 idle 超时）
    err_lines = []

    def _err_reader():
        try:
            for line in proc.stderr:
                err_lines.append(line)
                if len(err_lines) > 200:      # 只留尾部，防刷屏撑爆内存
                    del err_lines[:-50]
        except (ValueError, OSError):
            pass

    th_err = threading.Thread(target=_err_reader, daemon=True)
    th_err.start()

    t0 = time.time()
    timed_out = None
    while True:
        if proc.poll() is not None:
            break
        now = time.time()
        with lock:
            last, cnt = state["last_ts"], state["count"]
        if now - t0 > wall:
            timed_out = "wall"
            break
        if now - last > idle:
            timed_out = "idle"
            break
        time.sleep(1)

    if timed_out:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    th.join(timeout=5)
    th_err.join(timeout=5)
    with lock:
        snap = dict(state)

    stderr_tail = "".join(err_lines).strip()[-400:]

    if timed_out:
        kind = "墙钟超时" if timed_out == "wall" else "静默卡死（无新事件）"
        raise RuntimeError(
            f"opencode2 {kind}：运行 {int(time.time() - t0)}s、事件 {snap['count']} 个、"
            f"最后事件 {snap['last_type'] or '(无)'}"
            f"{'·' + snap['last_tool'] if snap['last_tool'] else ''}、"
            f"距上次活动 {int(time.time() - snap['last_ts'])}s")

    rc = proc.returncode
    if not events and rc != 0:
        raise RuntimeError(f"opencode2 退出码 {rc} 且无事件流输出: {stderr_tail[-200:]}")
    summary = _summarize_events(title, sandbox_dir, events, rc)
    summary["stderr_tail"] = stderr_tail
    stats = session_stats(summary["session_id"])
    if stats:
        summary.update({k: v for k, v in stats.items() if k in ("tokens", "cost")})
    return summary


def _run_tasks_direct(tasks: list, cfg: dict, progress=None) -> list:
    """并行直连 opencode2（每任务独立进程/独立 session）。"""
    from concurrent.futures import ThreadPoolExecutor

    results = [None] * len(tasks)

    def one(i, t):
        ctx = t.get("title") or os.path.basename(t["dir"])
        prog = (lambda ev: progress(ctx, ev)) if progress else None
        try:
            results[i] = _exec_opencode2(t["prompt"], t["dir"], None,
                                         title=ctx, cfg=cfg, progress=prog)
        except Exception as e:
            results[i] = {"title": ctx, "dir": t.get("dir"), "status": "error",
                          "session_id": None, "actions": {}, "total_actions": 0,
                          "final_result": f"直连调用异常: {e}", "tokens": 0,
                          "cost": 0.0, "exit_code": None, "stderr_tail": ""}

    with ThreadPoolExecutor(max_workers=min(max(len(tasks), 1), 6)) as ex:
        list(ex.map(lambda a: one(*a), list(enumerate(tasks))))
    return results


# ---------------- opencode2 session 清理 ----------------

def delete_session(session_id: str) -> bool:
    """从 opencode2 后台服务删除 session（对 404 视为已删除）。"""
    if not session_id:
        return False
    try:
        p = subprocess.run([OC2_BIN, "api", "delete", f"/api/session/{session_id}"],
                           capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            return False
        out = p.stdout or ""
        return "SessionNotFoundError" in out or "deleted" in out.lower() or '"_tag"' not in out
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------- 主入口：单 session（含纠偏） ----------------

def _run_one_stage(prompt: str, stage: str, sandbox_dir: str, session_id,
                   timeout: int, cfg: dict, progress=None) -> dict:
    """按配置选择调用通道：直连 opencode2（默认）或 oc-run2。两者返回兼容 dict。"""
    if cfg.get("use_direct_oc2", True):
        ctx = stage
        prog = (lambda ev: progress(ctx, ev)) if progress else None
        return _exec_opencode2(prompt, sandbox_dir, session_id,
                               title=f"{os.path.basename(sandbox_dir)}-{stage}",
                               cfg=cfg, progress=prog)
    return _run_single(prompt, sandbox_dir, session_id, timeout, cfg)


def _task_failure(task):
    if not task:
        return "审查进程未返回结果"
    if task.get("status") in ("error", "failed") or task.get("exit_code") not in (0, None):
        detail = task.get("stderr_tail") or task.get("final_result") or task.get("exit_code")
        return f"agent 执行失败: {detail}"
    return None


def run_stage(stage: str, sandbox_dir: str, issue_dir: str, session_id=None,
              inputs: dict = None, cfg: dict = None, timeout: int = None,
              progress=None) -> LLMResult:
    """执行一个 stage：首发 → 解析/校验 → 不合格则用纠偏模板续跑（≤max_json_retries）。
    progress(context, ev)：可选事件回调（直连模式下实时进度）。"""
    res = LLMResult()
    cfg = cfg or {}
    timeout = int(timeout or cfg.get("agent_timeout_sec", 7200))
    prompts = prompts_cfg.load()
    shared = prompts["shared"]
    max_retries = int(cfg.get("max_json_retries", 3))
    os.makedirs(issue_dir, exist_ok=True)

    system, user = assemble_prompt(stage, prompts, inputs or {})
    fmt = format_spec(stage, prompts)
    timer = Timer()
    prompt = f"{system}\n\n{user}"

    for attempt in range(1, max_retries + 2):  # 首发 1 + 纠偏 N
        res.attempts = attempt
        try:
            task = _run_one_stage(prompt, stage, sandbox_dir, session_id,
                                  timeout, cfg, progress)
        except (subprocess.TimeoutExpired, RuntimeError, OSError) as e:
            res.errors = [f"LLM 调用失败: {e}"]
            break
        raw = (task.get("final_result") or "").strip()
        session_id = task.get("session_id") or session_id
        res.session_id = session_id
        res.cost += task.get("cost") or 0
        res.tokens += task.get("tokens") or 0
        res.raws.append(raw)
        with open(os.path.join(issue_dir, f"llm-{stage}-{attempt}.txt"), "w", encoding="utf-8") as f:
            f.write(raw or "(空输出)")

        failure = _task_failure(task)
        if failure:
            res.errors = [failure]
            break
        data = parse_json(raw)
        errors = validate_stage(stage, data) if data is not None else ["输出不是合法的 JSON 对象"]
        if not errors:
            res.ok, res.data, res.errors = True, data, []
            break
        res.errors = errors
        # 组装纠偏 prompt（续跑同一 session）
        prompt = render(shared.get("correction_template", ""), {
            "last_output": raw[:4000], "errors": "\n".join(f"- {e}" for e in errors),
            "format_spec": fmt,
        })

    res.elapsed = timer.elapsed()
    return res


# ---------------- 三路审查并行 ----------------

def run_reviews(review_stages: list, sandbox_dir: str, issue_dir: str,
                inputs_by_stage: dict, cfg: dict, timeout: int = None,
                progress=None) -> dict:
    """并行派发三个审查 stage，再对不合格者逐个续跑纠偏。
    progress(context, ev)：可选事件回调（直连模式下实时进度）。返回 {stage: LLMResult}。"""
    cfg = cfg or {}
    timeout = int(timeout or cfg.get("agent_timeout_sec", 7200))
    prompts = prompts_cfg.load()
    shared = prompts["shared"]
    fmt_map = {s: format_spec(s, prompts) for s in review_stages}
    max_retries = int(cfg.get("max_json_retries", 3))
    os.makedirs(issue_dir, exist_ok=True)

    tasks = []
    for s in review_stages:
        system, user = assemble_prompt(s, prompts, inputs_by_stage.get(s, {}))
        tasks.append({"dir": sandbox_dir, "title": f"review-{s}",
                      "prompt": f"{system}\n\n{user}"})

    results = {s: LLMResult() for s in review_stages}
    timer = Timer()
    try:
        if cfg.get("use_direct_oc2", True):
            out = _run_tasks_direct(tasks, cfg, progress=progress)
        else:
            out = _run_tasks(tasks, timeout, cfg)
    except (subprocess.TimeoutExpired, RuntimeError, OSError) as e:
        for s in review_stages:
            results[s].errors = [f"LLM 并行调用失败: {e}"]
        return results

    by_title = {t.get("title"): t for t in out}
    first_pass = []  # 需要纠偏的 (stage, raw, errors)
    for s in review_stages:
        r = results[s]
        task = by_title.get(f"review-{s}", {})
        raw = (task.get("final_result") or "").strip()
        r.session_id = task.get("session_id")
        r.cost += task.get("cost") or 0
        r.tokens += task.get("tokens") or 0
        r.raws.append(raw)
        r.attempts = 1
        with open(os.path.join(issue_dir, f"llm-{s}-1.txt"), "w", encoding="utf-8") as f:
            f.write(raw or "(空输出)")
        failure = _task_failure(task)
        if failure:
            r.errors = [failure]
            continue
        data = parse_json(raw)
        errors = validate_stage(s, data) if data is not None else ["输出不是合法的 JSON 对象"]
        if not errors:
            r.ok, r.data, r.errors = True, data, []
        else:
            r.errors = errors
            first_pass.append((s, raw, errors))

    # 不合格者逐个续跑纠偏（通常 0~1 个）
    for s, raw, errors in first_pass:
        r = results[s]
        for attempt in range(2, max_retries + 2):
            r.attempts = attempt
            prompt = render(shared.get("correction_template", ""), {
                "last_output": raw[:4000], "errors": "\n".join(f"- {e}" for e in errors),
                "format_spec": fmt_map[s],
            })
            try:
                if cfg.get("use_direct_oc2", True):
                    prog = (lambda ev, _s=s: progress(_s, ev)) if progress else None
                    task = _exec_opencode2(prompt, sandbox_dir, r.session_id,
                                           title=f"{os.path.basename(sandbox_dir)}-{s}-retry",
                                           cfg=cfg, progress=prog)
                else:
                    task = _run_single(prompt, sandbox_dir, r.session_id, timeout, cfg)
            except (subprocess.TimeoutExpired, RuntimeError, OSError) as e:
                r.errors = [f"LLM 调用失败: {e}"]
                break
            raw = (task.get("final_result") or "").strip()
            r.session_id = task.get("session_id") or r.session_id
            r.cost += task.get("cost") or 0
            r.tokens += task.get("tokens") or 0
            r.raws.append(raw)
            with open(os.path.join(issue_dir, f"llm-{s}-{attempt}.txt"), "w", encoding="utf-8") as f:
                f.write(raw or "(空输出)")
            failure = _task_failure(task)
            if failure:
                r.errors = [failure]
                break
            data = parse_json(raw)
            errors = validate_stage(s, data) if data is not None else ["输出不是合法的 JSON 对象"]
            if not errors:
                r.ok, r.data, r.errors = True, data, []
                break
            r.errors = errors

    for r in results.values():
        r.elapsed = timer.elapsed()
    return results
