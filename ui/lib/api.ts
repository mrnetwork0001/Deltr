// REST + WebSocket client for the Deltr FastAPI backend (design section 6.1 / 6.2).
// API_BASE is "" when the static export is served by FastAPI (same origin);
// `NEXT_PUBLIC_API=http://127.0.0.1:8000` for `next dev`.
import type {
  AgentEvent,
  ApiError,
  ExecutionReceipt,
  MinEdgeResponse,
  PromptResult,
  ProposeResponse,
  RiskDecisionRecord,
  Snapshot,
  StressResult,
  StressScenario,
  SystemStatus,
  WsFrame,
  EdgeComponent,
} from "@/lib/types";

export const API_BASE: string = process.env.NEXT_PUBLIC_API ?? "";

// --------------------------------------------------------------------------- engines
// A deployment can expose more than one engine (the public showcase runs a PAPER one and a
// LIVE one, both read-only). NEXT_PUBLIC_ENGINES lists them as "key=base|key=base", where base
// is a path prefix the host proxies ("/live") or an absolute origin. Empty base = this origin.
export interface EngineDef {
  key: string;
  label: string;
  base: string;
}

export function engines(): EngineDef[] {
  const raw = process.env.NEXT_PUBLIC_ENGINES ?? "";
  const out: EngineDef[] = [];
  for (const part of raw.split("|")) {
    const [key, base = ""] = part.split("=");
    const k = key.trim();
    if (k) out.push({ key: k, label: k.toUpperCase(), base: base.trim().replace(/\/$/, "") });
  }
  return out;
}

let engineBase = "";
/** Select the engine every call below talks to; "" is the default (same-origin) engine. */
export function setEngine(base: string): void {
  engineBase = base.replace(/\/$/, "");
}
export function apiBase(): string {
  return `${API_BASE}${engineBase}`;
}

export class DeltrApiError extends Error {
  code: string;
  status: number;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "DeltrApiError";
    this.status = status;
    this.code = code;
  }
}

async function parseError(res: Response): Promise<DeltrApiError> {
  let code = `HTTP_${res.status}`;
  let message = res.statusText || "request failed";
  try {
    const body = (await res.json()) as Partial<ApiError>;
    if (body && body.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
    }
  } catch {
    /* non-JSON error body */
  }
  return new DeltrApiError(res.status, code, message);
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`, { cache: "no-store", signal });
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Deltr-Source": "ui" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as T;
}

// --------------------------------------------------------------------------- GET
export function fetchSnapshot(signal?: AbortSignal): Promise<Snapshot> {
  return getJson<Snapshot>("/api/snapshot", signal);
}

export function fetchMarket(horizonH?: number) {
  const q = horizonH ? `?horizon_h=${horizonH}` : "";
  return getJson<{ market: Snapshot["market"]; edge: Snapshot["edge"]; components?: EdgeComponent[] | null }>(
    `/api/market${q}`,
  );
}

/** The bundled mock (copied into ui/public/mock so the static export serves it). */
export async function fetchMockSnapshot(): Promise<Snapshot> {
  const res = await fetch("/mock/snapshot.json", { cache: "no-store" });
  if (!res.ok) throw new Error(`mock snapshot unavailable (${res.status})`);
  return (await res.json()) as Snapshot;
}

// --------------------------------------------------------------------------- POST (all send X-Deltr-Source: ui)
export function postPrompt(text: string): Promise<PromptResult> {
  return postJson<PromptResult>("/api/prompt", { text });
}

export function postPropose(body: { capital_usd: number; leverage?: number; symbol?: string }): Promise<ProposeResponse> {
  return postJson<ProposeResponse>("/api/hedge/propose", body);
}

export function postExecute(body: { plan_id: string; confirm?: boolean }): Promise<ExecutionReceipt> {
  return postJson<ExecutionReceipt>("/api/hedge/execute", body);
}

export function postUnwind(body: { position_id: string | "all"; reason?: string; confirm?: boolean }): Promise<ExecutionReceipt[]> {
  return postJson<ExecutionReceipt[]>("/api/hedge/unwind", body);
}

export function postKill(body: { on: boolean; reason?: string }): Promise<SystemStatus> {
  return postJson<SystemStatus>("/api/kill", body);
}

export function postResetHalt(body: { reason: string }): Promise<SystemStatus> {
  return postJson<SystemStatus>("/api/risk/reset_halt", body);
}

export function postStress(body: StressScenario): Promise<StressResult> {
  return postJson<StressResult>("/api/stress", body);
}

export function postMinEdge(body: { min_edge_bps: number }): Promise<MinEdgeResponse> {
  return postJson<MinEdgeResponse>("/api/scout/min_edge", body);
}

export function postEvaluate(body: { capital_usd: number; leverage: number; symbol?: string }): Promise<RiskDecisionRecord> {
  return postJson<RiskDecisionRecord>("/api/risk/evaluate", body);
}

// --------------------------------------------------------------------------- live stream (WS accelerator + 1 Hz poll fallback)
export interface StreamHandlers {
  onSnapshot: (s: Snapshot, transport: "ws" | "poll") => void;
  onEvent?: (e: AgentEvent) => void;
  onError?: (err: unknown) => void;
  onTransport?: (t: "ws" | "poll" | "down") => void;
}

export interface StreamHandle {
  stop: () => void;
}

function wsUrl(): string {
  const b = apiBase();
  const abs = /^https?:\/\//.test(b) ? b : (typeof window !== "undefined" ? window.location.origin : "") + b;
  return abs.replace(/^http/, "ws") + "/ws/stream";
}

/**
 * Section 6.2: WS is an accelerator. If the socket is not open within 1.5 s or
 * drops, poll GET /api/snapshot at 1 Hz. One WS retry per 10 s, nothing more.
 * The poller runs whenever the socket is not open, so the UI is never starved.
 */
export function openStream(h: StreamHandlers, pollMs = 1000): StreamHandle {
  let stopped = false;
  let ws: WebSocket | null = null;
  let wsOpen = false;
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let openTimer: ReturnType<typeof setTimeout> | null = null;
  let inflight = false;
  let lastRetry = 0;

  const setTransport = (t: "ws" | "poll" | "down") => h.onTransport?.(t);

  const poll = async () => {
    if (stopped) return;
    if (!wsOpen && !inflight) {
      inflight = true;
      try {
        const snap = await fetchSnapshot();
        if (!stopped) {
          h.onSnapshot(snap, "poll");
          setTransport("poll");
        }
      } catch (err) {
        if (!stopped) {
          h.onError?.(err);
          setTransport("down");
        }
      } finally {
        inflight = false;
      }
    }
    if (!stopped) pollTimer = setTimeout(poll, pollMs);
  };

  const connect = () => {
    if (stopped || typeof WebSocket === "undefined") return;
    const now = Date.now();
    if (now - lastRetry < 10_000) return; // one retry per 10 s
    lastRetry = now;
    try {
      ws = new WebSocket(wsUrl());
    } catch (err) {
      h.onError?.(err);
      return;
    }
    const sock = ws;
    openTimer = setTimeout(() => {
      if (sock.readyState !== WebSocket.OPEN) {
        try {
          sock.close();
        } catch {
          /* ignore */
        }
      }
    }, 1500);
    sock.onopen = () => {
      wsOpen = true;
      setTransport("ws");
    };
    sock.onmessage = (ev) => {
      let frame: WsFrame | null = null;
      try {
        frame = JSON.parse(String(ev.data)) as WsFrame;
      } catch {
        return;
      }
      if (!frame) return;
      if (frame.type === "snapshot") h.onSnapshot(frame.data, "ws");
      else if (frame.type === "event") h.onEvent?.(frame.data);
    };
    const onDown = () => {
      wsOpen = false;
      if (ws === sock) ws = null;
      if (!stopped) {
        setTransport("poll");
        if (retryTimer) clearTimeout(retryTimer);
        retryTimer = setTimeout(connect, 10_000);
      }
    };
    sock.onclose = onDown;
    sock.onerror = onDown;
  };

  connect();
  void poll();

  return {
    stop: () => {
      stopped = true;
      if (pollTimer) clearTimeout(pollTimer);
      if (retryTimer) clearTimeout(retryTimer);
      if (openTimer) clearTimeout(openTimer);
      try {
        ws?.close();
      } catch {
        /* ignore */
      }
    },
  };
}
