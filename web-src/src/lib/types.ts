export type Mode = "default" | "dry" | "semi" | "full" | "yolo" | "paused";

export const MODE_LABEL: Record<string, string> = {
  default: "跟随全局",
  dry: "DRY_RUN",
  semi: "SEMI",
  full: "FULL",
  yolo: "YOLO",
  paused: "暂停",
};

export interface Issue {
  number: number;
  title: string;
  stage: string;
  mode: string;
  repo?: string;
  complexity?: string | null;
  cost?: number;
  tokens?: number;
  merged?: boolean;
  pr_url?: string | null;
  updated_at?: string | null;
  error?: string | null;
  summary?: string | null;
  verdict?: any;
  audit?: any;
  patch_saved?: boolean;
  history?: { stage: string; status: string; ts?: string; summary?: string }[];
  publish_status?: string | null;
  merge_fix_status?: string | null;
  paused_from?: string | null;
  info_posted?: boolean;
  info_seen_comments?: number;
  retries?: Record<string, number>;
}

export interface AttentionItem {
  repo: string;
  number: number;
  title: string;
  stage: string;
  mode: string;
  kind: "needs_human" | "waiting" | "publishable" | "op_failed";
  tone: "err" | "warn" | "info";
  tag: string;
  reason: string;
  updated_at?: string | null;
  waiting_since?: number;
  ttl_sec?: number;
}

export interface InFlightItem extends Issue {
  repo: string;
}

export interface DashboardResp {
  global_mode: string;
  poll_interval_sec: number;
  model?: string;
  repo?: string;
  running: { repo: string; issue: number; started: number }[];
  agent_gate: GateSnap;
  board: InFlightItem[];
  attention: AttentionItem[];
  in_flight: InFlightItem[];
}

export interface ReleaseCfg {
  time_enabled: boolean;
  interval_hours: number;
  count_enabled: boolean;
  count_threshold: number;
  mode: string;
}

export interface Project {
  id: number | null;
  repo: string;
  repo_url: string;
  intro?: string;
  mode: string;
  agent_limit?: number;
  main_branch?: string;
  build_cmd?: string[];
  release_cfg?: ReleaseCfg;
  issues?: Issue[];
}

export interface SessionRow {
  id: number;
  repo?: string | null;
  issue_number?: number | null;
  key?: string | null;
  session_id: string;
  stage?: string | null;
  status?: string | null;
  tokens?: number | null;
  created_at?: string | null;
}

export interface StatDay {
  date: string;
  created: number;
  done: number;
  cost: number;
  tokens: number;
}

export interface Stats {
  days: StatDay[];
  totals: {
    issues: number;
    done: number;
    rejected: number;
    needs_human: number;
    open: number;
    merged: number;
    cost: number;
    tokens: number;
    projects: number;
  };
}

export interface ActivityEvent {
  ts?: string;
  source: "agent" | "bot";
  stage?: string;
  type?: string;
  tool?: string;
  chars?: number;
  level?: string;
  scope?: string;
  event?: string;
  issue?: string | number;
  repo?: string;
  detail?: any;
}

export interface ActivityResp {
  events: ActivityEvent[];
  running: { repo: string; issue: number; started: number }[];
  agent_gate: { limit: number; active: number; waiting: number; total_acquired: number };
}

export interface GateSnap {
  limit: number;
  active: number;
  waiting: number;
  total_acquired: number;
}

export interface ReleaseStatus {
  repo: string;
  due?: boolean;
  reasons?: string[];
  merged_since?: number;
  cfg?: ReleaseCfg;
  state?: { last_ts?: number; last_sha?: string; last_error?: string; last_error_ts?: number;
            last_result?: { ok?: boolean; dry?: boolean; tag?: string; error?: string; ts?: number } };
  progress?: { steps?: string[]; ts?: number } | null;
  workflows?: string[];
  error?: string;
}
