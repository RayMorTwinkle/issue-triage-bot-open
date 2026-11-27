import {
  Area, AreaChart, CartesianGrid, Line, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import type { StatDay } from "../lib/types";
import { getTheme } from "../lib/theme";

export function TrendChart({ days }: { days: StatDay[] }) {
  const dark = getTheme() === "dark";
  const c = dark
    ? { acc: "#ff8c42", ok: "#5fd08a", warn: "#ffb454", line: "#3d2f57", grid: "#2a2140" }
    : { acc: "#39628f", ok: "#3e7d4f", warn: "#b07a1f", line: "#d9d0ba", grid: "#e3dcc9" };
  return (
    <ResponsiveContainer width="100%" height={210}>
      <AreaChart data={days} margin={{ top: 6, right: 6, bottom: 0, left: -14 }}>
        <defs>
          <linearGradient id="gCost" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={c.acc} stopOpacity={0.32} />
            <stop offset="100%" stopColor={c.acc} stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={c.grid} strokeDasharray="3 3" vertical={false} />
        <XAxis
          dataKey="date"
          tickFormatter={(v) => String(v).slice(5)}
          tick={{ fontSize: 10.5, fill: c.line }}
          axisLine={{ stroke: c.grid }}
          tickLine={false}
        />
        <YAxis
          yAxisId="l"
          tick={{ fontSize: 10.5, fill: c.line }}
          axisLine={false}
          tickLine={false}
          width={38}
        />
        <YAxis
          yAxisId="r"
          orientation="right"
          allowDecimals={false}
          tick={{ fontSize: 10.5, fill: c.line }}
          axisLine={false}
          tickLine={false}
          width={26}
        />
        <Tooltip
          contentStyle={{
            background: "var(--bg2)",
            border: "1px solid var(--line)",
            borderRadius: 0,
            fontSize: 12,
            color: "var(--fg)",
          }}
          labelStyle={{ color: "var(--dim)" }}
        />
        <Area
          yAxisId="l"
          type="monotone"
          dataKey="cost"
          name="成本($)"
          stroke={c.acc}
          fill="url(#gCost)"
          strokeWidth={2}
        />
        <Line
          yAxisId="r"
          type="monotone"
          dataKey="done"
          name="完成"
          stroke={c.ok}
          strokeWidth={2}
          dot={false}
        />
        <Line
          yAxisId="r"
          type="monotone"
          dataKey="created"
          name="新增"
          stroke={c.warn}
          strokeWidth={1.5}
          strokeDasharray="4 3"
          dot={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
