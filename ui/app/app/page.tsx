"use client";
// Deltr dashboard (served at /app/): 12-column grid, 1 Hz snapshot poll with WS
// accelerator, automatic fallback to the bundled mock so the page is never blank.
import { useCallback, useEffect, useRef, useState } from "react";
import type { Snapshot } from "@/lib/types";
import { fetchMockSnapshot, fetchSnapshot, openStream } from "@/lib/api";
import StatusBar, { KpiStrip } from "@/components/StatusBar";
import SpreadChart from "@/components/SpreadChart";
import EdgeWaterfall from "@/components/EdgeWaterfall";
import PositionsTable from "@/components/PositionsTable";
import RiskGateLog from "@/components/RiskGateLog";
import TradeTrace from "@/components/TradeTrace";
import McpActivity from "@/components/McpActivity";
import PromptConsole from "@/components/PromptConsole";

const LIVE_STALE_MS = 5000; // no live frame for this long -> the page is "down" (never silently swaps in fake rows)

// The bundled mock is opt-in: `?mock=1`, or `next dev` off-origin with no NEXT_PUBLIC_API (no backend to
// talk to).  When FastAPI serves the page a stalled backend keeps the last live snapshot and shows
// "backend unreachable" instead of invented positions / receipts / MCP rows.
function mockAllowed(): boolean {
  if (typeof window === "undefined") return false;
  const q = new URLSearchParams(window.location.search);
  if (q.get("mock") === "1") return true;
  if (q.get("mock") === "0") return false;
  const devOrigin = /^https?:\/\/(localhost|127\.0\.0\.1):300[01]$/.test(window.location.origin);
  return devOrigin && !process.env.NEXT_PUBLIC_API;
}

// The static export on Vercel reaches the engine through vercel.json rewrites (/api, /mcp proxied to
// the VPS; WebSockets are not proxied, so the 1 Hz poll fallback carries the stream). Only if that
// same-origin path is dead AND NEXT_PUBLIC_APP_URL names the live dashboard elsewhere does /app/
// forward there; `?stay=1` keeps this copy open for debugging.
const LIVE_APP_URL = process.env.NEXT_PUBLIC_APP_URL ?? "";
function liveElsewhere(): string | null {
  if (typeof window === "undefined" || !LIVE_APP_URL || !/^https?:\/\//.test(LIVE_APP_URL)) return null;
  if (LIVE_APP_URL.startsWith(window.location.origin)) return null;
  return LIVE_APP_URL;
}

export default function Page() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [mock, setMock] = useState(false);
  const [transport, setTransport] = useState<"ws" | "poll" | "down">("down");
  const [lastUpdate, setLastUpdate] = useState<string | null>(null);
  const [selectedTrace, setSelectedTrace] = useState<string | null>(null);
  const lastLiveAt = useRef(0);
  const mockCache = useRef<Snapshot | null>(null);
  const mockLoading = useRef(false);

  const loadMock = useCallback(async () => {
    if (mockLoading.current || !mockAllowed()) return;
    mockLoading.current = true;
    try {
      const m = mockCache.current ?? (await fetchMockSnapshot());
      mockCache.current = m;
      setSnap((prev) => (prev && lastLiveAt.current && Date.now() - lastLiveAt.current < LIVE_STALE_MS ? prev : m));
      setMock(true);
    } catch {
      /* no mock either: keep whatever we have */
    } finally {
      mockLoading.current = false;
    }
  }, []);

  useEffect(() => {
    const h = openStream({
      onSnapshot: (s) => {
        lastLiveAt.current = Date.now();
        setSnap(s);
        setMock(false);
        setLastUpdate(new Date().toISOString());
      },
      onTransport: setTransport,
      onError: () => {
        if (!lastLiveAt.current || Date.now() - lastLiveAt.current > LIVE_STALE_MS) void loadMock();
      },
    });
    return () => h.stop();
  }, [loadMock]);

  // Forward to the live dashboard only when nothing has answered here for 6 s.
  useEffect(() => {
    const live = liveElsewhere();
    if (!live || transport !== "down" || new URLSearchParams(window.location.search).has("stay")) return;
    const t = setTimeout(() => {
      if (!lastLiveAt.current) window.location.replace(live);
    }, 6000);
    return () => clearTimeout(t);
  }, [transport]);

  const refresh = useCallback(async () => {
    try {
      const s = await fetchSnapshot();
      lastLiveAt.current = Date.now();
      setSnap(s);
      setMock(false);
    } catch {
      /* the poller will retry */
    }
  }, []);

  const status = snap?.status ?? null;
  const portfolio = snap?.portfolio ?? null;
  const market = snap?.market ?? null;

  return (
    <div className="min-h-screen">
      <StatusBar status={status} portfolio={portfolio} mock={mock} transport={transport} lastUpdate={lastUpdate} />
      <main className="mx-auto flex max-w-[1800px] flex-col gap-3 p-4">
        {!snap ? (
          <div className="flex h-64 flex-col items-center justify-center gap-3 rounded-lg border border-ink-700 bg-ink-900 text-sm text-gray-500">
            <span>{transport === "down" && LIVE_APP_URL ? "this copy has no engine behind it" : "connecting to Deltr…"}</span>
            {LIVE_APP_URL ? (
              <a href={LIVE_APP_URL} className="rounded-md border border-bnb/60 bg-bnb/10 px-3 py-1.5 font-semibold text-bnb hover:bg-bnb/20">
                Open the live dashboard →
              </a>
            ) : null}
          </div>
        ) : (
          <>
            <KpiStrip status={status} portfolio={portfolio} edge={snap.edge} opportunity={snap.opportunity} positions={snap.positions} decisions={snap.decisions} />

            <div className="grid grid-cols-1 gap-3 lg:grid-cols-12">
              <div className="flex min-w-0 lg:col-span-5">
                <SpreadChart history={snap.history} market={market} minEdgeBps={status?.min_edge_bps ?? snap.opportunity?.min_edge_bps_used ?? null} />
              </div>
              <div className="flex min-w-0 lg:col-span-4">
                <PositionsTable positions={snap.positions} portfolio={portfolio} market={market} status={status} receipts={snap.receipts} mock={mock} onReceipt={setSelectedTrace} />
              </div>
              <div className="flex min-w-0 lg:col-span-3 lg:row-span-2">
                <RiskGateLog decisions={snap.decisions} status={status} mock={mock} onChanged={refresh} />
              </div>
              <div className="flex min-w-0 lg:col-span-5">
                <EdgeWaterfall
                  edge={snap.edge}
                  market={market}
                  actionable={snap.opportunity ? snap.opportunity.is_actionable : null}
                  reason={snap.opportunity?.reason ?? null}
                  mock={mock}
                />
              </div>
              <div className="flex min-w-0 flex-col gap-3 lg:col-span-4">
                <PromptConsole status={status} mock={mock} onTrace={setSelectedTrace} onChanged={refresh} />
                <McpActivity activity={snap.activity} status={status} onSelectTrace={setSelectedTrace} />
              </div>
              <div className="flex min-w-0 lg:col-span-12">
                <TradeTrace receipts={snap.receipts} prompts={snap.prompts} selectedTraceId={selectedTrace} onSelectTrace={setSelectedTrace} />
              </div>
            </div>
          </>
        )}
        <footer className="px-1 py-3 text-center text-[11px] text-gray-500">
          Deltr never takes a directional bet and no order reaches Binance without the deterministic zero-LLM risk gate. Funding figures in
          paper mode are testnet-derived and indicative.
        </footer>
      </main>
    </div>
  );
}
