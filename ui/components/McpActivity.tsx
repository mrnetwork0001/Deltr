"use client";
// Scrolling MCP activity: who called what, how fast, and the trace it produced.
import { ArrowDownLeft, ArrowUpRight, Check, X } from "lucide-react";
import type { McpActivity as Row, SystemStatus } from "@/lib/types";
import { STATUS, clockMs, ms } from "@/lib/format";
import { apiBase } from "@/lib/api";
import { Chip, Panel } from "@/components/StatusBar";

export interface McpActivityProps {
  activity: Row[];
  status: SystemStatus | null;
  onSelectTrace: (id: string) => void;
}

function mcpUrl(): string {
  const b = apiBase();
  if (/^https?:\/\//.test(b)) return b.replace(/^https?:\/\//, "") + "/mcp";
  if (typeof window !== "undefined") return `${window.location.host}${b}/mcp`;
  return ":8000/mcp";
}

export default function McpActivity({ activity, status, onSelectTrace }: McpActivityProps) {
  const rows = [...activity].sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime());
  const up = status?.upstream;
  return (
    <Panel title="MCP activity" className="flex-1" right={<span className="font-mono tabular-nums">{rows.length} calls</span>}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Chip color="#1FC7D4" title="Deltr's own MCP server (streamable HTTP)">
          Deltr MCP: http {mcpUrl()}
        </Chip>
        <Chip
          color={up?.kind === "official" ? STATUS.good : up?.kind === "shim" ? "#1FC7D4" : "#6b7280"}
          title={up?.error ?? up?.url ?? "no upstream"}
        >
          Binance upstream: {up?.kind ?? "none"}
          {up?.tools_discovered?.length ? ` · ${up.tools_discovered.length} tools` : ""}
        </Chip>
      </div>
      {rows.length ? (
        <ul className="flex max-h-[420px] flex-col overflow-y-auto rounded-md border border-ink-700/80 text-xs">
          {rows.map((r) => {
            const inbound = r.direction === "inbound";
            return (
              <li key={r.id} className="grid grid-cols-[14px_84px_1fr_auto] items-center gap-2 border-b border-ink-700/60 px-2.5 py-1.5 last:border-0 hover:bg-ink-800/50">
                <span className="text-gray-500" title={inbound ? "inbound: a client called Deltr" : "outbound: Deltr called an upstream"}>
                  {inbound ? <ArrowDownLeft size={13} /> : <ArrowUpRight size={13} />}
                </span>
                <span className="font-mono tabular-nums text-gray-500">{clockMs(r.ts)}</span>
                <span className="min-w-0 truncate">
                  <span className="text-gray-300">{r.client ?? "unknown"}</span>
                  <span className="text-gray-600"> → </span>
                  <span className="text-gray-400">{r.server}:</span>
                  <span className="font-mono font-semibold text-gray-100">{r.tool}</span>
                  {r.result_summary ? <span className="ml-2 text-gray-400">{r.result_summary}</span> : null}
                </span>
                <span className="flex items-center gap-2 font-mono tabular-nums">
                  <span className="inline-flex items-center gap-[2px]" style={{ color: r.ok ? STATUS.good : STATUS.critical }}>
                    {r.ok ? <Check size={12} strokeWidth={3} /> : <X size={12} strokeWidth={3} />}
                    {r.ok ? "ok" : "error"}
                  </span>
                  <span className="text-gray-400">{ms(r.latency_ms)}</span>
                  {r.trace_id ? (
                    <a
                      href="#trace"
                      onClick={(e) => {
                        e.preventDefault();
                        onSelectTrace(r.trace_id as string);
                        document.getElementById("trace")?.scrollIntoView({ behavior: "smooth", block: "start" });
                      }}
                      className="text-cake hover:underline"
                      title={r.trace_id}
                    >
                      trace
                    </a>
                  ) : (
                    <span className="w-9 text-gray-700">–</span>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      ) : (
        <div className="flex min-h-[8rem] flex-1 flex-col items-center justify-center gap-1 rounded-md border border-dashed border-ink-700 px-3 text-center text-xs text-gray-500">
          <span>no MCP calls yet</span>
          <span className="font-mono text-gray-600">claude mcp add deltr --transport http http://127.0.0.1:8000/mcp</span>
        </div>
      )}
    </Panel>
  );
}
