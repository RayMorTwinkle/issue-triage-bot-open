import React, { useEffect, useState } from "react";
import {
  CircleDot, FolderGit2, Info, KeyRound, LayoutDashboard, ListTree, MessagesSquare,
  Moon, RefreshCcw, Rocket, ScrollText, Settings, Sparkles, Sun,
} from "lucide-react";
import { api } from "../lib/api";
import { cn } from "../lib/utils";
import { navigate, type Route } from "../lib/router";
import { applyTheme, getTheme, toggleTheme, type Theme } from "../lib/theme";

const NAV_GROUPS: { sec: string; items: { to: string; name: string; label: string; icon: any }[] }[] = [
  {
    sec: "日常",
    items: [
      { to: "#/", name: "dashboard", label: "总览", icon: LayoutDashboard },
      { to: "#/issues", name: "issues", label: "Issues", icon: CircleDot },
      { to: "#/release", name: "release", label: "Release", icon: Rocket },
      { to: "#/sessions", name: "sessions", label: "Sessions", icon: MessagesSquare },
    ],
  },
  {
    sec: "系统",
    items: [
      { to: "#/projects", name: "projects", label: "项目", icon: FolderGit2 },
      { to: "#/prompts", name: "prompts", label: "提示词", icon: ScrollText },
      { to: "#/env", name: "env", label: "环境", icon: KeyRound },
      { to: "#/logs", name: "logs", label: "日志", icon: ListTree },
      { to: "#/settings", name: "settings", label: "设置", icon: Settings },
      { to: "#/about", name: "about", label: "关于", icon: Info },
    ],
  },
];

export function AppShell({ route, children }: { route: Route; children: React.ReactNode }) {
  const [theme, setTheme] = useState<Theme>(getTheme());
  const [sub, setSub] = useState("");
  useEffect(() => {
    applyTheme(theme);
  }, [theme]);
  useEffect(() => {
    api
      .get("/api/overview")
      .then((d) => setSub(`${d.model || ""} · ${d.repo || ""}`))
      .catch(() => {});
  }, []);
  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 flex h-screen w-[210px] shrink-0 flex-col border-r border-line bg-bg2/85 backdrop-blur-xl">
        <div className="border-b border-line px-4 py-3.5">
          <div className="font-display text-[15px] font-bold tracking-wide">🤖 issue-triage-bot</div>
          <div className="mt-0.5 truncate text-[10.5px] text-dim">{sub || "…"}</div>
        </div>
        <nav className="flex-1 overflow-y-auto py-1">
          {NAV_GROUPS.map((g) => (
            <div key={g.sec}>
              <div className="sec-label px-4 pb-1 pt-3">
                {g.sec}
              </div>
              {g.items.map((it) => {
                const active =
                  route.name === it.name || (it.name === "issues" && route.name === "issue");
                return (
                  <button
                    key={it.to}
                    onClick={() => navigate(it.to)}
                    className={cn(
                      "flex w-full cursor-pointer items-center gap-2.5 border-l-[3px] px-4 py-2 text-left text-[13px] transition-colors",
                      active
                        ? "border-l-acc bg-accsoft text-fg"
                        : "border-l-transparent text-dim hover:bg-accsoft/50 hover:text-fg"
                    )}
                  >
                    <it.icon size={14} />
                    <span>{it.label}</span>
                  </button>
                );
              })}
            </div>
          ))}
        </nav>
        <div className="flex items-center gap-2 border-t border-line p-3">
          <button
            onClick={() => setTheme(toggleTheme())}
            title="切换主题"
            className="flex flex-1 cursor-pointer items-center justify-center gap-1.5 border border-line px-2 py-1.5 text-xs text-dim transition-colors hover:border-line2 hover:text-fg"
          >
            {theme === "light" ? <Moon size={12} /> : <Sun size={12} />}
            {theme === "light" ? "墨夜" : "宣纸"}
          </button>
          <button
            onClick={() => navigate("#/wizard")}
            title="运行接入向导"
            className="flex flex-1 cursor-pointer items-center justify-center gap-1.5 border border-line px-2 py-1.5 text-xs text-dim transition-colors hover:border-line2 hover:text-fg"
          >
            <Sparkles size={12} /> 向导
          </button>
          <button
            onClick={() => api.post("/api/poll", {})}
            title="立即轮询全部项目"
            className="flex cursor-pointer items-center justify-center border border-line px-2 py-1.5 text-xs text-dim transition-colors hover:border-line2 hover:text-fg"
          >
            <RefreshCcw size={12} />
          </button>
        </div>
      </aside>
      <main className="min-w-0 flex-1 px-6 py-5">
        <div className="mx-auto max-w-[1280px]">{children}</div>
      </main>
    </div>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="role-display">{title}</h1>
        {subtitle && <div className="role-meta mt-0.5">{subtitle}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function useInterval(fn: () => void, ms: number | null) {
  useEffect(() => {
    if (ms === null) return;
    const id = window.setInterval(fn, ms);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms]);
}

export function usePoll(fn: () => void | Promise<void>, ms: number) {
  useEffect(() => {
    let stop = false;
    let inFlight = false;
    const tick = async () => {
      if (stop || inFlight) return;
      inFlight = true;
      try {
        await fn();
      } finally {
        inFlight = false;
      }
    };
    tick();
    const id = window.setInterval(tick, ms);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
