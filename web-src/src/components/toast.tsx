import React, { createContext, useCallback, useContext, useState } from "react";
import { cn } from "../lib/utils";
import { CheckCircle2, Info, XCircle } from "lucide-react";

export interface Toast {
  id: number;
  kind: "ok" | "err" | "info";
  title: string;
  body?: string;
}

const ToastCtx = createContext<{ push: (t: Omit<Toast, "id">) => void }>({
  push: () => {},
});

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((t: Omit<Toast, "id">) => {
    const id = Date.now() + Math.random();
    setItems((xs) => [...xs.slice(-4), { ...t, id }]);
    window.setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), t.kind === "err" ? 10000 : 4800);
  }, []);
  return (
    <ToastCtx.Provider value={{ push }}>
      {children}
      <div className="pointer-events-none fixed right-4 bottom-4 z-[100] flex w-[380px] max-w-[calc(100vw-2rem)] flex-col gap-2">
        {items.map((t) => (
          <div
            key={t.id}
            className={cn(
              "glass fadeup pointer-events-auto border-l-4 p-3 shadow-[var(--shadow-lift)]",
              t.kind === "err" ? "border-l-err" : t.kind === "ok" ? "border-l-ok" : "border-l-acc"
            )}
          >
            <div className="flex items-start gap-2">
              {t.kind === "err" ? (
                <XCircle size={15} className="mt-0.5 shrink-0 text-err" />
              ) : t.kind === "ok" ? (
                <CheckCircle2 size={15} className="mt-0.5 shrink-0 text-ok" />
              ) : (
                <Info size={15} className="mt-0.5 shrink-0 text-acc" />
              )}
              <div className="min-w-0">
                <div className="text-sm font-medium">{t.title}</div>
                {t.body && (
                  <div className="mt-0.5 text-xs break-all whitespace-pre-wrap text-dim">{t.body}</div>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast() {
  return useContext(ToastCtx);
}
