"""SQLite 数据层：多项目托管的关联存储（零依赖，python 内置 sqlite3）。

- 数据库文件：<STATE_DIR>/bot.db（路径由 state.py 提供，测试可隔离）
- WAL 模式 + busy_timeout；每操作短连接（线程安全）
- schema 版本：PRAGMA user_version（当前 1）
- v2 → v3 迁移在 state.py 的 migrate_if_needed 中编排（本模块提供原语）

表：projects / issues / sessions / prs / branches / runs / releases / kv
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

SCHEMA_VERSION = 3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT UNIQUE NOT NULL,
  repo_url TEXT NOT NULL,
  intro TEXT DEFAULT '',
  mode TEXT DEFAULT 'default',
  agent_limit INTEGER DEFAULT 0,
  build_cmd TEXT DEFAULT '[]',
  build_env TEXT DEFAULT '{}',
  main_branch TEXT DEFAULT 'main',
  oc_variant TEXT DEFAULT '',
  release_cfg TEXT DEFAULT '{}',
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  number INTEGER NOT NULL,
  title TEXT DEFAULT '',
  stage TEXT DEFAULT 'pending',
  complexity TEXT,
  mode TEXT DEFAULT 'default',
  verdict TEXT, audit TEXT, fix_json TEXT, review_json TEXT, final_json TEXT,
  summary TEXT DEFAULT '',
  error TEXT,
  pr_url TEXT,
  merged INTEGER DEFAULT 0,
  cost REAL DEFAULT 0,
  tokens INTEGER DEFAULT 0,
  retries TEXT DEFAULT '{}',
  history TEXT DEFAULT '[]',
  session_ids TEXT DEFAULT '{}',
  direct_calls TEXT DEFAULT '[]',
  sandbox_dir TEXT, branch TEXT,
  paused_from TEXT, patch_saved INTEGER DEFAULT 0,
  info_posted INTEGER DEFAULT 0, info_seen_comments INTEGER DEFAULT 0,
  publish_status TEXT, publish_result TEXT,
  merge_fix_status TEXT, merge_fix_result TEXT,
  created_at TEXT, updated_at TEXT,
  UNIQUE(project_id, number)
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER, issue_id INTEGER,
  key TEXT, session_id TEXT UNIQUE,
  stage TEXT, status TEXT DEFAULT 'active',
  tokens INTEGER DEFAULT 0,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS prs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER, issue_id INTEGER,
  number INTEGER, url TEXT, branch TEXT,
  state TEXT DEFAULT 'open', merged_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS branches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER, issue_id INTEGER,
  name TEXT, pushed_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER, issue_id INTEGER,
  stage TEXT, ok INTEGER, detail TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS releases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER, tag TEXT, url TEXT,
  trigger TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

# issues 表里的 JSON 字段（读写自动序列化/解析）
ISSUE_JSON_FIELDS = ("verdict", "audit", "fix_json", "review_json", "final_json",
                     "retries", "history", "session_ids", "direct_calls",
                     "publish_result", "merge_fix_result")
PROJECT_JSON_FIELDS = ("build_cmd", "build_env", "release_cfg")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _dumps(v):
    return json.dumps(v, ensure_ascii=False)


def _loads(v, default):
    if v is None or v == "":
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return default


class DB:
    def __init__(self, path: str):
        self.path = path

    # ---------------- 连接 ----------------
    @contextmanager
    def _conn(self):
        """连接上下文：进入=开连接，事务成功提交/异常回滚，退出=【确定性 close】。

        注意：sqlite3.Connection 自带的 with 是事务语义、不关连接——连接若留给
        GC 兜底，长期高频轮询下会 fd 泄漏（每连接挂 db+wal 两个 fd，耗尽
        launchd 默认 256 上限 → 调度线程崩溃 + HTTP 全部 reset，
        2026-09-12 生产事故）。连接必须在此显式关闭。"""
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=10000")
        try:
            with c:            # 事务边界（原语义不变）
                yield c
        finally:
            c.close()          # fd 不留给 GC

    def init(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA_SQL)
            # schema 演进：缺列则补（幂等；新库建表已含全部列）
            cols = self._columns(c, "issues")
            if cols and "paused_from" not in cols:
                c.execute("ALTER TABLE issues ADD COLUMN paused_from TEXT")
            if cols and "patch_saved" not in cols:
                c.execute("ALTER TABLE issues ADD COLUMN patch_saved INTEGER DEFAULT 0")
            if cols and "info_posted" not in cols:
                c.execute("ALTER TABLE issues ADD COLUMN info_posted INTEGER DEFAULT 0")
            if cols and "info_seen_comments" not in cols:
                c.execute("ALTER TABLE issues ADD COLUMN info_seen_comments INTEGER DEFAULT 0")
            for col in ("publish_status", "publish_result",
                        "merge_fix_status", "merge_fix_result"):
                if cols and col not in cols:
                    c.execute(f"ALTER TABLE issues ADD COLUMN {col} TEXT")
            c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _columns(c, table: str) -> set:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}

    # ---------------- kv ----------------
    def kv_get(self, k: str, default=None):
        with self._conn() as c:
            row = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        if row is None:
            return default
        return _loads(row["v"], row["v"])

    def kv_set(self, k: str, v) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO kv(k,v) VALUES(?,?) "
                      "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                      (k, _dumps(v)))

    def kv_cas(self, k: str, expected, new) -> bool:
        """原子 compare-and-set：当前值 ∈ expected 时写入 new，返回是否成功。
        expected 可传单个值或列表；含 ""/None 时行不存在也视为匹配
        （用于"占坑"类锁，替代 check-then-set 竞态——如 release_running / init_status）。"""
        exps = list(expected) if isinstance(expected, (list, tuple, set)) else [expected]
        with self._conn() as c:
            if any(e in (None, "") for e in exps):
                ph = ",".join("?" * len(exps))
                cur = c.execute(
                    f"INSERT INTO kv(k,v) VALUES(?,?) "
                    f"ON CONFLICT(k) DO UPDATE SET v=excluded.v "
                    f"WHERE kv.v IN ({ph})",
                    (k, _dumps(new), *[_dumps(e) for e in exps]))
            else:
                ph = ",".join("?" * len(exps))
                cur = c.execute(f"UPDATE kv SET v=? WHERE k=? AND v IN ({ph})",
                                (_dumps(new), k, *[_dumps(e) for e in exps]))
            return cur.rowcount > 0

    # ---------------- projects ----------------
    def add_project(self, repo: str, repo_url: str, **fields) -> int:
        fields = {k: (_dumps(v) if k in PROJECT_JSON_FIELDS else v)
                  for k, v in fields.items()}
        cols = ["repo", "repo_url"] + list(fields)
        vals = [repo, repo_url] + list(fields.values())
        with self._conn() as c:
            cur = c.execute(
                f"INSERT INTO projects({','.join(cols)}, created_at) "
                f"VALUES({','.join('?' * len(cols))}, ?)",
                vals + [now()])
            return cur.lastrowid

    def get_or_add_project(self, repo: str, repo_url: str = "", **fields) -> dict:
        """存在即返回；否则创建（并发安全：撞 UNIQUE 时回读已有行）。"""
        p = self.get_project(repo)
        if p:
            return p
        try:
            self.add_project(repo, repo_url, **fields)
        except sqlite3.IntegrityError:
            pass   # 并发下另一线程先建了：直接读已有行
        return self.get_project(repo)

    def get_project(self, repo: str):
        with self._conn() as c:
            row = c.execute("SELECT * FROM projects WHERE repo=?", (repo,)).fetchone()
        return self._project_row(row) if row else None

    def get_project_by_id(self, pid: int):
        with self._conn() as c:
            row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return self._project_row(row) if row else None

    def list_projects(self) -> list:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM projects ORDER BY id").fetchall()
        return [self._project_row(r) for r in rows]

    def update_project(self, repo: str, **fields) -> None:
        if not fields:
            return
        fields = {k: (_dumps(v) if k in PROJECT_JSON_FIELDS else v)
                  for k, v in fields.items()}
        sets = ",".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE projects SET {sets} WHERE repo=?",
                      list(fields.values()) + [repo])

    @staticmethod
    def _project_row(row) -> dict:
        d = dict(row)
        for k in PROJECT_JSON_FIELDS:
            if k in d:
                d[k] = _loads(d[k], {} if k != "build_cmd" else [])
        return d

    # ---------------- issues ----------------
    @staticmethod
    def _upsert_issue_sql(fields: dict):
        fields = {k: (_dumps(v) if k in ISSUE_JSON_FIELDS else v)
                  for k, v in fields.items()}
        cols = ["project_id", "number"] + list(fields)
        vals = list(fields.values())
        sql = (f"INSERT INTO issues({','.join(cols)}, created_at, updated_at) "
               f"VALUES({','.join('?' * len(cols))}, ?, ?)")
        if fields:
            sets = ",".join(f"{k}=excluded.{k}" for k in fields)
            sql += (f" ON CONFLICT(project_id,number) DO UPDATE SET {sets}, "
                    f"updated_at=excluded.updated_at")
        else:
            sql += " ON CONFLICT(project_id,number) DO UPDATE SET updated_at=excluded.updated_at"
        return sql, vals

    def upsert_issue(self, project_id: int, number: int, **fields) -> None:
        """原子 upsert（INSERT ... ON CONFLICT DO UPDATE）：无 SELECT-then-INSERT 竞态，
        只写传入字段（窄写：调用方没传的列不动）。"""
        sql, vals = self._upsert_issue_sql(fields)
        with self._conn() as c:
            c.execute(sql, [project_id, number] + vals + [now(), now()])

    def save_issue_full(self, project_id: int, number: int, fields: dict,
                        session_ids: dict, stage: str = None) -> None:
        """issue upsert + sessions 镜像同步在【同一事务】：消除 issue 已写但
        session 未写（或反向）的中间态。

        sessions 表收敛为 session_ids 的精确镜像：不在集合中的本 issue 行
        软删除（墓碑同时挡住 stale entry 快照的复活）；集合中的按
        ON CONFLICT upsert，已有墓碑的行不复活。"""
        sql, vals = self._upsert_issue_sql(fields)
        with self._conn() as c:
            c.execute(sql, [project_id, number] + vals + [now(), now()])
            issue_id = c.execute(
                "SELECT id FROM issues WHERE project_id=? AND number=?",
                (project_id, number)).fetchone()["id"]
            keep = [sid for sid in (session_ids or {}).values() if sid]
            if keep:
                c.execute(
                    f"UPDATE sessions SET status='deleted' WHERE issue_id=? "
                    f"AND status != 'deleted' "
                    f"AND session_id NOT IN ({','.join('?' * len(keep))})",
                    [issue_id] + keep)
            else:
                c.execute("UPDATE sessions SET status='deleted' "
                          "WHERE issue_id=? AND status != 'deleted'", (issue_id,))
            for key, sid in (session_ids or {}).items():
                if not sid:
                    continue
                c.execute(
                    "INSERT INTO sessions(project_id, issue_id, key, session_id, stage, created_at) "
                    "VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(session_id) DO UPDATE SET stage=excluded.stage "
                    "WHERE sessions.status != 'deleted'",
                    (project_id, issue_id, key, sid, stage, now()))

    def get_issue(self, project_id: int, number: int):
        with self._conn() as c:
            row = c.execute("SELECT * FROM issues WHERE project_id=? AND number=?",
                            (project_id, number)).fetchone()
        return self._issue_row(row) if row else None

    def get_issue_by_id(self, issue_id: int):
        with self._conn() as c:
            row = c.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        return self._issue_row(row) if row else None

    def list_issues(self, project_id: int) -> list:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM issues WHERE project_id=? ORDER BY number DESC",
                             (project_id,)).fetchall()
        return [self._issue_row(r) for r in rows]

    @staticmethod
    def _issue_row(row) -> dict:
        d = dict(row)
        for k in ISSUE_JSON_FIELDS:
            if k in d:
                default = [] if k in ("history", "direct_calls") else (
                    {} if k in ("retries", "session_ids") else None)
                d[k] = _loads(d[k], default)
        return d

    # ---------------- sessions ----------------
    def add_session(self, project_id, issue_id, key: str, session_id: str,
                    stage: str = None) -> None:
        if not session_id:
            return
        with self._conn() as c:
            # status='deleted' 是软删除墓碑：stale entry 快照里的 session_ids 经
            # session 同步重放时不得复活已删除行（WHERE 条件挡住 upsert）
            c.execute(
                "INSERT INTO sessions(project_id, issue_id, key, session_id, stage, created_at) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET stage=excluded.stage "
                "WHERE sessions.status != 'deleted'",
                (project_id, issue_id, key, session_id, stage, now()))

    def list_sessions(self, project_id: int = None, issue_id: int = None) -> list:
        q = "SELECT * FROM sessions"
        cond, args = ["status != 'deleted'"], []
        if project_id is not None:
            cond.append("project_id=?"); args.append(project_id)
        if issue_id is not None:
            cond.append("issue_id=?"); args.append(issue_id)
        q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY id DESC"
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def get_session(self, session_id: str):
        with self._conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE session_id=?",
                            (session_id,)).fetchone()
        return dict(row) if row else None

    def delete_session_row(self, session_id: str) -> None:
        """软删除（墓碑）：stale entry 快照经 session 同步重放时，
        upsert 被 status='deleted' 挡住，行不会复活。"""
        with self._conn() as c:
            c.execute("UPDATE sessions SET status='deleted' WHERE session_id=?",
                      (session_id,))

    def list_sessions_enriched(self) -> list:
        """Session 管理器数据源：关联项目/issue 信息（不含已删除墓碑）。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT s.*, p.repo AS repo, i.number AS issue_number "
                "FROM sessions s "
                "LEFT JOIN projects p ON p.id = s.project_id "
                "LEFT JOIN issues i ON i.id = s.issue_id "
                "WHERE s.status != 'deleted' "
                "ORDER BY s.id DESC").fetchall()
        return [dict(r) for r in rows]

    # ---------------- prs / branches / releases ----------------
    def add_pr(self, project_id, issue_id, number: int, url: str, branch: str) -> None:
        """登记 PR；按 (project_id, number) 去重——重跑/补发重复登记会虚增
        发布计数条件，同号只留一行。"""
        with self._conn() as c:
            c.execute(
                "INSERT INTO prs(project_id, issue_id, number, url, branch, created_at) "
                "SELECT ?,?,?,?,?,? "
                "WHERE NOT EXISTS(SELECT 1 FROM prs WHERE project_id=? AND number=?)",
                (project_id, issue_id, number, url, branch, now(),
                 project_id, number))

    def set_pr_merged(self, url: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE prs SET state='merged', merged_at=? WHERE url=?", (now(), url))

    def list_prs(self, project_id: int = None) -> list:
        q = "SELECT * FROM prs"
        args = []
        if project_id is not None:
            q += " WHERE project_id=?"; args.append(project_id)
        q += " ORDER BY id DESC"
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def add_branch(self, project_id, issue_id, name: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO branches(project_id, issue_id, name, pushed_at) "
                      "VALUES(?,?,?,?)", (project_id, issue_id, name, now()))

    def add_release(self, project_id, tag: str, url: str, trigger: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO releases(project_id, tag, url, trigger, created_at) "
                      "VALUES(?,?,?,?,?)", (project_id, tag, url, trigger, now()))

    def list_releases(self, project_id: int = None) -> list:
        q = "SELECT * FROM releases"
        args = []
        if project_id is not None:
            q += " WHERE project_id=?"; args.append(project_id)
        q += " ORDER BY id DESC"
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    # ---------------- runs ----------------
    def add_run(self, project_id, issue_id, stage: str, ok: bool, detail=None) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO runs(project_id, issue_id, stage, ok, detail, ts) "
                      "VALUES(?,?,?,?,?,?)",
                      (project_id, issue_id, stage, 1 if ok else 0,
                       _dumps(detail) if detail is not None else None, now()))
