import React from "react";
import { cn } from "../lib/utils";
import { X } from "lucide-react";

/* ============================== Button ============================== */
type BtnVariant = "primary" | "outline" | "ghost" | "danger" | "subtle";
type BtnSize = "xs" | "sm" | "md";

export function Button({
  variant = "primary",
  size = "md",
  className,
  children,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: BtnVariant; size?: BtnSize }) {
  const variants: Record<BtnVariant, string> = {
    primary: "bg-acc text-white border-acc shadow-[var(--shadow)] hover:shadow-[var(--shadow-lift)] hover:brightness-105",
    outline: "bg-transparent text-acc border-acc hover:bg-accsoft",
    ghost: "bg-transparent text-dim border-transparent hover:text-fg hover:bg-accsoft",
    danger: "bg-err text-white border-err shadow-[var(--shadow)] hover:shadow-[var(--shadow-lift)] hover:brightness-105",
    subtle: "bg-bg2 text-fg border-line shadow-[0_1px_1px_rgba(60,50,20,0.06)] hover:border-line2 hover:bg-card",
  };
  const sizes: Record<BtnSize, string> = {
    xs: "px-2 py-0.5 text-[11px]",
    sm: "px-2.5 py-1 text-xs",
    md: "px-3.5 py-1.5 text-sm",
  };
  return (
    <button
      className={cn(
        "inline-flex items-center justify-center gap-1.5 border font-sans cursor-pointer",
        "transition-all duration-100 select-none whitespace-nowrap active:translate-y-px",
        "disabled:opacity-45 disabled:cursor-not-allowed disabled:active:translate-y-0",
        variants[variant],
        sizes[size],
        className
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

/* ============================== Card ============================== */
export function Card({
  title,
  right,
  className,
  bodyClassName,
  pad = true,
  children,
}: {
  title?: React.ReactNode;
  right?: React.ReactNode;
  className?: string;
  bodyClassName?: string;
  pad?: boolean;
  children?: React.ReactNode;
}) {
  return (
    <div className={cn("glass", pad && "p-4", className)}>
      {(title || right) && (
        <div className="mb-3 flex items-center justify-between gap-2 border-b border-line/60 pb-2">
          <h3 className="sec-label">{title}</h3>
          {right}
        </div>
      )}
      <div className={bodyClassName}>{children}</div>
    </div>
  );
}

/* ============================== Badge ============================== */
export function Badge({
  className,
  children,
  tone = "acc",
}: {
  className?: string;
  children: React.ReactNode;
  tone?: "acc" | "ok" | "err" | "warn" | "dim" | "info";
}) {
  const tones = {
    acc: "text-acc border-acc",
    ok: "text-ok border-ok",
    err: "text-err border-err",
    warn: "text-warn border-warn",
    dim: "text-dim border-line2",
    info: "text-info border-info",
  };
  return (
    <span
      className={cn(
        "inline-block border px-2 py-[1px] text-[11px] leading-4 whitespace-nowrap",
        tones[tone],
        className
      )}
    >
      {children}
    </span>
  );
}

export const STAGE_TONE: Record<string, "acc" | "ok" | "err" | "warn" | "dim"> = {
  done: "ok",
  merged: "ok",
  needs_human: "err",
  rejected: "err",
  pending: "dim",
  waiting_info: "warn",
};

export const STAGE_LABEL: Record<string, string> = {
  pending: "待处理",
  audit: "问题审核",
  triage: "分诊",
  waiting_info: "等待补充信息",
  fix: "修复",
  gate: "门禁",
  review: "审查",
  final: "终审",
  reply: "回复",
  execute: "执行",
  done: "已完成",
  rejected: "已拒绝",
  needs_human: "需人工",
};

export function StageBadge({ stage, className }: { stage: string; className?: string }) {
  return (
    <Badge tone={STAGE_TONE[stage] || "acc"} className={className}>
      {STAGE_LABEL[stage] || stage}
    </Badge>
  );
}

/* ============================== 表单控件 ============================== */
const fieldCls =
  "bg-bg2 text-fg border border-line px-2.5 py-1 text-sm outline-none " +
  "shadow-[var(--inset)] transition-[border-color,box-shadow] duration-100 " +
  "focus:border-acc focus:shadow-[0_0_0_2px_var(--acc-soft)] " +
  "placeholder:text-dim/70 disabled:opacity-50";

export function Input({ className, ...rest }: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn(fieldCls, className)} {...rest} />;
}

export function Select({ className, children, ...rest }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={cn(fieldCls, "cursor-pointer", className)} {...rest}>
      {children}
    </select>
  );
}

export function Textarea({ className, ...rest }: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn(fieldCls, "font-mono text-xs leading-relaxed", className)} {...rest} />;
}

export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: React.ReactNode;
  disabled?: boolean;
}) {
  return (
    <label className={cn("inline-flex items-center gap-2 select-none", disabled ? "opacity-50" : "cursor-pointer")}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cn(
          "relative h-5 w-9 border transition-colors",
          checked ? "bg-acc border-acc" : "bg-bg2 border-line2 shadow-[var(--inset)]"
        )}
      >
        <span
          className={cn(
            "absolute top-[1px] left-[1px] h-[16px] w-[16px] bg-white transition-all",
            checked && "left-[calc(100%-17px)]"
          )}
        />
      </button>
      {label && <span className={cn("text-sm", checked ? "text-fg" : "text-dim")}>{label}</span>}
    </label>
  );
}

/* ============================== Dialog ============================== */
export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
  width = 520,
}: {
  open: boolean;
  onClose: () => void;
  title?: React.ReactNode;
  children?: React.ReactNode;
  footer?: React.ReactNode;
  width?: number;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/45" onClick={onClose} />
      <div
        className="glass fadeup relative max-h-[85vh] w-full overflow-y-auto p-5 shadow-[var(--shadow-lift)]"
        style={{ maxWidth: width }}
      >
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="font-display text-[15px] font-bold tracking-wide">{title}</div>
          <button className="cursor-pointer text-dim hover:text-fg" onClick={onClose} aria-label="关闭">
            <X size={16} />
          </button>
        </div>
        <div className="text-sm">{children}</div>
        {footer && <div className="mt-4 flex justify-end gap-2">{footer}</div>}
      </div>
    </div>
  );
}

export function ConfirmDialog({
  open,
  title,
  children,
  confirmText = "确认",
  danger,
  busy,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: React.ReactNode;
  children?: React.ReactNode;
  confirmText?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      width={480}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            取消
          </Button>
          <Button variant={danger ? "danger" : "primary"} onClick={onConfirm} disabled={busy}>
            {busy ? "执行中…" : confirmText}
          </Button>
        </>
      }
    >
      {children}
    </Dialog>
  );
}

/* ============================== Misc ============================== */
export function Progress({ value, className }: { value: number; className?: string }) {
  return (
    <div className={cn("h-1.5 w-full bg-line", className)}>
      <div
        className="h-full bg-acc transition-all"
        style={{ width: `${Math.min(100, Math.max(0, value))}%` }}
      />
    </div>
  );
}

export function Empty({ children }: { children?: React.ReactNode }) {
  return <div className="py-6 text-center text-xs text-dim">{children || "暂无数据"}</div>;
}

/* 筛选/文件 chip：样式契约在 index.css 的 .chip */
export function Chip({
  on, className, children, ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { on?: boolean }) {
  return (
    <button className={cn("chip", on && "on", className)} {...rest}>
      {children}
    </button>
  );
}

export function KV({ k, children }: { k: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex gap-2 text-xs">
      <span className="shrink-0 text-dim">{k}</span>
      <span className="min-w-0 break-all">{children}</span>
    </div>
  );
}

export function StatCard({
  label,
  value,
  sub,
  accent,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  sub?: React.ReactNode;
  accent?: boolean;
}) {
  return (
    <div className="glass p-4">
      <div className="sec-label">{label}</div>
      <div className={cn("font-display mt-1 text-2xl font-bold tabular-nums", accent && "text-acc")}>
        {value}
      </div>
      {sub && <div className="mt-0.5 text-[11px] text-dim">{sub}</div>}
    </div>
  );
}
