"""Release agent：按条件触发发布（workflow 优先；无则按 Release.md 执行）。

触发条件（release_cfg，独立开关，OR 关系；无启用项则永不自动触发）：
  {time_enabled, interval_hours=24, count_enabled, count_threshold=5,
   mode: "follow"|"dry"|"semi"|"full"|"yolo"}
- 时间：距上次发布 >= interval_hours 且有新 commit（比对 last_sha）
- 数量：自上次发布以来 merged 的 PR 数 >= count_threshold
- 发布成功：last_ts / last_sha / 计数 全部重置

执行（release 档位；follow=跟随项目/全局档位）：
- dry：生成发布计划落盘，不动作
- semi/full：workflow 直跑；Release.md 路径 → 准备 release PR（tag 人工）
- yolo：workflow 直跑；Release.md 路径 → 全自动（commit+push main+tag+gh release）

workflow 优先；workflow 运行失败 → agent 修错 → 重试 ≤2 → 超限 needs_human（通知）。
"""
import json
import os
import subprocess
import time

import github as gh
import llm
import sandbox
import state as state_mod
from util import log_event, notify, valid_version_tag

DEFAULT_CFG = {"time_enabled": False, "interval_hours": 24,
               "count_enabled": False, "count_threshold": 5,
               "mode": "follow"}
WF_KEYS = ("release", "publish", "tag", "deploy", "发布")
MAX_FIX_TRIES = 2


def get_release_cfg(project: dict) -> dict:
    cfg = dict(DEFAULT_CFG)
    cfg.update(project.get("release_cfg") or {})
    return cfg


def _state_key(project: dict) -> str:
    return f"release_state::{project.get('repo', '')}"


def get_release_state(db, project: dict) -> dict:
    return db.kv_get(_state_key(project), {}) or {}


def set_release_state(db, project: dict, st: dict) -> None:
    db.kv_set(_state_key(project), st)


def _merged_since(db, project: dict, st: dict) -> int:
    """自上次发布以来已合并的 PR 数（prs.merged_at > last_ts）。"""
    last = float(st.get("last_ts") or 0)
    n = 0
    pid = project.get("id")
    if pid is None:
        return 0
    for pr in db.list_prs(pid):
        if pr.get("state") != "merged" or not pr.get("merged_at"):
            continue
        try:
            ts = time.mktime(time.strptime(pr["merged_at"], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        if ts > last:
            n += 1
    return n


def check_due(db, project: dict) -> dict:
    """评估触发条件。返回 {due, reasons[], cfg, state, merged_since}"""
    rc = get_release_cfg(project)
    st = get_release_state(db, project)
    reasons = []
    if rc.get("time_enabled"):
        last = float(st.get("last_ts") or 0)
        if time.time() - last >= float(rc.get("interval_hours", 24)) * 3600:
            sha = None
            try:
                sha = gh.latest_commit_sha(project["repo"],
                                           project.get("main_branch", "main"))
                if st.pop("check_error", None) is not None:
                    set_release_state(db, project, st)   # 查询恢复：清掉旧错误留痕
            except Exception as e:
                # gh 查询失败（sha=None）不触发：把网络抖动当"有新提交"会误发布；
                # 无法证明有变化时按未到期处理，下一轮再评估。失败要留痕便于排查。
                st["check_error"] = f"latest_commit_sha 查询失败: {str(e)[:200]}"
                set_release_state(db, project, st)
            if sha and sha != st.get("last_sha"):
                reasons.append(f"时间条件（≥{rc.get('interval_hours', 24)}h 且有新提交）")
    merged_since = _merged_since(db, project, st)
    if rc.get("count_enabled"):
        if merged_since >= int(rc.get("count_threshold", 5)):
            reasons.append(f"数量条件（{merged_since} ≥ {rc.get('count_threshold', 5)} 个已合并）")
    return {"due": bool(reasons), "reasons": reasons, "cfg": rc, "state": st,
            "merged_since": merged_since}


def resolve_mode(db, project: dict, rc: dict) -> str:
    m = rc.get("mode") or "follow"
    if m != "follow":
        return m
    m = project.get("mode") or "default"
    if m in state_mod.MODES:
        return m
    return db.kv_get("global_mode", "dry")


def list_release_workflows(repo: str) -> list:
    """检测疑似发布 workflow（名称/路径关键词匹配）。"""
    try:
        p = subprocess.run(["gh", "workflow", "list", "--repo", repo,
                            "--json", "name,path"],
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            return []
        wfs = json.loads(p.stdout or "[]")
        out = []
        for w in wfs:
            blob = ((w.get("name") or "") + " " + (w.get("path") or "")).lower()
            if any(k in blob for k in WF_KEYS):
                out.append(w)
        return out
    except Exception:
        return []


def run_workflow(repo: str, wf: dict) -> None:
    name = wf.get("name") or wf.get("path") or ""
    # name 进 argv 位置：- 开头会被 gh 当成 flag（选项注入防护）
    if not name or name.startswith("-") or "\x00" in name:
        raise RuntimeError(f"非法 workflow 名: {name[:60]!r}")
    p = subprocess.run(["gh", "workflow", "run", name, "--repo", repo],
                       capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        raise RuntimeError(f"gh workflow run 失败: {(p.stderr or p.stdout)[-200:]}")


def _latest_run(repo: str, wf: dict):
    try:
        p = subprocess.run(
            ["gh", "run", "list", "--repo", repo, "--workflow", wf.get("name") or "",
             "--limit", "1", "--json", "databaseId,status,conclusion,url,createdAt"],
            capture_output=True, text=True, timeout=60)
        runs = json.loads(p.stdout or "[]")
        return runs[0] if runs else None
    except Exception:
        return None


def _iso_ts(s: str) -> float:
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def trigger_and_wait(repo: str, wf: dict, timeout_sec: int = 1800) -> tuple:
    """触发 workflow 并等待【新的】run 完成（修错重试场景也正确）。
    返回 (ok, detail)。"""
    prev = _latest_run(repo, wf)
    prev_id = (prev or {}).get("databaseId")
    start_ts = time.time()
    run_workflow(repo, wf)
    deadline = start_ts + timeout_sec
    while time.time() < deadline:
        time.sleep(15)
        cur = _latest_run(repo, wf)
        if not cur or cur.get("databaseId") == prev_id:
            continue
        # 只认本次触发之后创建的 run：他人此前手动触发的 run 不误认为我们的
        if _iso_ts(cur.get("createdAt") or "") < start_ts - 30:
            continue
        if cur.get("status") == "completed":
            ok = cur.get("conclusion") == "success"
            return ok, f"{cur.get('conclusion')} {cur.get('url', '')}"
    return False, "等待超时"


# ---------------- 执行 ----------------

def _progress(db, project: dict, steps: list) -> None:
    """把最近步骤快照写入 kv（Release 页实时展示进行中状态）。"""
    try:
        db.kv_set(f"release_progress::{project.get('repo', '')}",
                  {"steps": steps[-4:], "ts": time.time()})
    except Exception:
        pass


def run_release(project: dict, cfg: dict, db, trigger: str = "schedule",
                log_dir: str = None) -> dict:
    """执行一次发布。返回 {ok, mode, dry, steps[], error?}"""
    run_start = time.time()   # 基线取开始时刻：发布期间合并的 PR 下轮仍计入
    rc = get_release_cfg(project)
    mode = resolve_mode(db, project, rc)
    if (mode == "paused" or project.get("mode") == "paused"
            or db.kv_get("global_mode") == "paused"):
        # 总开关：paused 下任何发布路径都不起跑——不做 gh 调用、不起 agent、
        # 不触发 workflow（零 token 零额度消耗）。release_cfg.mode / project.mode
        # 显式设置会绕过 resolve_mode 的全局回退，故三层各自直查。
        return {"ok": False, "mode": mode, "dry": False, "steps": [],
                "error": "已暂停（paused）：发布已拒绝，恢复档位后可重新触发"}
    repo = project["repo"]
    slug = state_mod.project_slug(repo)
    base = log_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work_dir = os.path.join(base, "projects", slug, "releases", stamp)
    os.makedirs(work_dir, exist_ok=True)
    steps = []
    _progress(db, project, steps)
    log_event("info", "release", "release_start", log_dir=base,
              repo=repo, mode=mode, trigger=trigger)

    # ---- 路径 1：workflow ----
    wfs = list_release_workflows(repo)
    if wfs:
        wf = wfs[0]
        steps.append(f"检测到发布 workflow: {wf.get('name') or wf.get('path')}")
        _progress(db, project, steps)
        if mode == "dry":
            steps.append("[DRY] 生成发布计划（不执行）")
            _progress(db, project, steps)
            plan = {"workflow": wf, "mode": mode, "trigger": trigger,
                    "note": "DRY：仅计划，不执行"}
            with open(os.path.join(work_dir, "release-plan.json"), "w",
                      encoding="utf-8") as f:
                json.dump(plan, f, ensure_ascii=False, indent=2)
            steps.append("[DRY] 发布计划已落盘")
            _finish(project, db, trigger, ok=True, dry=True, steps=steps, base=base)
            return {"ok": True, "mode": mode, "dry": True, "steps": steps}
        last_err = ""
        steps.append("触发并等待 GitHub Actions（最长 30 分钟）")
        _progress(db, project, steps)
        for attempt in range(1, MAX_FIX_TRIES + 2):
            try:
                ok, detail = trigger_and_wait(repo, wf)
                steps.append(f"workflow 结果（第 {attempt} 次）: {detail}")
                _progress(db, project, steps)
                if ok:
                    tag = gh.latest_release_tag(repo)   # 版本号在 Actions 内产生，回填展示
                    _finish(project, db, trigger, ok=True, steps=steps, base=base,
                            tag=tag, start_ts=run_start)
                    return {"ok": True, "mode": mode, "dry": False, "steps": steps,
                            "tag": tag}
                last_err = detail or last_err
                if attempt > MAX_FIX_TRIES:
                    break
                steps.append(f"失败 → 调用 agent 修错（第 {attempt} 次）")
                _progress(db, project, steps)
                fixed = _fix_workflow_failure(project, cfg, repo, wf, log_dir,
                                              attempt)
                steps.extend(fixed.get("steps", []))
                if fixed.get("awaiting_merge"):
                    # 非 yolo：修复落在未合并 PR 上，立刻重跑 workflow 仍跑在
                    # main 的旧代码上（必然同样失败）——停等人工合并，不烧重试
                    _needs_human(project, db,
                                 "发布 workflow 修复已提交为 PR，待人工合并后重新发布", base)
                    _finish(project, db, trigger, ok=False, steps=steps, base=base,
                            error="修复 PR 待合并（非 yolo 不自动重跑）")
                    return {"ok": False, "mode": mode, "dry": False, "steps": steps,
                            "error": "修复 PR 待合并", "awaiting_merge": True}
                if not fixed.get("ok"):
                    last_err = "修错 agent 未能修复"
                    break
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                steps.append(f"workflow 异常: {e}")
                last_err = str(e)
                break
        _needs_human(project, db, f"发布 workflow 失败（{'; '.join(steps[-3:])}）", base)
        _finish(project, db, trigger, ok=False, steps=steps, base=base,
                error=last_err or "workflow 失败且修错超限")
        return {"ok": False, "mode": mode, "dry": False, "steps": steps,
                "error": last_err or "workflow 失败且修错超限"}

    # ---- 路径 2：Release.md（agent 执行） ----
    release_md = _read_release_md(project, cfg, work_dir)
    if not release_md:
        steps.append("未找到 release workflow 与 Release.md——无法发布")
        _needs_human(project, db, "发布失败：既无 workflow 也无 Release.md（可用初始化功能生成）",
                     base)
        _finish(project, db, trigger, ok=False, steps=steps, base=base,
                error="既无发布 workflow 也无 Release.md")
        return {"ok": False, "mode": mode, "dry": False, "steps": steps,
                "error": "无发布方案"}

    if mode == "dry":
        steps.append("[DRY] 生成发布计划（不执行）")
        _progress(db, project, steps)
        plan = {"path": "Release.md", "mode": mode, "trigger": trigger,
                "release_md": release_md[:2000], "note": "DRY：仅计划，不执行"}
        with open(os.path.join(work_dir, "release-plan.json"), "w",
                  encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
        steps.append("[DRY] 发布计划已落盘（Release.md 路径）")
        _finish(project, db, trigger, ok=True, dry=True, steps=steps, base=base)
        return {"ok": True, "mode": mode, "dry": True, "steps": steps}

    # agent 在沙箱里按 Release.md 执行（生成版本/变更集）
    sb_dir = os.path.join(work_dir, "clone")
    try:
        steps.append("克隆仓库（沙箱）")
        _progress(db, project, steps)
        sandbox.clone(project.get("repo_url") or "", sb_dir,
                      project.get("main_branch", "main"))
        steps.append("agent 执行发布（生成版本/变更集）")
        _progress(db, project, steps)
        meta_fp = sandbox.git_meta_fingerprint(sb_dir)  # agent 有写权限：前后比对 .git
        r = llm.run_stage("release_run", sb_dir, work_dir,
                          inputs={"release_md": release_md,
                                  "repo": repo,
                                  "repo_root": sb_dir,
                                  "mode": mode,
                                  "trigger": trigger},
                          cfg=cfg)
        steps.append(f"agent 发布执行: ok={r.ok}")
        if sandbox.git_meta_fingerprint(sb_dir) != meta_fp:
            _needs_human(project, db, "发布 agent 改动了 .git 元数据（防篡改检查），已中止", base)
            _finish(project, db, trigger, ok=False, steps=steps, base=base,
                    error="agent 篡改 .git 元数据")
            return {"ok": False, "mode": mode, "dry": False, "steps": steps,
                    "error": "agent 篡改 .git 元数据"}
        if not r.ok:
            _needs_human(project, db, f"发布 agent 失败: {'; '.join(r.errors)}", base)
            _finish(project, db, trigger, ok=False, steps=steps, base=base,
                    error=f"agent 执行失败: {'; '.join(r.errors)[:200]}")
            return {"ok": False, "mode": mode, "dry": False, "steps": steps,
                    "error": "agent 执行失败"}
        data = r.data or {}
        version = data.get("version") or ""
        steps.append(f"提议版本: {version}")
        _progress(db, project, steps)
        # 落地（yolo：直推 main + tag + release；semi/full：PR）
        if mode == "yolo":
            landed = _land_release_yolo(project, cfg, sb_dir, data, steps)
        else:
            landed = _land_release_pr(project, cfg, sb_dir, data, steps)
        steps.append("落地中（YOLO 推 main+tag / 其余建 PR）")
        _progress(db, project, steps)
        if not landed.get("ok"):
            _needs_human(project, db, f"发布落地失败: {landed.get('error')}", base)
            _finish(project, db, trigger, ok=False, steps=steps, base=base,
                    error=f"发布落地失败: {landed.get('error')}")
            return {"ok": False, "mode": mode, "dry": False, "steps": steps,
                    "error": landed.get("error")}
        _finish(project, db, trigger, ok=True, steps=steps, base=base,
                tag=landed.get("tag"), url=landed.get("url"),
                start_ts=run_start)
        return {"ok": True, "mode": mode, "dry": False, "steps": steps,
                "tag": landed.get("tag"), "url": landed.get("url")}
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        steps.append(f"发布异常: {e}")
        _needs_human(project, db, f"发布异常: {e}", base)
        _finish(project, db, trigger, ok=False, steps=steps, base=base, error=str(e))
        return {"ok": False, "mode": mode, "dry": False, "steps": steps, "error": str(e)}
    finally:
        sandbox.cleanup(sb_dir)


def _read_release_md(project: dict, cfg: dict, work_dir: str) -> str:
    """读取项目根 Release.md（gh api contents，不做完整 clone）。失败返回空串。"""
    repo = project.get("repo") or ""
    branch = project.get("main_branch", "main")
    try:
        p = subprocess.run(
            ["gh", "api", f"repos/{repo}/contents/Release.md",
             "-f", f"ref={branch}", "-H", "Accept: application/vnd.github.raw"],
            capture_output=True, text=True, timeout=30)
        if p.returncode == 0 and p.stdout:
            return p.stdout[:8000]
        return ""
    except Exception:
        return ""


def _fix_workflow_failure(project: dict, cfg: dict, repo: str, wf: dict,
                          log_dir: str, attempt: int) -> dict:
    """workflow 运行失败 → agent 读日志修错。yolo 直推 main；否则 PR。"""
    steps = []
    slug = state_mod.project_slug(repo)
    base = log_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work_dir = os.path.join(base, "projects", slug, "releases", f"fix-{stamp}")
    os.makedirs(work_dir, exist_ok=True)
    # 取失败日志
    run_log = ""
    try:
        p = subprocess.run(["gh", "run", "view", "--repo", repo, "--log-failed",
                            "--workflow", wf.get("name") or ""],
                           capture_output=True, text=True, timeout=120)
        run_log = (p.stdout or p.stderr or "")[-6000:]
    except Exception:
        pass
    sb_dir = os.path.join(work_dir, "clone")
    try:
        sandbox.clone(project.get("repo_url") or "", sb_dir,
                      project.get("main_branch", "main"))
        meta_fp = sandbox.git_meta_fingerprint(sb_dir)  # agent 有写权限：前后比对 .git
        r = llm.run_stage("release_fix", sb_dir, work_dir,
                          inputs={"repo": repo, "repo_root": sb_dir,
                                  "workflow": wf.get("name") or wf.get("path") or "",
                                  "failed_log": run_log},
                          cfg=cfg)
        steps.append(f"修错 agent: ok={r.ok}")
        if sandbox.git_meta_fingerprint(sb_dir) != meta_fp:
            return {"ok": False,
                    "steps": steps + ["修错 agent 改动了 .git 元数据（防篡改检查），已中止"]}
        if not r.ok:
            return {"ok": False, "steps": steps}
        files = (r.data or {}).get("files") or []
        if not files:
            return {"ok": False, "steps": steps + ["agent 未修改任何文件"]}
        sandbox.stage_all(sb_dir)
        sandbox.commit(sb_dir, f"fix(release): 修复发布 workflow（第 {attempt} 次）")
        mode = resolve_mode(state_mod._db(), project, get_release_cfg(project))
        branch = f"release-fix-{int(time.time())}"
        if mode == "yolo":
            sandbox.push_branch(sb_dir, project.get("main_branch", "main"))
            steps.append("修复已直推 main（YOLO）")
            return {"ok": True, "steps": steps}
        sandbox.create_branch(sb_dir, branch)
        sandbox.push_branch(sb_dir, branch)
        try:
            body = os.path.join(work_dir, "fix-pr-body.md")
            with open(body, "w", encoding="utf-8") as f:
                f.write("发布 workflow 自动修复（由 release agent 生成），请审阅合并。\n")
            gh.create_pr(repo, branch, "fix(release): 修复发布 workflow", body)
            steps.append(f"修复 PR 已创建（{branch}），待合并后重新触发发布")
        except (RuntimeError, OSError) as e:
            steps.append(f"PR 创建失败: {e}")
            return {"ok": False, "steps": steps}
        # 非 yolo：修复未合入 main，立即重跑 workflow 必败 → 标记等待人工合并
        return {"ok": True, "awaiting_merge": True, "steps": steps}
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "steps": steps + [f"修错异常: {e}"]}
    finally:
        sandbox.cleanup(sb_dir)


def _land_release_yolo(project: dict, cfg: dict, sb_dir: str, data: dict,
                       steps: list) -> dict:
    """YOLO：commit（agent 已改文件）+ 推 main + tag + gh release。"""
    repo = project["repo"]
    branch = project.get("main_branch", "main")
    tag = data.get("version") or ""
    # 版本号来自 LLM 输出：进 gh argv 位置，必须先过白名单（- 开头 = flag 注入）。
    # 校验在 push 之前：tag 非法时不能把 main 推出去半截
    if not tag or not valid_version_tag(tag):
        return {"ok": False, "error": f"agent 给出的版本号非法: {tag[:60]!r}",
                "steps": steps}
    try:
        sandbox.stage_all(sb_dir)
        if sandbox.has_changes(sb_dir):
            sandbox.commit(sb_dir, f"chore(release): {data.get('version', '')}")
            sandbox.push_branch(sb_dir, branch)
            steps.append("版本改动已推送 main")
        notes = data.get("release_notes") or data.get("changelog") or f"Release {tag}"
        notes_file = os.path.join(os.path.dirname(sb_dir), "notes.md")
        with open(notes_file, "w", encoding="utf-8") as f:
            f.write(notes)
        p = subprocess.run(["gh", "release", "create", tag, "--repo", repo,
                            "--title", tag, "--notes-file", notes_file],
                           capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            return {"ok": False,
                    "error": f"gh release create 失败: {(p.stderr or p.stdout)[-200:]}",
                    "steps": steps}
        url = (p.stdout or "").strip().splitlines()[-1:] or [""]
        steps.append(f"GitHub Release 已创建: {tag}")
        return {"ok": True, "tag": tag, "url": url[0], "steps": steps}
    except (RuntimeError, OSError) as e:
        return {"ok": False, "error": str(e), "steps": steps}


def _land_release_pr(project: dict, cfg: dict, sb_dir: str, data: dict,
                     steps: list) -> dict:
    """semi/full：把版本改动推成 PR（tag 由人工）。"""
    repo = project["repo"]
    ver = str(data.get("version") or "")
    # version 来自 LLM 输出且进分支名/标题：白名单校验（- 开头/路径符即拒绝）
    if ver and not valid_version_tag(ver):
        return {"ok": False, "error": f"agent 给出的版本号非法: {ver[:60]!r}",
                "steps": steps}
    branch = f"release/{ver or int(time.time())}"
    try:
        sandbox.stage_all(sb_dir)
        if not sandbox.has_changes(sb_dir):
            steps.append("agent 未产生版本改动——跳过 PR")
            return {"ok": True, "tag": None, "steps": steps}
        sandbox.create_branch(sb_dir, branch)
        sandbox.commit(sb_dir, f"chore(release): {data.get('version', '')}")
        sandbox.push_branch(sb_dir, branch)
        body = os.path.join(os.path.dirname(sb_dir), "pr-body.md")
        with open(body, "w", encoding="utf-8") as f:
            f.write(f"发布准备（{data.get('version', '')}）。\n\n{dict(data).get('summary', '')}\n")
        pr_url = gh.create_pr(repo, branch, f"Release {data.get('version', '')}", body)
        steps.append(f"发布 PR 已创建（{branch}）；tag 由人工发布")
        return {"ok": True, "tag": None, "url": pr_url, "steps": steps}
    except (RuntimeError, OSError) as e:
        return {"ok": False, "error": str(e), "steps": steps}


def _finish(project: dict, db, trigger: str, ok: bool, steps: list,
            base: str, dry: bool = False, tag: str = None,
            url: str = "", error: str = "", start_ts: float = None) -> None:
    """记录结果：成功则重置双计数；失败留痕 last_error；两种结局都写
    last_result（UI 全局 toast / 结果卡展示）+ 清进度 kv；成功发系统通知。
    写 releases 表 + 统一日志。"""
    repo = project["repo"]
    try:
        db.kv_set(f"release_progress::{repo}", "")   # 进行中状态清理
    except Exception:
        pass
    st = get_release_state(db, project)
    if ok and not dry:
        sha = None
        try:
            sha = gh.latest_commit_sha(repo, project.get("main_branch", "main"))
        except Exception:
            pass
        st["last_ts"] = start_ts or time.time()
        if sha:
            st["last_sha"] = sha
        st.pop("last_error", None)
        st.pop("last_error_ts", None)
        set_release_state(db, project, st)
        if project.get("id"):
            try:
                db.add_release(project["id"], tag or "", url or "", trigger)
            except Exception:
                pass
    elif not ok:
        st["last_error"] = (error or "; ".join(steps[-2:]) or "未知错误")[-300:]
        st["last_error_ts"] = time.time()
        set_release_state(db, project, st)
    try:
        st["last_result"] = {"ok": bool(ok), "dry": bool(dry),
                             "tag": tag or "", "error": "" if ok else (error or "")[:200],
                             "ts": time.time()}
        set_release_state(db, project, st)
    except Exception:
        pass
    if ok and not dry:
        notify("✅ 发布成功", f"{repo} {tag or ''}".strip())
    log_event("info" if ok else "error", "release", "release_finish", log_dir=base,
              repo=repo, ok=ok, dry=dry, trigger=trigger, tag=tag,
              steps=steps[-5:])


def _needs_human(project: dict, db, reason: str, base: str) -> None:
    log_event("error", "release", "needs_human", log_dir=base,
              repo=project["repo"], reason=reason[:300])
    notify("issue-triage-bot · release", f"{project['repo']}: {reason[:100]}")
