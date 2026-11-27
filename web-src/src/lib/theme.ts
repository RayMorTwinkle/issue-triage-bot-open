export type Theme = "light" | "dark";

export function getTheme(): Theme {
  return (localStorage.getItem("botTheme") as Theme) || "light";
}

export function applyTheme(t: Theme): void {
  document.documentElement.dataset.theme = t;
  localStorage.setItem("botTheme", t);
}

export function toggleTheme(): Theme {
  const next: Theme = getTheme() === "light" ? "dark" : "light";
  applyTheme(next);
  return next;
}

export function themeColors(): { acc: string; ok: string; warn: string; err: string; line: string } {
  const dark = getTheme() === "dark";
  return dark
    ? { acc: "#d9915a", ok: "#70b586", warn: "#d2a24c", err: "#cf6a5b", line: "#38322a" }
    : { acc: "#8f4f1d", ok: "#3e7d4f", warn: "#b07a1f", err: "#a83a32", line: "#d9d0ba" };
}
