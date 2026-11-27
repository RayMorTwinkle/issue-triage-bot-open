import { useEffect, useState } from "react";

export interface Route {
  name: string;
  params: Record<string, string>;
}

export function parseHash(): Route {
  const h = (location.hash || "#/").replace(/^#/, "");
  const seg = h.split("/").filter(Boolean);
  if (seg.length === 0) return { name: "dashboard", params: {} };
  const [a, b, c] = seg;
  switch (a) {
    case "issues":
      return { name: "issues", params: {} };
    case "projects":
      return { name: "projects", params: {} };
    case "p":
      return { name: "project", params: { slug: b || "" } };
    case "i":
      return { name: "issue", params: { slug: b || "", n: c || "" } };
    case "release":
      return { name: "release", params: {} };
    case "sessions":
      return { name: "sessions", params: {} };
    case "prompts":
      return { name: "prompts", params: {} };
    case "env":
      return { name: "env", params: {} };
    case "logs":
      return { name: "logs", params: {} };
    case "settings":
      return { name: "settings", params: {} };
    case "about":
      return { name: "about", params: {} };
    case "wizard":
      return { name: "wizard", params: {} };
    default:
      return { name: "dashboard", params: {} };
  }
}

export function navigate(to: string): void {
  location.hash = to;
}

export function useRoute(): Route {
  const [r, setR] = useState<Route>(parseHash);
  useEffect(() => {
    const f = () => setR(parseHash());
    window.addEventListener("hashchange", f);
    return () => window.removeEventListener("hashchange", f);
  }, []);
  return r;
}
