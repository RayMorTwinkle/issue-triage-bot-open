import { useEffect, useRef, useState } from "react";
import { api } from "./lib/api";
import { useRoute } from "./lib/router";
import { useToast } from "./components/toast";
import { AppShell } from "./components/AppShell";
import { Dashboard } from "./pages/Dashboard";
import { IssuesPage } from "./pages/IssuesPage";
import { IssueDetail } from "./pages/IssueDetail";
import { ProjectsPage } from "./pages/ProjectsPage";
import { ProjectPage } from "./pages/ProjectPage";
import { ReleasePage } from "./pages/ReleasePage";
import { SessionsPage } from "./pages/SessionsPage";
import { PromptsPage } from "./pages/PromptsPage";
import { EnvPage } from "./pages/EnvPage";
import { LogsPage } from "./pages/LogsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { About } from "./pages/About";
import { Wizard } from "./pages/Wizard";

function Pages({ route }: { route: ReturnType<typeof useRoute> }) {
  switch (route.name) {
    case "dashboard":
      return <Dashboard />;
    case "issues":
      return <IssuesPage />;
    case "issue":
      return <IssueDetail slug={route.params.slug} n={route.params.n} />;
    case "projects":
      return <ProjectsPage />;
    case "project":
      return <ProjectPage slug={route.params.slug} />;
    case "release":
      return <ReleasePage />;
    case "sessions":
      return <SessionsPage />;
    case "prompts":
      return <PromptsPage />;
    case "env":
      return <EnvPage />;
    case "logs":
      return <LogsPage />;
    case "settings":
      return <SettingsPage />;
    case "about":
      return <About />;
    default:
      return <Dashboard />;
  }
}

/** 发布结束全局提醒：任意页面都会弹 toast（进行中→空闲 跳变检测）。 */
function ReleaseWatcher() {
  const { push } = useToast();
  const prev = useRef("");
  useEffect(() => {
    const id = window.setInterval(async () => {
      try {
        const st = await api.get("/api/release/status");
        const running = st.running || "";
        if (prev.current && !running) {
          const repo = prev.current;
          const row = (st.projects || []).find((p: { repo: string }) => p.repo === repo);
          const r = row?.state?.last_result;
          if (r?.ok) {
            push({
              kind: "ok", title: r.dry ? "📝 发布计划已生成（DRY）" : "✅ 发布成功",
              body: `${repo}${r.tag ? " " + r.tag : ""}`,
            });
          } else {
            push({
              kind: "err", title: "❌ 发布失败",
              body: `${repo} — ${r?.error || "详见 Release 页"}`,
            });
          }
        }
        prev.current = running;
      } catch {
        /* noop */
      }
    }, 15000);
    return () => window.clearInterval(id);
  }, [push]);
  return null;
}

export function App() {
  const route = useRoute();
  const [wizard, setWizard] = useState<"loading" | "show" | "hide">("loading");

  useEffect(() => {
    api
      .get("/api/wizard/status")
      .then((d) => setWizard(d.done ? "hide" : "show"))
      .catch(() => setWizard("hide"));
  }, []);

  if (route.name === "wizard") {
    return <Wizard onDone={() => setWizard("hide")} />;
  }
  if (wizard === "show") {
    return <Wizard onDone={() => setWizard("hide")} />;
  }
  return (
    <AppShell route={route}>
      <Pages route={route} />
      <ReleaseWatcher />
    </AppShell>
  );
}
