import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

export function fmtAge(sec: number): string {
  sec = Math.max(0, Math.floor(sec || 0));
  if (sec < 60) return `${sec} 秒`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`;
  return `${Math.floor(sec / 3600)} 小时 ${Math.floor((sec % 3600) / 60)} 分`;
}

export function fmtMoney(v?: number | null): string {
  const x = v || 0;
  return `$${x.toFixed(Math.abs(x) < 0.1 ? 4 : 2)}`;
}

export function fmtTokens(v?: number | null): string {
  const x = v || 0;
  if (x >= 1_000_000) return `${(x / 1_000_000).toFixed(2)}M`;
  if (x >= 10_000) return `${(x / 1000).toFixed(1)}k`;
  return x.toLocaleString();
}

export function fmtTs(ts?: string | null): string {
  return (ts || "").replace("T", " ");
}

export function fmtClock(ts?: string | null): string {
  if (!ts) return "--:--:--";
  const m = String(ts).match(/(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : String(ts).slice(-8);
}

export function fmtAgoTs(ts?: string | null): string {
  if (!ts) return "—";
  const t = Date.parse(String(ts).replace(" ", "T"));
  if (!t) return String(ts);
  const d = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (d < 60) return `${d} 秒前`;
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
}

export function repoSlug(repo: string): string {
  return (repo || "").replace("/", "__");
}

export function slugToRepo(slug: string): string {
  return (slug || "").replace("__", "/");
}

export async function copyText(s: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(s);
  } catch {
    /* noop */
  }
}

/* 全局 UI 选择态（跨页面记忆） */
export function selRepo(): string {
  return localStorage.getItem("selRepo") || "";
}
export function setSelRepo(repo: string): void {
  localStorage.setItem("selRepo", repo);
}
