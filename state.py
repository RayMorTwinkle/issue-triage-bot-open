"""运行状态（v3：SQLite 后端，保留 v2 API 兼容层）。

- 数据库文件：<STATE_DIR>/bot.db（随 STATE_DIR 走，测试可隔离）
- 本模块保留 v2 的 load()/save()/issue_entry()/effective_mode()/bump_retry()/InstanceLock
  接口（单项目视角兼容层）；多项目细粒度 API 见 db.py（P2 起逐步切换）。
- v2 → v3 迁移：migrate_if_needed(cfg, log_dir)（服务启动时调用，幂等）：
  state.json → SQLite（备份旧文件）+ 档案目录搬迁 logs/issues/* → logs/projects/<slug>/issues/*
"""
import fcntl
import glob as _glob
import os
import shutil
import sqlite3
import threading
import time

import db as db_mod
from util import load_json
from workflow import MODES, resolve_mode

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
STATE_PATH = os.path.join(STATE_DIR, "state.json")   # v2 遗留（仅作迁移源）
LOCK_PATH = os.path.join(STATE_DIR, "bot.lock")
PID_PATH = os.path.join(STATE_DIR, "bot.pid")

STAGES = ("pending", "audit", "triage", "waiting_info", "fix", "gate", "review", "final",
          "reply", "execute", "done", "rejected", "needs_human")

_initialized = set()
_lock = threading.RLock()   # 进程内并发保护（多 issue 并行时）


def project_slug(repo: str) -> str:
    """owner/name → owner__name（档案目录名/URL 友好）。"""
    return (repo or "").replace("/", "__")


def _db_path() -> str:
    return os.path.join(STATE_DIR, "bot.db")


def _db() -> "db_mod.DB":
    with _lock:   # init 串行化：并发首调同库时 ALTER 只跑一次（否则 duplicate column）
        d = db_mod.DB(_db_path())
        if d.path not in _initialized:
            d.init()
            _initialized.add(d.path)
        return d


# ---------------- v2 兼容层 ----------------

def default_state() -> dict:
    return {
        "global_mode": "dry",
        "poll_interval_sec": 600,          # 0 = 手动
        "issues": {},
    }


def _compat_repo(d: "db_mod.DB" = None) -> str:
    """兼容层视角的"当前项目"：优先 kv 标记，其次第一个项目，
    都没有时用保底项目 '_default'（测试/首启场景）。"""
    d = d or _db()
    r = d.kv_get("compat_project")
    if r:
        return r
    ps = d.list_projects()
    if ps:
        return ps[0]["repo"]
    return "_default"


def _entry_to_fields(e: dict) -> dict:
    """v2 entry dict → issues 表字段（JSON 字段交给 db 层序列化）。"""
    f = {}
    for k in ("title", "stage", "complexity", "mode", "summary", "error",
              "pr_url", "sandbox_dir", "branch", "paused_from",
              "publish_status", "merge_fix_status"):
        if k in e and e[k] is not None:
            f[k] = e[k]
    for k in ("verdict", "audit", "fix_json", "review_json", "final_json",
              "retries", "history", "session_ids", "direct_calls",
              "publish_result", "merge_fix_result"):
        if k in e and e[k] is not None:
            f[k] = e[k]
    if "merged" in e:
        f["merged"] = 1 if e.get("merged") else 0
    if "patch_saved" in e:
        f["patch_saved"] = 1 if e.get("patch_saved") else 0
    if "info_posted" in e:
        f["info_posted"] = 1 if e.get("info_posted") else 0
    if "info_seen_comments" in e:
        f["info_seen_comments"] = int(e.get("info_seen_comments") or 0)
    if "cost" in e:
        f["cost"] = float(e.get("cost") or 0)
    if "tokens" in e:
        f["tokens"] = int(e.get("tokens") or 0)
    return f


def _row_to_entry(row: dict) -> dict:
    """issues 表行 → v2 entry dict。"""
    e = {k: row.get(k) for k in
         ("title", "stage", "complexity", "mode", "summary", "error",
          "pr_url", "sandbox_dir", "branch", "paused_from",
          "publish_status", "merge_fix_status",
          "verdict", "audit", "fix_json", "review_json", "final_json",
          "publish_result", "merge_fix_result")}
    e["merged"] = bool(row.get("merged"))
    e["patch_saved"] = bool(row.get("patch_saved"))
    _seen = row.get("info_seen_comments")
    e["info_seen_comments"] = int(_seen) if _seen is not None else -1   # -1=基线未知
    e["info_posted"] = bool(row.get("info_posted"))
    e["cost"] = float(row.get("cost") or 0)
    e["tokens"] = int(row.get("tokens") or 0)
    e["retries"] = row.get("retries") or {}
    e["history"] = row.get("history") or []
    e["session_ids"] = row.get("session_ids") or {}
    e["direct_calls"] = row.get("direct_calls") or []
    e["updated_at"] = row.get("updated_at")
    if not e.get("mode"):
        e["mode"] = "default"
    if not e.get("stage"):
        e["stage"] = "pending"
    if not e.get("title"):
        e["title"] = ""
    return e


def load() -> dict:
    """v2 兼容：返回 {global_mode, poll_interval_sec, issues: {str(n): entry}}（单项目视角）。"""
    with _lock:
        d = _db()
        st = {"global_mode": d.kv_get("global_mode", "dry"),
              "poll_interval_sec": int(d.kv_get("poll_interval_sec", 600) or 600),
              "issues": {}}
        repo = _compat_repo(d)
        p = d.get_project(repo)
        if p:
            for row in d.list_issues(p["id"]):
                st["issues"][str(row["number"])] = _row_to_entry(row)
        return st


def save(st: dict) -> None:
    """v2 兼容：全量提交（仅 issues upsert；不删除不在 st 中的记录）。

    不写 global_mode / poll_interval_sec——调用方持有的 st 可能是数小时前的
    快照，盲写会把用户在运行中改的档位/间隔静默回滚（lost update）。
    全局值只由 /api/mode、/api/interval 的显式 kv_set 与迁移写入。"""
    with _lock:
        d = _db()
        repo = _compat_repo(d)
        # 保底项目（首启/测试）：自动创建，保证 roundtrip 不丢数据
        p = d.get_or_add_project(repo, "")
        for k, e in (st.get("issues") or {}).items():
            try:
                d.save_issue_full(p["id"], int(k), _entry_to_fields(e),
                                  e.get("session_ids"), e.get("stage"))
            except (ValueError, TypeError, sqlite3.Error):
                continue


def save_issue(st: dict, n, repo: str = None, *, worker=False) -> None:
    """细粒度保存：只提交 issue n 的行（多 issue 并发安全）。

    - repo 指定项目（多项目隔离）；缺省=兼容项目（旧调用方不变）
    - 多 issue 并行处理时使用本方法（各自只 touch 自己的行，不会 last-wins 互踩）
    - 不写 global_mode / poll_interval_sec（st 是长任务快照，盲写会回滚用户改档）
    """
    with _lock:
        d = _db()
        r = repo or _compat_repo(d)
        if repo and not d.kv_get("compat_project"):
            d.kv_set("compat_project", repo)   # 首个明确项目自动成为兼容视角
        p = d.get_or_add_project(r, "")
        e = (st.get("issues") or {}).get(str(n))
        if e is not None:
            fields = _entry_to_fields(e)
            if worker:
                # Operator-owned column. Never overwrite a concurrent downgrade.
                fields.pop("mode", None)
                for nullable in ("paused_from", "error"):
                    if nullable in e:
                        fields[nullable] = e[nullable]
            d.save_issue_full(p["id"], int(n), fields, e.get("session_ids"), e.get("stage"))


def load_project(repo: str) -> dict:
    """按【项目】加载状态（多项目隔离）：issues 来自该项目行；global_mode 等为全局。"""
    with _lock:
        d = _db()
        st = {"global_mode": d.kv_get("global_mode", "dry"),
              "poll_interval_sec": int(d.kv_get("poll_interval_sec", 600) or 600),
              "issues": {}}
        p = d.get_project(repo) if repo else None
        if p:
            for row in d.list_issues(p["id"]):
                st["issues"][str(row["number"])] = _row_to_entry(row)
        return st


def save_project(st: dict, repo: str) -> None:
    """按【项目】全量提交（poll 等批量场景；多项目隔离）。

    不写 global_mode / poll_interval_sec（理由同 save()）。"""
    with _lock:
        d = _db()
        if repo and not d.kv_get("compat_project"):
            d.kv_set("compat_project", repo)
        p = d.get_or_add_project(repo or "", "")
        for k, e in (st.get("issues") or {}).items():
            try:
                d.save_issue_full(p["id"], int(k), _entry_to_fields(e),
                                  e.get("session_ids"), e.get("stage"))
            except (ValueError, TypeError, sqlite3.Error):
                continue


def issue_entry(st: dict, n: int) -> dict:
    """取（并按需初始化）某 issue 的状态条目（内存逻辑，与 v2 一致）。"""
    key = str(n)
    if key not in st["issues"]:
        st["issues"][key] = {
            "mode": "default",             # default=跟随 | dry | semi | full | yolo | paused
            "stage": "pending",
            "session_ids": {},             # s1..s5 / review-*
            "retries": {},                 # 阶段级重试计数
            "verdict": None,
            "sandbox_dir": None,
            "branch": None,
            "title": "",
            "updated_at": None,
            "error": None,
        }
    return st["issues"][key]


def effective_mode(st: dict, entry: dict, project_mode: str = None) -> str:
    """三层档位：per-issue 覆盖 > 项目档位 > 全局默认。"""
    return resolve_mode(st.get("global_mode"), project_mode, entry.get("mode"))


def bump_retry(entry: dict, key: str) -> int:
    """重试计数 +1，返回新计数。超限判断由调用方负责。"""
    c = entry["retries"].get(key, 0) + 1
    entry["retries"][key] = c
    return c


# ---------------- v2 → v3 迁移 ----------------

def migrate_if_needed(cfg: dict, log_dir: str = None) -> str:
    """v2 → v3 一次性迁移（幂等）：
    1) config 里的项目 → projects 表（配置同步）
    2) state.json → SQLite（含 kv），旧文件备份
    3) 档案目录搬迁 logs/issues/* → logs/projects/<slug>/issues/*（含 run-*.jsonl）
    在服务启动时调用（此时无运行中任务）。返回摘要字符串。
    """
    d = _db()
    if d.kv_get("migrated_v3"):
        return "already"
    repo = (cfg.get("repo") or "").strip()
    if not repo:
        # 未配置项目时不置 migrated_v3：遗留 state.json 留待配好项目后的启动再迁，
        # 否则旗标先行会把旧数据永久关在门外。
        return "pending: no repo configured"
    summary = []
    if repo:
        p = d.get_project(repo)
        if not p:
            d.add_project(repo, cfg.get("repo_url", ""),
                          intro=cfg.get("repo_intro", ""),
                          mode="default",
                          build_cmd=cfg.get("build_cmd", []),
                          build_env=cfg.get("build_env", {}),
                          main_branch=cfg.get("main_branch", "main"))
            summary.append("project created")
        else:
            d.update_project(repo, build_cmd=cfg.get("build_cmd", []),
                             build_env=cfg.get("build_env", {}))
        d.kv_set("compat_project", repo)

    # 2) state.json → db
    legacy = load_json(STATE_PATH, None)
    if isinstance(legacy, dict) and repo:
        p = d.get_project(repo)
        n_mig = 0
        for k, e in (legacy.get("issues") or {}).items():
            try:
                d.upsert_issue(p["id"], int(k), **_entry_to_fields(e))
                n_mig += 1
            except (ValueError, TypeError, sqlite3.Error):
                continue
        if legacy.get("global_mode") and d.kv_get("global_mode") is None:
            d.kv_set("global_mode", legacy["global_mode"])
        if legacy.get("poll_interval_sec") is not None and d.kv_get("poll_interval_sec") is None:
            d.kv_set("poll_interval_sec", int(legacy["poll_interval_sec"]))
        if n_mig:
            summary.append(f"state.json: {n_mig} issues migrated")
        try:
            bk = STATE_PATH.replace(".json", f"-migrated-v2-{int(time.time())}.json")
            shutil.move(STATE_PATH, bk)   # 退休而非复制：避免旧文件被再次当作数据源
            summary.append("state.json retired")
        except OSError:
            pass

    # 3) 档案目录搬迁
    if log_dir and repo:
        slug = project_slug(repo)
        old = os.path.join(log_dir, "issues")
        new = os.path.join(log_dir, "projects", slug, "issues")
        n_mv = 0
        if os.path.isdir(old):
            os.makedirs(new, exist_ok=True)
            for name in os.listdir(old):
                src = os.path.join(old, name)
                dst = os.path.join(new, name)
                if os.path.isdir(src) and not os.path.exists(dst):
                    shutil.move(src, dst)
                    n_mv += 1
        if n_mv:
            summary.append(f"archives moved: {n_mv}")
        # run-*.jsonl 也搬进项目目录
        dst_dir = os.path.join(log_dir, "projects", slug)
        os.makedirs(dst_dir, exist_ok=True)
        for f in _glob.glob(os.path.join(log_dir, "run-*.jsonl")):
            base = os.path.basename(f)
            dst = os.path.join(dst_dir, base)
            if not os.path.exists(dst):
                shutil.move(f, dst)

    d.kv_set("migrated_v3", "1")
    return "; ".join(summary) if summary else "ok"


def recover_interrupted(log_dir=None):
    """Reconcile orphaned work after the instance lock is acquired, before scheduling."""
    from util import log_event
    d = _db()
    for project in d.list_projects():
        for row in d.list_issues(project["id"]):
            stage = row["stage"]
            fields = {}
            if stage in ("fix", "gate", "review", "final", "reply"):
                fields.update(stage="pending", paused_from=stage)
            elif stage in ("audit", "triage", "execute"):
                # These stages can post comments or create a PR before checkpointing.
                fields.update(stage="needs_human", error=f"{stage} 阶段中断，请核对远端记录后重跑")
            for operation in ("publish", "merge_fix"):
                if row.get(operation + "_status") == "running":
                    fields[operation + "_status"] = "error"
                    fields[operation + "_result"] = {"ok": False, "error": "进程中断，请核对远端状态"}
            if fields:
                d.upsert_issue(project["id"], row["number"], **fields)
                log_event("warn", "pipeline", "interrupted_recovered", issue=row["number"],
                          log_dir=log_dir, repo=project["repo"], previous=stage,
                          stage=fields.get("stage", stage))


class InstanceLock:
    """flock 单实例锁（阻塞前先探测，避免后台重复起）。"""

    def __init__(self):
        self._fh = None

    def acquire(self, blocking: bool = False) -> bool:
        os.makedirs(STATE_DIR, exist_ok=True)
        self._fh = open(LOCK_PATH, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | (fcntl.LOCK_NB if not blocking else 0))
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        with open(PID_PATH, "w") as f:
            f.write(str(os.getpid()))
        return True

    def release(self):
        if self._fh:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
                try:
                    os.remove(PID_PATH)
                except OSError:
                    pass
