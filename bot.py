"""issue-triage-bot 入口：本地/局域网 Web 后台(:8770) + 多项目调度线程。

- 凭据与环境集中在 .env（BOT_MODEL / BOT_TOKEN / BOT_AUTH_ENABLED），env 优先于 config.json。
- AUTH_ENABLED=0 时后台完全开放；=1 时所有 /api/* 需 X-Bot-Token（页面本身不鉴权）。
- 调度线程按 config/state 的 poll_interval_sec 轮询（0=仅手动）；多项目（SQLite projects 表）。
- 多 issue 可交错并行，agent 调用由全局闸门（GATE，agent_limit 可配）排队。
- 提示词可在后台编辑，保存即热加载（下一轮生效，进行中的不受影响）。
"""
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import github as gh
import llm
import pipeline as pl
import prompts_cfg
import release as release_mod
import sandbox
import state as state_mod
from gate import GATE
from util import (append_log, cleanup_logs, load_json, log_event,
                  valid_ref_name, valid_repo_name, valid_repo_url)

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.json")
ENV_PATH = os.path.join(BASE, ".env")
WEB_DIR = os.path.join(BASE, "web")
TOKEN_PATH = os.path.join(state_mod.STATE_DIR, "web_token.txt")


def load_env() -> dict:
    """极简 .env 解析：KEY=VALUE，支持 # 注释与引号。"""
    env = {}
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def save_env(env: dict) -> None:
    lines = ["# issue-triage-bot 环境与凭据（修改后需重启服务生效）", ""]
    for k in ("BOT_MODEL", "BOT_TOKEN", "LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL",
              "GH_TOKEN"):
        v = env.get(k)
        if v:
            v = str(v).replace("\n", "").replace("\r", "")
            if v:
                lines.append(f"{k}={v}")
    lines.append(f"BOT_AUTH_ENABLED={1 if env.get('BOT_AUTH_ENABLED') in ('1', 'true', 'yes', 'on', True) else 0}")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass


def ensure_path():
    """launchd/cron 环境下 PATH 极短，补齐 gh/git/oc-run2 所需路径。"""
    need = ["/opt/homebrew/bin", "/usr/local/bin",
            os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.opencode/bin")]
    cur = os.environ.get("PATH", "")
    for p in need:
        if os.path.isdir(p) and p not in cur.split(os.pathsep):
            cur = p + os.pathsep + cur
    os.environ["PATH"] = cur


def get_web_token(cfg: dict) -> str:
    """auth_enabled=False → 空令牌（不鉴权）；否则 BOT_TOKEN/env 固定值；再否则随机生成。"""
    if not cfg.get("auth_enabled", True):
        return ""
    fixed = (cfg.get("web_token") or "").strip()
    if fixed:
        return fixed
    os.makedirs(state_mod.STATE_DIR, exist_ok=True)
    if os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH, encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
        except OSError:
            pass
    t = secrets.token_hex(12)
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(t)
    try:  # 令牌只给当前用户读
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    return t


def lan_ips() -> list:
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return ips or ["127.0.0.1"]


def issue_log_dir(cfg: dict, n, repo: str = None) -> str:
    """issue 档案目录（v3 项目分域）：logs/projects/<owner__repo>/issues/<n>。"""
    slug = ((repo or cfg.get("repo")) or "").replace("/", "__")
    return os.path.join(BASE, "logs", "projects", slug, "issues", str(n))


def _query_one(query: str, key: str) -> str:
    from urllib.parse import parse_qs
    return (parse_qs(query).get(key) or [""])[0]


def resolve_project(cfg: dict, repo: str = "") -> dict:
    """按 repo 解析项目；缺省=第一个项目（兼容视角）。"""
    ps = list_projects_cfg(cfg)
    if repo:
        for p in ps:
            if p.get("repo") == repo:
                return p
    return ps[0] if ps else {}


def _set_issue_fields(cfg: dict, repo: str, n: int, **fields) -> None:
    """Patch status columns without replaying an older permission or stage."""
    d = state_mod._db()
    target = repo or state_mod._compat_repo(d)
    project = d.get_project(target)
    if not project and (not repo or repo == cfg.get("repo")):
        project = d.get_or_add_project(target, "")
    if project:
        d.upsert_issue(project["id"], n, **fields)


poll_lock = threading.Lock()   # 轮询去重：并发 poll 只放行一个（修连点竞态）
_running = {}                  # key("repo#n") → {"repo","n","started"}（多 issue 并行）
_running_lock = threading.Lock()


def load_cfg() -> dict:
    cfg = load_json(CFG_PATH, {})
    for k, d in [("repo", ""), ("repo_url", ""), ("model", "volcengine/glm-5.3-flash"),
                 ("poll_interval_sec", 600), ("web_port", 8770),
                 ("build_cmd", ["true"]), ("main_branch", "main"),
                 ("issue_time_budget_sec", 14400),
                 ("sandbox_root", "~/.cache/issue-triage-bot/sandbox"),
                 ("max_json_retries", 3), ("max_rework_loops", 2),
                 ("web_host", "127.0.0.1"), ("web_token", ""),
                 ("auth_enabled", True), ("cleanup_sessions", []),
                 ("llm_api_base", ""), ("llm_api_key", ""), ("llm_model", "glm-5.3-flash"),
                 ("repo_intro", ""), ("disclosure_enabled", True),
                 ("log_retention_days", 30), ("oc_variant", "low"),
                 ("use_direct_oc2", True), ("agent_timeout_sec", 7200),
                 ("agent_idle_timeout_sec", 1200)]:
        cfg.setdefault(k, d)
    env = load_env()  # .env 优先于 config.json
    if env.get("BOT_MODEL"):
        cfg["model"] = env["BOT_MODEL"]
    if env.get("BOT_TOKEN"):
        cfg["web_token"] = env["BOT_TOKEN"]
    if env.get("BOT_AUTH_ENABLED"):
        cfg["auth_enabled"] = env["BOT_AUTH_ENABLED"] in ("1", "true", "yes", "on")
    if env.get("LLM_API_BASE"):
        cfg["llm_api_base"] = env["LLM_API_BASE"]
    if env.get("LLM_API_KEY"):
        cfg["llm_api_key"] = env["LLM_API_KEY"]
    if env.get("LLM_MODEL"):
        cfg["llm_model"] = env["LLM_MODEL"]
    return cfg


def save_cfg(cfg: dict) -> None:
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def apply_agent_limit(cfg: dict) -> int:
    """把全局 agent 并发上限应用到 GATE（kv.agent_limit，默认 3）。"""
    try:
        limit = int(state_mod._db().kv_get("agent_limit", 3) or 3)
    except Exception:
        limit = 3
    return GATE.set_limit(limit)


def list_projects_cfg(cfg: dict) -> list:
    """项目列表：SQLite 优先；为空时用 cfg 构造单项目（向后兼容）。"""
    try:
        ps = state_mod._db().list_projects()
        if ps:
            return ps
    except Exception:
        pass
    repo = cfg.get("repo") or ""
    if not repo:
        return []
    return [{"id": None, "repo": repo, "repo_url": cfg.get("repo_url", ""),
             "mode": "default", "build_cmd": cfg.get("build_cmd", ["true"]),
             "build_env": cfg.get("build_env") or {},
             "main_branch": cfg.get("main_branch", "main")}]


def _ready_issues(proj: dict) -> list:
    """db 中 stage=pending 的 issue（自动续跑：中断恢复/排队补偿）。"""
    try:
        d = state_mod._db()
        pid = proj.get("id")
        if pid is None:
            p = d.get_project(proj.get("repo"))
            pid = p["id"] if p else None
        if pid is None:
            return []
        return [i for i in d.list_issues(pid)
                if i.get("stage") == "pending"
                and (i.get("mode") or "default") != "paused"]
    except Exception:
        return []


def run_pipeline_once(cfg: dict, n: int, project: dict = None, *, rerun=False) -> bool:
    """启动一个 issue 处理任务：spawn 线程（非阻塞）、同 issue 防重入。

    多 issue 可交错并行推进；每次 agent 调用由 GATE 排队（v3 并发模型）。
    """
    proj = project
    if proj is None:
        ps = list_projects_cfg(cfg)
        proj = ps[0] if ps else None
    if not proj:
        return False
    repo = proj.get("repo") or ""
    key = f"{repo}#{n}"
    with _running_lock:
        # 同 issue 防重入：主流程/补发/merge-fix 互斥（key 前缀匹配覆盖 #pub/#mf 变体）
        if any(k == key or k.startswith(key + "#") for k in _running):
            return False
        if rerun:
            _set_issue_fields(cfg, repo, n, stage="pending", error=None, retries={})
        _running[key] = {"repo": repo, "n": n, "started": time.time()}

    def _job():
        try:
            pl.Pipeline(cfg, proj).process(n)
        except Exception as e:
            log_event("error", "pipeline", "job_crash", issue=n,
                      log_dir=os.path.join(BASE, "logs"),
                      repo=repo, error=f"{type(e).__name__}: {e}")
        finally:
            with _running_lock:
                _running.pop(key, None)

    threading.Thread(target=_job, daemon=True).start()
    return True


def scheduler_loop():
    cfg = load_cfg()
    log_event("info", "scheduler", "started",
              log_dir=os.path.join(BASE, "logs"),
              interval=cfg.get("poll_interval_sec"))
    while True:
        try:
            cfg = load_cfg()      # 每轮重读：config.json 改动不重启也生效
            st = state_mod.load()   # 放进 try：瞬时 DB 抖动不允许杀死调度线程
        except Exception as e:
            log_event("error", "scheduler", "state_load_error", error=str(e),
                      log_dir=os.path.join(BASE, "logs"))
            time.sleep(5)
            continue
        interval = int(st.get("poll_interval_sec", cfg.get("poll_interval_sec", 600)))
        if st.get("global_mode") == "paused" or interval <= 0:
            time.sleep(5)
            continue
        try:
            projects = list_projects_cfg(cfg)
            # 1) 轮询发现新 issue（轻量；与手动「立即轮询」互斥）
            if poll_lock.acquire(blocking=False):
                try:
                    for proj in projects:
                        if proj.get("mode") == "paused":
                            continue
                        try:
                            pipe = pl.Pipeline(cfg, proj)
                            for n in pipe.poll():
                                run_pipeline_once(cfg, n, proj)
                            for n in pipe.check_waiting():
                                run_pipeline_once(cfg, n, proj)   # 作者回复 → 立即重审
                        except Exception as e:
                            log_event("error", "scheduler", "poll_error",
                                      log_dir=os.path.join(BASE, "logs"),
                                      repo=proj.get("repo"), error=str(e))
                finally:
                    poll_lock.release()
            # 2) 补偿：pending 但未运行的 issue（中断恢复/排队）
            if state_mod.load().get("global_mode") != "paused":
                for proj in projects:
                    if proj.get("mode") == "paused":
                        continue
                    for issue in _ready_issues(proj):
                        run_pipeline_once(cfg, issue["number"], proj)
            # 3) release 条件检查（条件评估含本地计数 + 可能一次 gh 调用）
            try:
                d = state_mod._db()
                for proj in projects:
                    if proj.get("mode") == "paused" or not proj.get("id"):
                        continue
                    if d.kv_get("release_running"):
                        break   # 快路径跳过（省 gh 调用）；CAS 仍是真实互斥点
                    due = release_mod.check_due(d, proj)
                    if not due["due"]:
                        continue
                    # 原子占位（CAS）：与手动 /api/release/run 并发时只有一个能起跑
                    if not d.kv_cas("release_running", "", proj["repo"]):
                        break
                    log_event("info", "scheduler", "release_due",
                              log_dir=os.path.join(BASE, "logs"),
                              repo=proj["repo"], reasons=due["reasons"])

                    def _rel_job(p=proj):
                        try:
                            release_mod.run_release(
                                p, cfg, state_mod._db(), trigger="schedule",
                                log_dir=os.path.join(BASE, "logs"))
                        finally:
                            state_mod._db().kv_set("release_running", "")
                    threading.Thread(target=_rel_job, daemon=True).start()
                    break
            except Exception as e:
                log_event("error", "scheduler", "release_check_error", error=str(e),
                          log_dir=os.path.join(BASE, "logs"))
        except Exception as e:  # 调度线程永不退出
            log_event("error", "scheduler", "loop_error", error=str(e),
                      log_dir=os.path.join(BASE, "logs"))
        for _ in range(max(interval, 5)):
            try:
                if state_mod.load().get("global_mode") == "paused":
                    break
            except Exception:
                pass   # 瞬时 DB 抖动：按未暂停处理，下一轮自愈
            time.sleep(1)


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "issue-triage-bot/1.0"
    token = ""           # main() 注入
    auth_enabled = True  # main() 注入

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    # --- helpers ---
    def _authed(self) -> bool:
        if not self.auth_enabled:
            return True
        if not self.token:
            return True
        import hmac
        if hmac.compare_digest(self.headers.get("X-Bot-Token") or "", self.token):
            return True
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        return hmac.compare_digest((q.get("token") or [""])[0], self.token)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: str, ctype: str):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        """解析 JSON 请求体；畸形/超限返回 None（调用方应回 400，不可炸穿连接）。"""
        try:
            ln = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return None
        if ln <= 0:
            return {}
        if ln > 1024 * 1024:   # 1MB 上限：防异常大体撑爆内存
            return None
        try:
            data = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _safe_issue_file(self, p: str) -> bool:
        base = os.path.realpath(os.path.join(BASE, "logs", "projects"))
        real = os.path.realpath(p)
        return (real == base or real.startswith(base + os.sep)) and os.path.isfile(real)

    # --- GET ---
    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html"):
            dist_index = os.path.join(WEB_DIR, "dist", "index.html")
            if os.path.isfile(dist_index):
                return self._file(dist_index, "text/html; charset=utf-8")
            return self._file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")

        if not path.startswith("/api/"):
            # 新前端（web/dist）静态资源 + SPA 路由回退；无 dist 时回退旧版单页
            dist = os.path.join(WEB_DIR, "dist")
            if os.path.isdir(dist) and ".." not in path:
                rel = path.lstrip("/")
                full = os.path.join(dist, rel)
                if rel and os.path.isfile(full):
                    ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
                    return self._file(full, ctype)
                if "." not in rel.rsplit("/", 1)[-1]:
                    return self._file(os.path.join(dist, "index.html"),
                                      "text/html; charset=utf-8")
            return self._json({"error": "not found"}, 404)

        if not self._authed():
            return self._json({"error": "unauthorized", "hint": "请在后台输入访问令牌"}, 401)

        if path == "/api/overview":
            st = state_mod.load()
            cfg = load_cfg()
            issues = []
            for k, e in sorted(st.get("issues", {}).items(), key=lambda kv: -int(kv[0])):
                issues.append({"number": int(k), **{x: e.get(x) for x in
                                ("mode", "stage", "title", "retries", "verdict", "error",
                                 "summary", "pr_url", "cost", "tokens",
                                 "complexity", "merged")}})
            with _running_lock:
                running_list = [{"repo": v["repo"], "issue": v["n"]} for v in _running.values()]
            return self._json({"global_mode": st.get("global_mode"),
                               "poll_interval_sec": st.get("poll_interval_sec"),
                               "cleanup_sessions": cfg.get("cleanup_sessions", []),
                               "auth_enabled": bool(cfg.get("auth_enabled", True)),
                               "running": running_list[0]["issue"] if running_list else None,
                               "running_all": running_list,
                               "agent_gate": GATE.snapshot(),
                               "model": cfg.get("model"),
                               "repo": cfg.get("repo"),
                               "issues": issues})

        if path.startswith("/api/issue/"):
            n = path.rsplit("/", 1)[-1]
            repo = _query_one(query, "repo")
            # n/repo 进文件路径：n 必须纯数字，repo 必须 owner/name（防 ../ 拼接逃逸）
            if not n.isdigit():
                return self._json({"error": "bad issue"}, 400)
            if repo and not valid_repo_name(repo):
                return self._json({"error": "bad repo"}, 400)
            cfg = load_cfg()
            if repo:
                d = state_mod._db()
                proj = d.get_project(repo)
                if not proj:
                    return self._json({"error": "unknown repo"}, 400)
                row = d.get_issue(proj["id"], int(n))
                e = state_mod._row_to_entry(row) if row else None
            else:
                st = state_mod.load()
                e = st.get("issues", {}).get(n)
            ddir = issue_log_dir(cfg, n, repo or None)
            if _query_one(query, "review") == "1":
                snapshot = os.path.join(ddir, "review-snapshot.json")
                if self._safe_issue_file(snapshot):
                    return self._file(snapshot, "application/json; charset=utf-8")
                # Older DRY runs only archived the patch in their outbox.
                patch = os.path.join(ddir, "outbox", "fix.patch")
                diff = ""
                if self._safe_issue_file(patch):
                    with open(patch, encoding="utf-8", errors="replace") as handle:
                        diff = handle.read(160001)
                return self._json({"diff": diff[:160000], "truncated": len(diff) > 160000,
                                   "reviews": (e or {}).get("review_json") or {}})
            files = []
            if os.path.isdir(ddir):
                for root, _, fs in os.walk(ddir):
                    for f in fs:
                        full = os.path.join(root, f)
                        files.append({"path": os.path.relpath(full, BASE),
                                      "size": os.path.getsize(full)})
            files.sort(key=lambda x: x["path"])
            reply_text = ""
            for fname in ("final-reply.md", "triage-comment.md"):
                try:
                    with open(os.path.join(ddir, "outbox", fname), encoding="utf-8") as f:
                        reply_text = f.read()
                    break
                except OSError:
                    continue
            progress_info = None
            ppath = os.path.join(ddir, "agent-progress.jsonl")
            if os.path.isfile(ppath):
                try:
                    with open(ppath, encoding="utf-8", errors="replace") as f:
                        tail = f.readlines()[-1:]
                    last = json.loads(tail[0]) if tail else {}
                    progress_info = {"last": last,
                                     "age_sec": int(time.time() - os.path.getmtime(ppath))}
                except (OSError, json.JSONDecodeError, ValueError):
                    pass
            return self._json({
                "repo": repo or cfg.get("repo") or "",
                "entry": e,
                "parsed": {
                    "audit": (e or {}).get("audit"),
                    "triage": (e or {}).get("verdict"),
                    "fix": (e or {}).get("fix_json"),
                    "reviews": (e or {}).get("review_json"),
                    "final": (e or {}).get("final_json"),
                    "reply": reply_text,
                },
                "progress": progress_info,
                "files": files})

        if path.startswith("/api/issue-zip/"):
            import io
            import zipfile
            n = path.rsplit("/", 1)[-1]
            if not n.isdigit():
                return self._json({"error": "bad issue"}, 400)
            repo = _query_one(query, "repo")
            if repo and not valid_repo_name(repo):
                return self._json({"error": "bad repo"}, 400)
            d = issue_log_dir(load_cfg(), n, repo or None)
            if not os.path.isdir(d):
                return self._json({"error": "not found"}, 404)
            d_real = os.path.realpath(d)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for root, _, fs in os.walk(d):
                    for f in fs:
                        full = os.path.join(root, f)
                        # 符号链接逃逸防护：agent 可在 issue 目录放 symlink 指向
                        # ~/.ssh 等，打包前按 realpath 校验仍在档案目录内
                        real = os.path.realpath(full)
                        if not real.startswith(d_real + os.sep) or not os.path.isfile(real):
                            continue
                        z.write(real, os.path.relpath(full, d))
            body = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             f'attachment; filename="issue-{n}-archive.zip"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/settings":
            cfg = load_cfg()
            return self._json({"disclosure_enabled": bool(cfg.get("disclosure_enabled", True)),
                               "repo_intro": cfg.get("repo_intro", ""),
                               "log_retention_days": int(cfg.get("log_retention_days", 30)),
                               "oc_variant": cfg.get("oc_variant", "low")})

        if path.startswith("/api/publish/preview/"):
            n = path.rsplit("/", 1)[-1]
            if not n.isdigit():
                return self._json({"error": "bad issue"}, 400)
            cfg = load_cfg()
            ps = list_projects_cfg(cfg)
            pobj = pl.Pipeline(cfg, ps[0] if ps else None)
            return self._json(pobj.publish_preview(int(n)))

        if path == "/api/release/status":
            cfg = load_cfg()
            d = state_mod._db()
            gpaused = d.kv_get("global_mode") == "paused"
            out = []
            for proj in list_projects_cfg(cfg):
                try:
                    paused = gpaused or proj.get("mode") == "paused"
                    if paused:
                        # 总开关语义：暂停态连状态页都不发 gh 调用——
                        # 只回放缓存 state（due/workflows 现场探测跳过）
                        out.append({"repo": proj.get("repo"), "paused": True,
                                    "due": False, "reasons": [],
                                    "merged_since": 0,
                                    "cfg": release_mod.get_release_cfg(proj),
                                    "state": release_mod.get_release_state(d, proj) or {},
                                    "workflows": [],
                                    "progress": d.kv_get(
                                        f"release_progress::{proj.get('repo')}", "") or ""})
                        continue
                    due = release_mod.check_due(d, proj)
                    wfs = []
                    if proj.get("repo"):
                        wfs = [w.get("name") for w in
                               release_mod.list_release_workflows(proj["repo"])]
                    out.append({"repo": proj.get("repo"),
                                "due": due["due"], "reasons": due["reasons"],
                                "merged_since": due["merged_since"],
                                "cfg": due["cfg"], "state": due["state"] or {},
                                "workflows": wfs,
                                "progress": d.kv_get(
                                    f"release_progress::{proj.get('repo')}", "") or ""})
                except Exception as e:
                    out.append({"repo": proj.get("repo"), "error": str(e)})
            return self._json({"projects": out,
                               "running": d.kv_get("release_running", "")})

        if path == "/api/init-status":
            from urllib.parse import parse_qs
            q = parse_qs(query)
            repo = (q.get("repo") or [""])[0]
            d = state_mod._db()
            if not repo:
                ps = list_projects_cfg(load_cfg())
                repo = ps[0]["repo"] if ps else ""
            return self._json({"repo": repo,
                               "status": d.kv_get(f"init_status::{repo}", ""),
                               "result": d.kv_get(f"init_result::{repo}", None)})

        if path == "/api/wizard/status":
            d = state_mod._db()
            ps = list_projects_cfg(load_cfg())
            env = load_env()
            return self._json({"done": bool(d.kv_get("wizard_done")),
                               "has_project": bool(ps and ps[0].get("repo")),
                               "llm_configured": bool(env.get("LLM_API_BASE")
                                                      and env.get("LLM_API_KEY")),
                               "gh_token_configured": bool(env.get("GH_TOKEN"))})

        if path == "/api/wizard/gh":
            try:
                p = subprocess.run(["gh", "auth", "status"], capture_output=True,
                                   text=True, timeout=30)
                authed = p.returncode == 0
                user = ""
                if authed:
                    m = re.search(r"account\s+(\S+)", (p.stdout or "") + (p.stderr or ""))
                    user = m.group(1) if m else ""
                return self._json({"authed": authed, "user": user,
                                   "hint": "" if authed else
                                   "未检测到 gh 认证：可配置 GH_TOKEN，或在终端执行 gh auth login"})
            except Exception as e:
                return self._json({"authed": False, "user": "", "hint": str(e)})

        if path == "/api/wizard/opencode":
            exe = shutil.which("opencode2")
            version = ""
            if exe:
                try:
                    p = subprocess.run([exe, "--version"], capture_output=True,
                                       text=True, timeout=20)
                    version = ((p.stdout or p.stderr).strip().splitlines() or [""])[0][:60]
                except Exception:
                    pass
            return self._json({"installed": bool(exe), "path": exe or "",
                               "version": version,
                               "install_cmd": "npm i -g @opencode-ai/cli@beta"})

        if path == "/api/models":
            cfg = load_cfg()
            base = (cfg.get("llm_api_base") or "").rstrip("/")
            key = cfg.get("llm_api_key") or ""
            if not base or not key:
                return self._json({"ok": False, "models": [],
                                   "error": "请先配置 LLM_API_BASE 与 LLM_API_KEY"})
            try:
                req = urllib.request.Request(
                    base + "/models", headers={"Authorization": f"Bearer {key}"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode("utf-8"))
                ids = []
                for m in (data.get("data") or []):
                    if isinstance(m, dict) and m.get("id"):
                        if m.get("status") in (None, "", "Available", "available"):
                            ids.append(m["id"])
                return self._json({"ok": True, "models": sorted(set(ids))})
            except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                    json.JSONDecodeError) as e:
                return self._json({"ok": False, "models": [], "error": f"拉取失败: {e}"})

        if path == "/api/projects":
            cfg = load_cfg()
            d = state_mod._db()
            out = []
            for p in list_projects_cfg(cfg):
                issues = []
                pid = p.get("id")
                if pid:
                    for i in d.list_issues(pid):
                        issues.append({k: i.get(k) for k in
                                       ("number", "title", "stage", "complexity", "mode",
                                        "cost", "tokens", "merged", "updated_at", "error")})
                base = {k: p.get(k) for k in
                        ("id", "repo", "repo_url", "intro", "mode", "agent_limit",
                         "main_branch", "build_cmd", "release_cfg")}
                base["issues"] = issues
                out.append(base)
            return self._json({"projects": out})

        if path == "/api/issues":
            cfg = load_cfg()
            repo = _query_one(query, "repo")
            d = state_mod._db()
            proj = d.get_project(repo) if repo else None
            if proj is None:
                ps = list_projects_cfg(cfg)
                proj = d.get_project(ps[0]["repo"]) if ps and ps[0].get("repo") else None
            issues = []
            if proj:
                for i in d.list_issues(proj["id"]):
                    issues.append({k: i.get(k) for k in
                                   ("number", "title", "stage", "mode", "complexity",
                                    "cost", "tokens", "merged", "pr_url", "updated_at",
                                    "error", "summary", "verdict", "publish_status",
                                    "merge_fix_status", "paused_from")})
            return self._json({"repo": proj["repo"] if proj else "", "issues": issues})

        if path == "/api/dashboard":
            # 待办中心聚合：跨全部项目算出「需要你处理」与「流水线中」两列。
            cfg = load_cfg()
            d = state_mod._db()
            st0 = state_mod.load()
            ttl = float(cfg.get("waiting_info_ttl_sec", 7 * 24 * 3600)
                        or 7 * 24 * 3600)
            attention, in_flight, board = [], [], []
            _ACTIVE = ("pending", "audit", "triage", "fix", "gate",
                       "review", "final", "reply", "execute")
            for proj_cfg in list_projects_cfg(cfg):
                repo = proj_cfg.get("repo") or ""
                proj = d.get_project(repo) if repo else None
                if not proj:
                    continue
                for row in d.list_issues(proj["id"]):
                    e = state_mod._row_to_entry(row)
                    stage = e.get("stage") or "pending"
                    base = {"repo": repo, "number": int(row["number"]),
                            "title": e.get("title") or "", "stage": stage,
                            "mode": e.get("mode") or "default",
                            "updated_at": e.get("updated_at")}
                    board.append(base)
                    mfr = (e.get("merge_fix_result") or {})
                    pbr = (e.get("publish_result") or {})
                    if e.get("merge_fix_status") == "error":
                        attention.append({**base, "kind": "op_failed",
                            "tone": "err", "tag": "合并修复失败",
                            "reason": mfr.get("error") or "merge 冲突修复失败"})
                    elif e.get("publish_status") == "error":
                        attention.append({**base, "kind": "op_failed",
                            "tone": "err", "tag": "补发失败",
                            "reason": pbr.get("error") or "PR 补发失败"})
                    elif stage == "needs_human":
                        attention.append({**base, "kind": "needs_human",
                            "tone": "err", "tag": "需人工",
                            "reason": e.get("error")
                                      or "bot 无法自行处理，等待人工介入"})
                    elif stage == "waiting_info":
                        since = float((e.get("retries") or {})
                                      .get("info_waiting_since") or 0)
                        left = ttl - max(0.0, time.time() - since) if since else ttl
                        reason = ("追问评论已发出，作者未回复 — 已超过等待上限，"
                                  "下轮检查将自动转「需人工」" if left <= 0 else
                                  f"追问评论已发出，作者未回复 — 约 "
                                  f"{int(left // 86400) + 1} 天后自动转「需人工」")
                        attention.append({**base, "kind": "waiting",
                            "tone": "warn", "tag": "等待作者补充",
                            "reason": reason,
                            "waiting_since": since, "ttl_sec": ttl})
                    elif stage == "done" and e.get("patch_saved") \
                            and not e.get("pr_url"):
                        attention.append({**base, "kind": "publishable",
                            "tone": "info", "tag": "产物待补发",
                            "reason": "流水线已完成并留存补丁 — "
                                      "档位允许时可直接补发为 PR"})
                    if stage in _ACTIVE:
                        in_flight.append({**base,
                            "complexity": e.get("complexity"),
                            "cost": e.get("cost") or 0,
                            "tokens": e.get("tokens") or 0,
                            "pr_url": e.get("pr_url"),
                            "paused_from": e.get("paused_from"),
                            "audit": bool(e.get("audit")),
                            "history": e.get("history") or []})
            attention.sort(key=lambda x: str(x.get("updated_at") or ""))
            in_flight.sort(key=lambda x: str(x.get("updated_at") or ""),
                           reverse=True)
            with _running_lock:
                running_list = [{"repo": v["repo"], "issue": v["n"],
                                 "started": int(v.get("started") or 0)}
                                for v in _running.values()]
            return self._json({"global_mode": st0.get("global_mode"),
                               "poll_interval_sec": st0.get("poll_interval_sec"),
                               "model": cfg.get("model"),
                               "repo": cfg.get("repo"),
                               "running": running_list,
                               "agent_gate": GATE.snapshot(),
                               "board": sorted(board, key=lambda item: item["updated_at"] or "", reverse=True)[:100],
                               "attention": attention[:50],
                               "in_flight": in_flight[:50]})

        if path == "/api/activity":
            try:
                limit = min(max(int(_query_one(query, "limit") or "80"), 10), 400)
            except ValueError:
                limit = 80
            import glob as _g
            evs = []
            proj_root = os.path.join(BASE, "logs", "projects")
            for pp in _g.glob(os.path.join(proj_root, "*", "issues", "*",
                                           "agent-progress.jsonl")):
                try:
                    if time.time() - os.path.getmtime(pp) > 6 * 3600:
                        continue
                    parts = pp.split(os.sep)
                    slug, num = parts[-4], parts[-2]
                    with open(pp, encoding="utf-8", errors="replace") as f:
                        tail = f.readlines()[-60:]
                except OSError:
                    continue
                for ln in tail:
                    try:
                        rec = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    rec["source"] = "agent"
                    rec["repo"] = slug.replace("__", "/", 1)
                    rec.setdefault("issue", num)
                    evs.append(rec)
            for lf in sorted(_g.glob(os.path.join(BASE, "logs", "bot-*.jsonl")))[-2:]:
                try:
                    with open(lf, encoding="utf-8", errors="replace") as f:
                        lines_ = f.readlines()[-400:]
                except OSError:
                    continue
                for ln in lines_:
                    try:
                        rec = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    rec["source"] = "bot"
                    evs.append(rec)
            evs.sort(key=lambda r: str(r.get("ts") or ""))
            with _running_lock:
                running_list = [{"repo": v["repo"], "issue": v["n"],
                                 "started": int(v.get("started") or 0)}
                                for v in _running.values()]
            return self._json({"events": evs[-limit:], "running": running_list,
                               "agent_gate": GATE.snapshot()})

        if path == "/api/stats":
            try:
                days = min(max(int(_query_one(query, "days") or "14"), 7), 60)
            except ValueError:
                days = 14
            d = state_mod._db()
            today = date.today()
            keys = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
            per = {k: {"date": k, "created": 0, "done": 0, "cost": 0.0, "tokens": 0}
                   for k in keys}
            totals = {"issues": 0, "done": 0, "rejected": 0, "needs_human": 0,
                      "open": 0, "merged": 0, "cost": 0.0, "tokens": 0, "projects": 0}
            for p in d.list_projects():
                totals["projects"] += 1
                for i in d.list_issues(p["id"]):
                    totals["issues"] += 1
                    stage = i.get("stage") or "pending"
                    if stage in ("done", "rejected", "needs_human"):
                        totals[stage] += 1
                    else:
                        totals["open"] += 1
                    if i.get("merged"):
                        totals["merged"] += 1
                    totals["cost"] += float(i.get("cost") or 0)
                    totals["tokens"] += int(i.get("tokens") or 0)
                    c = (i.get("created_at") or "")[:10]
                    if c in per:
                        per[c]["created"] += 1
                    u = (i.get("updated_at") or "")[:10]
                    if u in per and stage in ("done", "rejected"):
                        per[u]["done"] += 1
                        per[u]["cost"] += float(i.get("cost") or 0)
                        per[u]["tokens"] += int(i.get("tokens") or 0)
            return self._json({"days": [per[k] for k in keys], "totals": totals})

        if path == "/api/sessions":
            return self._json({"sessions": state_mod._db().list_sessions_enriched()})

        if path == "/api/agent-limit":
            d = state_mod._db()
            return self._json({"limit": int(d.kv_get("agent_limit", 3) or 3),
                               "snapshot": GATE.snapshot()})

        if path == "/api/file":
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(query)
            p = (q.get("path") or [""])[0]
            if not self._safe_issue_file(os.path.join(BASE, p)) and not self._safe_issue_file(p):
                return self._json({"error": "forbidden"}, 403)
            full = p if os.path.isabs(p) else os.path.join(BASE, p)
            with open(full, encoding="utf-8", errors="replace") as f:
                return self._json({"path": p, "content": f.read()[:200000]})

        if path == "/api/prompts":
            files, customized = {}, []
            for st_ in prompts_cfg.STAGES:
                for kind in ("system", "user"):
                    fn = f"{st_}.{kind}.md"
                    files[fn] = prompts_cfg.pick_file(fn)
                    if prompts_cfg.is_customized(fn):
                        customized.append(fn)
            files["_shared.json"] = json.dumps(prompts_cfg.load()["shared"],
                                               ensure_ascii=False, indent=2)
            if prompts_cfg.is_customized("_shared.json"):
                customized.append("_shared.json")
            return self._json({"version": prompts_cfg.version(), "files": files,
                               "customized": customized,
                               "meta": prompts_cfg.load()["meta"].get("stages", {})})

        if path == "/api/env":
            env = load_env()
            token = env.get("BOT_TOKEN", "")
            masked = (token[:3] + "***" + token[-2:]) if len(token) > 6 else ("***" if token else "")
            lkey = env.get("LLM_API_KEY", "")
            lmasked = (lkey[:7] + "***" + lkey[-4:]) if len(lkey) > 14 else ("***" if lkey else "")
            gtok = env.get("GH_TOKEN", "")
            gmasked = (gtok[:7] + "***" + gtok[-4:]) if len(gtok) > 14 else ("***" if gtok else "")
            return self._json({"env": {"BOT_MODEL": env.get("BOT_MODEL", ""),
                                       "BOT_TOKEN": masked,
                                       "BOT_AUTH_ENABLED": env.get("BOT_AUTH_ENABLED", "1"),
                                       "LLM_API_BASE": env.get("LLM_API_BASE", ""),
                                       "LLM_API_KEY": lmasked,
                                       "LLM_MODEL": env.get("LLM_MODEL", ""),
                                       "GH_TOKEN": gmasked},
                               "auth_enabled_now": bool(load_cfg().get("auth_enabled", True)),
                               "note": "修改后需重启服务生效（launchd: bash scripts/install_autostart.sh）"})

        if path == "/api/log":
            from urllib.parse import parse_qs
            q = parse_qs(query)
            level = (q.get("level") or [""])[0]
            issue_f = (q.get("issue") or [""])[0]
            kw = (q.get("q") or [""])[0]
            try:
                limit = min(int((q.get("limit") or ["400"])[0]), 3000)
            except ValueError:
                limit = 400
            import glob as _g
            logs = sorted(_g.glob(os.path.join(BASE, "logs", "bot-*.jsonl")))[-7:]
            chunks = []
            for lf in logs:
                try:
                    with open(lf, encoding="utf-8", errors="replace") as f:
                        chunks.append(f.read())
                except OSError:
                    continue
            raw = "".join(chunks).splitlines()

            def keep(ln: str) -> bool:
                try:
                    d = json.loads(ln)
                except json.JSONDecodeError:
                    return True
                if level and d.get("level") != level:
                    return False
                if issue_f and str(d.get("issue")) != issue_f:
                    return False
                if kw and kw not in ln:
                    return False
                return True

            return self._json({"lines": [x for x in raw if keep(x)][-limit:]})

        return self._json({"error": "not found"}, 404)

    # --- POST ---
    def do_POST(self):
        path = self.path.partition("?")[0]
        if not self._authed():
            return self._json({"error": "unauthorized", "hint": "请在后台输入访问令牌"}, 401)
        body = self._body()
        if body is None:
            return self._json({"error": "malformed body"}, 400)
        cfg = load_cfg()
        st = state_mod.load()

        if path == "/api/mode":
            m = body.get("mode")
            if m not in state_mod.MODES:
                return self._json({"error": "bad mode"}, 400)
            state_mod._db().kv_set("global_mode", m)
            log_event("info", "web", "mode_change", mode=m,
                      log_dir=os.path.join(BASE, "logs"))
            return self._json({"ok": True, "global_mode": m})

        if path == "/api/interval":
            try:
                sec = int(body.get("sec", 600))
            except (TypeError, ValueError):
                return self._json({"error": "bad sec"}, 400)
            sec = max(0, sec)
            state_mod._db().kv_set("poll_interval_sec", sec)
            return self._json({"ok": True, "poll_interval_sec": sec})

        if path == "/api/cleanup-sessions":
            keys = body.get("keys") or []
            valid = {f"s{i}" for i in range(1, 7)}
            keys = [k for k in keys if k in valid]
            cfg["cleanup_sessions"] = keys
            save_cfg(cfg)
            return self._json({"ok": True, "cleanup_sessions": keys})

        if path == "/api/issue-mode":
            m = body.get("mode")
            repo = str(body.get("repo") or "").strip()
            if m not in ("default",) + state_mod.MODES:
                return self._json({"error": "bad mode"}, 400)
            try:
                n = int(body.get("issue"))
            except (TypeError, ValueError):
                return self._json({"error": "bad issue"}, 400)
            d = state_mod._db()
            target = repo or state_mod._compat_repo(d)
            project = d.get_project(target)
            if not project and repo and repo != cfg.get("repo"):
                return self._json({"error": "project not found"}, 404)
            project = project or d.get_or_add_project(target, "")
            d.upsert_issue(project["id"], n, mode=m)
            return self._json({"ok": True})

        if path == "/api/rerun":
            try:
                n = int(body.get("issue", 0))
            except (TypeError, ValueError):
                return self._json({"error": "bad issue"}, 400)
            if n <= 0:
                return self._json({"error": "bad issue"}, 400)
            repo = str(body.get("repo") or "").strip()
            project = resolve_project(cfg, repo) if repo else None
            if repo and project.get("repo") != repo:
                return self._json({"error": "project not found"}, 404)
            triggered = run_pipeline_once(cfg, n, project, rerun=True)
            log_event("info", "web", "rerun", issue=n,
                      log_dir=os.path.join(BASE, "logs"), triggered=triggered)
            return self._json({"ok": True, "triggered": triggered})

        if path == "/api/poll":
            # 在 handler 内抢先拿锁：拿不到就如实返回 busy，避免「假成功」
            if not poll_lock.acquire(blocking=False):
                log_event("info", "web", "poll_busy",
                          log_dir=os.path.join(BASE, "logs"))
                return self._json({"ok": False, "error": "poll_busy"})

            def _poll_job():
                try:
                    for proj in list_projects_cfg(cfg):
                        if proj.get("mode") == "paused":
                            continue   # 项目级暂停：连 gh list 调用都不发
                        pipe = pl.Pipeline(cfg, proj)
                        for x in pipe.poll():
                            run_pipeline_once(cfg, x, proj)
                        for x in pipe.check_waiting():
                            run_pipeline_once(cfg, x, proj)
                except Exception as e:
                    log_event("error", "web", "poll_error", error=str(e),
                              log_dir=os.path.join(BASE, "logs"))
                finally:
                    poll_lock.release()
            try:
                threading.Thread(target=_poll_job, daemon=True).start()
            except Exception:
                poll_lock.release()  # 线程起不来时归还锁，避免永久 poll_busy
                return self._json({"ok": False, "error": "poll_start_failed"}, 500)
            return self._json({"ok": True, "hint": "polling started in background"})

        if path == "/api/prompts":
            name = body.get("name", "")
            content = body.get("content", "")
            try:
                if name == "_shared.json":
                    ver = prompts_cfg.save_shared(json.loads(content))
                elif name.endswith((".system.md", ".user.md")):
                    stage = name.split(".")[0]
                    kind = "system" if name.endswith(".system.md") else "user"
                    ver = prompts_cfg.save_stage_file(stage, kind, content)
                else:
                    return self._json({"error": "bad name"}, 400)
                return self._json({"ok": True, "version": ver})
            except (json.JSONDecodeError, ValueError) as e:
                return self._json({"error": str(e)}, 400)

        if path == "/api/prompts/reset":
            prompts_cfg.reset_to_defaults()
            return self._json({"ok": True, "version": prompts_cfg.version()})

        if path == "/api/env/save":
            env = load_env()
            for k in ("BOT_MODEL", "LLM_API_BASE", "LLM_MODEL"):
                if k in body:
                    env[k] = str(body[k]).strip()
            for k in ("BOT_TOKEN", "LLM_API_KEY", "GH_TOKEN"):  # 打码值不覆盖
                if k in body:
                    t = str(body[k]).strip()
                    if t and "*" not in t:
                        env[k] = t
            env["BOT_AUTH_ENABLED"] = "1" if body.get("BOT_AUTH_ENABLED") in ("1", True, "true") else "0"
            save_env(env)
            log_event("info", "web", "env_saved", log_dir=os.path.join(BASE, "logs"))
            return self._json({"ok": True, "restart_required": True})

        if path == "/api/settings":
            cfg = load_cfg()
            if "disclosure_enabled" in body:
                cfg["disclosure_enabled"] = bool(body["disclosure_enabled"])
            if "repo_intro" in body:
                cfg["repo_intro"] = str(body["repo_intro"]).strip()
            if "oc_variant" in body:
                v = str(body["oc_variant"]).strip()
                if v in ("", "low", "high", "max"):
                    cfg["oc_variant"] = v
            save_cfg(cfg)
            log_event("info", "web", "settings_saved", log_dir=os.path.join(BASE, "logs"))
            return self._json({"ok": True})

        if path == "/api/publish":
            try:
                n = int(body.get("issue", 0))
            except (TypeError, ValueError):
                return self._json({"error": "bad issue"}, 400)
            if n <= 0:
                return self._json({"error": "bad issue"}, 400)
            repo = str(body.get("repo") or "").strip()
            # 防重入按 (repo, n) 精确匹配：同号跨项目不互挡；补发任务自己也注册
            key_repo = (resolve_project(cfg, repo).get("repo") or repo
                        or cfg.get("repo") or "")
            key = f"{key_repo}#{n}"
            with _running_lock:
                # 前缀互斥：主流程/publish/merge-fix 任一在跑都拒绝——共享
                # issue-{n} 沙箱目录，并发会互相 rmtree
                if any(k == key or k.startswith(key + "#") for k in _running):
                    return self._json({"ok": False, "error": "该 issue 正在处理中，稍后再试"}, 409)
                _running[f"{key}#pub"] = {"repo": key_repo, "n": n,
                                          "started": time.time(), "kind": "publish"}
            _set_issue_fields(cfg, repo, n, publish_status="running", publish_result=None)
            log_event("info", "web", "publish_start", issue=n,
                      log_dir=os.path.join(BASE, "logs"))

            def _pub_job():
                try:
                    r = pl.Pipeline(cfg, resolve_project(cfg, repo)).publish(n)
                except Exception as ex:
                    r = {"ok": False, "error": f"{type(ex).__name__}: {ex}", "steps": []}
                finally:
                    with _running_lock:
                        _running.pop(f"{key}#pub", None)
                _set_issue_fields(cfg, repo, n,
                                  publish_status="done" if r.get("ok") else "error",
                                  publish_result=r)
                log_event("info", "web", "publish_finished", issue=n,
                          log_dir=os.path.join(BASE, "logs"), ok=bool(r.get("ok")))

            threading.Thread(target=_pub_job, daemon=True).start()
            return self._json({"ok": True, "hint": "publishing started in background"})

        if path == "/api/merge-fix":
            try:
                n = int(body.get("issue", 0))
            except (TypeError, ValueError):
                return self._json({"error": "bad issue"}, 400)
            if n <= 0:
                return self._json({"error": "bad issue"}, 400)
            repo = str(body.get("repo") or "").strip()
            key_repo = (resolve_project(cfg, repo).get("repo") or repo
                        or cfg.get("repo") or "")
            key = f"{key_repo}#{n}"
            with _running_lock:
                # 前缀互斥：主流程/publish/merge-fix 任一在跑都拒绝——共享
                # issue-{n} 沙箱目录，并发会互相 rmtree
                if any(k == key or k.startswith(key + "#") for k in _running):
                    return self._json({"ok": False, "error": "该 issue 正在处理中，稍后再试"}, 409)
                _running[f"{key}#mf"] = {"repo": key_repo, "n": n,
                                         "started": time.time(), "kind": "merge_fix"}
            _set_issue_fields(cfg, repo, n, merge_fix_status="running", merge_fix_result=None)
            log_event("info", "web", "merge_fix_start", issue=n,
                      log_dir=os.path.join(BASE, "logs"))

            def _mf_job():
                try:
                    r = pl.Pipeline(cfg, resolve_project(cfg, repo)).merge_fix(n)
                except Exception as ex:
                    r = {"ok": False, "error": f"{type(ex).__name__}: {ex}", "steps": []}
                finally:
                    with _running_lock:
                        _running.pop(f"{key}#mf", None)
                _set_issue_fields(cfg, repo, n,
                                  merge_fix_status="done" if r.get("ok") else "error",
                                  merge_fix_result=r)
                log_event("info", "web", "merge_fix_finished", issue=n,
                          log_dir=os.path.join(BASE, "logs"), ok=bool(r.get("ok")))

            threading.Thread(target=_mf_job, daemon=True).start()
            return self._json({"ok": True, "hint": "merge-fix started in background"})

        if path == "/api/release/run":
            repo = str(body.get("repo") or "").strip()
            ps = list_projects_cfg(cfg)
            proj = next((p for p in ps if p.get("repo") == repo), None) \
                if repo else (ps[0] if ps else None)
            if not proj:
                return self._json({"ok": False, "error": "项目不存在"}, 404)
            d = state_mod._db()
            # 总开关：全局/项目 paused 拒绝手动触发（run_release 内还有兜底守卫）
            if d.kv_get("global_mode") == "paused" or proj.get("mode") == "paused":
                return self._json({"ok": False,
                                   "error": "已暂停（paused）：恢复档位后再触发发布"}, 409)
            # 原子占位（CAS）：check-then-set 会让调度器与手动触发并发双起发布
            if not d.kv_cas("release_running", "", proj["repo"]):
                return self._json({"ok": False, "error": "已有发布任务在运行"}, 409)
            log_event("info", "web", "release_start", repo=proj["repo"],
                      log_dir=os.path.join(BASE, "logs"))

            def _rel_job():
                try:
                    release_mod.run_release(proj, cfg, state_mod._db(),
                                            trigger="manual",
                                            log_dir=os.path.join(BASE, "logs"))
                finally:
                    state_mod._db().kv_set("release_running", "")

            threading.Thread(target=_rel_job, daemon=True).start()
            return self._json({"ok": True, "hint": "release started in background"})

        if path == "/api/release/cfg":
            repo = str(body.get("repo") or "").strip()
            rcfg = body.get("release_cfg") or {}
            ps = list_projects_cfg(cfg)
            proj = next((p for p in ps if p.get("repo") == repo), None) \
                if repo else (ps[0] if ps else None)
            if not proj or not proj.get("repo"):
                return self._json({"ok": False, "error": "项目不存在"}, 404)
            merged = dict(release_mod.get_release_cfg(proj))
            for k in ("time_enabled", "interval_hours", "count_enabled",
                      "count_threshold", "mode"):
                if k in rcfg:
                    merged[k] = rcfg[k]
            state_mod._db().update_project(proj["repo"], release_cfg=merged)
            log_event("info", "web", "release_cfg_saved", repo=proj["repo"],
                      log_dir=os.path.join(BASE, "logs"))
            return self._json({"ok": True, "release_cfg": merged})

        if path == "/api/init-project":
            repo = str(body.get("repo") or "").strip()
            landing = str(body.get("landing") or "pr")
            if landing not in ("main", "pr", "pr_auto"):
                return self._json({"ok": False,
                                   "error": "landing 必须为 main/pr/pr_auto"}, 400)
            ps = list_projects_cfg(cfg)
            proj = next((p for p in ps if p.get("repo") == repo), None) \
                if repo else (ps[0] if ps else None)
            if not proj:
                return self._json({"ok": False, "error": "项目不存在"}, 404)
            d = state_mod._db()
            # 总开关：paused 下不起初始化 agent（init_project 内还有兜底守卫）
            if d.kv_get("global_mode") == "paused" or proj.get("mode") == "paused":
                return self._json({"ok": False,
                                   "error": "已暂停（paused）：恢复档位后再初始化"}, 409)
            key = f"init_status::{proj['repo']}"
            # 原子占位（CAS）：连点/并发只放行一个初始化任务
            if not d.kv_cas(key, ["", "done", "error"], "running"):
                return self._json({"ok": False, "error": "初始化已在进行中"}, 409)
            d.kv_set(f"init_result::{proj['repo']}", None)
            log_event("info", "web", "init_start", repo=proj["repo"],
                      landing=landing, log_dir=os.path.join(BASE, "logs"))

            def _init_job():
                try:
                    r = pl.Pipeline(cfg, proj).init_project(landing=landing)
                except Exception as ex:
                    r = {"ok": False, "error": f"{type(ex).__name__}: {ex}", "steps": []}
                dd = state_mod._db()
                dd.kv_set(f"init_result::{proj['repo']}", r)
                dd.kv_set(key, "done" if r.get("ok") else "error")
                log_event("info", "web", "init_finished", repo=proj["repo"],
                          log_dir=os.path.join(BASE, "logs"), ok=bool(r.get("ok")))

            threading.Thread(target=_init_job, daemon=True).start()
            return self._json({"ok": True, "hint": "init started in background"})

        if path == "/api/wizard/complete":
            state_mod._db().kv_set("wizard_done", "1")
            return self._json({"ok": True})

        if path == "/api/wizard/reset":
            state_mod._db().kv_set("wizard_done", "")
            return self._json({"ok": True})

        if path == "/api/projects/add":
            repo = str(body.get("repo") or "").strip()
            if not valid_repo_name(repo):
                return self._json({"ok": False, "error": "repo 需为 owner/name 格式"}, 400)
            d = state_mod._db()
            if d.get_project(repo):
                return self._json({"ok": False, "error": "项目已存在"}, 409)
            repo_url = str(body.get("repo_url") or f"https://github.com/{repo}.git").strip()
            # repo_url/main_branch 会进 git argv 位置：- 开头 = 选项注入（RCE 通道）
            if not valid_repo_url(repo_url):
                return self._json({"ok": False, "error": "repo_url 格式非法"}, 400)
            kwargs = {}
            if body.get("intro"):
                kwargs["intro"] = str(body["intro"])
            if body.get("main_branch"):
                mb = str(body["main_branch"])
                if not valid_ref_name(mb):
                    return self._json({"ok": False, "error": "main_branch 非法"}, 400)
                kwargs["main_branch"] = mb
            if isinstance(body.get("build_cmd"), list):
                kwargs["build_cmd"] = body["build_cmd"]
            if isinstance(body.get("build_env"), dict):
                kwargs["build_env"] = body["build_env"]
            try:
                d.add_project(repo, repo_url, mode="default", **kwargs)
            except Exception:
                return self._json({"ok": False, "error": "项目已存在"}, 409)
            log_event("info", "web", "project_added", repo=repo,
                      log_dir=os.path.join(BASE, "logs"))
            return self._json({"ok": True, "repo": repo})

        if path == "/api/project/update":
            repo = str(body.get("repo") or "").strip()
            d = state_mod._db()
            if not repo or not d.get_project(repo):
                return self._json({"ok": False, "error": "项目不存在"}, 404)
            fields = {}
            if "intro" in body:
                fields["intro"] = str(body["intro"])
            if "main_branch" in body:
                mb = str(body["main_branch"])
                if not valid_ref_name(mb):
                    return self._json({"ok": False, "error": "main_branch 非法"}, 400)
                fields["main_branch"] = mb
            if "mode" in body and body["mode"] in ("default",) + state_mod.MODES:
                fields["mode"] = body["mode"]
            if isinstance(body.get("build_cmd"), list):
                fields["build_cmd"] = body["build_cmd"]
            if isinstance(body.get("build_env"), dict):
                fields["build_env"] = body["build_env"]
            if "agent_limit" in body:
                try:
                    fields["agent_limit"] = max(0, min(int(body["agent_limit"]), 99))
                except (TypeError, ValueError):
                    pass
            if fields:
                d.update_project(repo, **fields)
            log_event("info", "web", "project_updated", repo=repo,
                      log_dir=os.path.join(BASE, "logs"))
            return self._json({"ok": True, "project": d.get_project(repo)})

        if path == "/api/sessions/delete":
            sids = [str(s) for s in (body.get("session_ids") or [])][:200]
            d = state_mod._db()
            out = []
            for sid in sids:
                try:
                    ok = llm.delete_session(sid)
                except Exception:
                    ok = False
                try:
                    d.delete_session_row(sid)
                except Exception:
                    pass
                out.append({"session_id": sid, "oc2_deleted": bool(ok)})
            log_event("info", "web", "sessions_deleted", count=len(out),
                      log_dir=os.path.join(BASE, "logs"))
            return self._json({"ok": True, "deleted": out})

        if path == "/api/agent-limit":
            try:
                lim = int(body.get("limit") or 3)
            except (TypeError, ValueError):
                return self._json({"error": "bad limit"}, 400)
            lim = max(1, min(lim, 99))
            state_mod._db().kv_set("agent_limit", lim)
            GATE.set_limit(lim)
            log_event("info", "web", "agent_limit_set", limit=lim,
                      log_dir=os.path.join(BASE, "logs"))
            return self._json({"ok": True, "limit": lim})

        return self._json({"error": "not found"}, 404)


def main():
    ensure_path()
    _env = load_env()
    if _env.get("GH_TOKEN"):
        os.environ["GH_TOKEN"] = _env["GH_TOKEN"]  # gh CLI 全局生效（无 gh auth 的机器借此认证）
    cfg = load_cfg()
    # git/gh 代理（GFW 场景：github 直连被阻断时经本机代理走外网）
    sandbox.set_proxy(str(cfg.get("git_proxy", "") or ""))
    if sandbox._PROXY:
        _px = sandbox._PROXY
        os.environ["HTTPS_PROXY"] = _px    # gh / opencode2 / urllib 调用时继承
        os.environ["HTTP_PROXY"] = _px
        os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
        log_event("info", "bot", "git_proxy", log_dir=os.path.join(BASE, "logs"),
                  proxy=_px, covers="git+gh+llm")
        print(f"  git/gh 代理: {_px}")
    os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)
    token = get_web_token(cfg)
    Handler.token = token
    Handler.auth_enabled = bool(cfg.get("auth_enabled", True))
    lock = state_mod.InstanceLock()
    if not lock.acquire():
        print("已有一个 bot 实例在运行（flock），退出。")
        return
    # 启动即清发布锁：进程若在发布中途退出（launchd 重启/断电），
    # 残留的 release_running 会让手动发布永远 409（InstanceLock 保证单实例，清了安全）
    try:
        state_mod._db().kv_set("release_running", "")
    except Exception:
        pass
    try:
        mig = state_mod.migrate_if_needed(cfg, os.path.join(BASE, "logs"))
        print(f"  数据迁移（v2→v3）: {mig}")
    except Exception as e:
        mig = f"error: {e}"
        print(f"  ⚠️ 数据迁移异常（继续启动）: {e}")
    state_mod.recover_interrupted(os.path.join(BASE, "logs"))
    removed = cleanup_logs(int(cfg.get("log_retention_days", 30)), os.path.join(BASE, "logs"))
    log_event("info", "bot", "started", log_dir=os.path.join(BASE, "logs"),
              model=cfg.get("model"), repo=cfg.get("repo"), migration=mig,
              auth_enabled=Handler.auth_enabled, logs_removed=removed)
    limit = apply_agent_limit(cfg)
    print(f"  agent 并发上限: {limit}（可在后台设置，1-99）")
    threading.Thread(target=scheduler_loop, daemon=True).start()
    host = str(cfg.get("web_host", "127.0.0.1"))
    port = int(cfg.get("web_port", 8770))
    print(f"issue-triage-bot 后台已启动（模型: {cfg.get('model')}）")
    for ip in ([host] if host not in ("0.0.0.0", "::") else lan_ips()):
        print(f"  后台地址: http://{ip}:{port}")
    if token:
        print(f"  访问令牌: {token}   （已存 {TOKEN_PATH}，请勿外传）")
        print(f"  局域网访问已开启——令牌是唯一防线，切勿端口转发到公网。")
    else:
        print("  ⚠️ 鉴权已关闭（BOT_AUTH_ENABLED=0）——局域网内任何人可操作，请勿暴露到不可信网络。")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
