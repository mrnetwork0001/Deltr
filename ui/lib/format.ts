// Number / time formatting helpers. Every figure the dashboard prints goes through here.

const nf = (min: number, max: number) =>
  new Intl.NumberFormat("en-US", { minimumFractionDigits: min, maximumFractionDigits: max });

const F0 = nf(0, 0);
const F1 = nf(1, 1);
const F2 = nf(2, 2);

export function isNum(x: unknown): x is number {
  return typeof x === "number" && Number.isFinite(x);
}

/** Basis points with sign: "+2.2 bps" / "-16.6 bps". */
export function bps(x: number | null | undefined, digits = 1, unit = true): string {
  if (!isNum(x)) return "–";
  const s = x > 0 ? "+" : x < 0 ? "-" : "";
  return `${s}${nf(digits, digits).format(Math.abs(x))}${unit ? " bps" : ""}`;
}

/** US dollars: "$10,000" / "-$5.40". `digits` defaults to 2 below 1,000, else 0. */
export function usd(x: number | null | undefined, digits?: number): string {
  if (!isNum(x)) return "–";
  const d = digits ?? (Math.abs(x) < 1000 ? 2 : 0);
  const s = x < 0 ? "-" : "";
  return `${s}$${nf(d, d).format(Math.abs(x))}`;
}

/** Signed dollars: "+$1.20" / "-$5.40". */
export function usdSigned(x: number | null | undefined, digits = 2): string {
  if (!isNum(x)) return "–";
  const s = x > 0 ? "+" : x < 0 ? "-" : "";
  return `${s}$${nf(digits, digits).format(Math.abs(x))}`;
}

/** Microseconds: "1.04 µs". */
export function us(x: number | null | undefined, digits = 2): string {
  if (!isNum(x)) return "–";
  return `${nf(digits, digits).format(x)} µs`;
}

/** Milliseconds: "41 ms" (sub-ms shown with one decimal). */
export function ms(x: number | null | undefined): string {
  if (!isNum(x)) return "–";
  return x < 1 ? `${F1.format(x)} ms` : `${F0.format(x)} ms`;
}

/** Feed age: "412 ms" / "2.9 s" / "1m 12s". */
export function age(msAge: number | null | undefined): string {
  if (!isNum(msAge)) return "–";
  if (msAge < 1000) return `${F0.format(msAge)} ms`;
  if (msAge < 60_000) return `${F1.format(msAge / 1000)} s`;
  const m = Math.floor(msAge / 60_000);
  const s = Math.round((msAge % 60_000) / 1000);
  return `${m}m ${s}s`;
}

/** Percent: "0.05 %" (input already in percent units). */
export function pct(x: number | null | undefined, digits = 2): string {
  if (!isNum(x)) return "–";
  return `${nf(digits, digits).format(x)} %`;
}

/** Funding rate as a percent per interval: "0.0100 %". */
export function rate(x: number | null | undefined): string {
  if (!isNum(x)) return "–";
  return `${(x >= 0 ? "+" : "-")}${nf(4, 4).format(Math.abs(x) * 100)} %`;
}

/** Price: "686.19". */
export function px(x: number | null | undefined, digits = 2): string {
  if (!isNum(x)) return "–";
  return nf(digits, digits).format(x);
}

/** Base quantity: "4.85 BNB". */
export function qty(x: number | null | undefined, base = "BNB", digits = 2): string {
  if (!isNum(x)) return "–";
  return `${nf(digits, digits).format(x)} ${base}`;
}

/** Leverage: "2.0x". */
export function lev(x: number | null | undefined): string {
  if (!isNum(x)) return "–";
  return `${F1.format(x)}x`;
}

export function num(x: number | null | undefined, digits = 2): string {
  if (!isNum(x)) return "–";
  return nf(0, digits).format(x);
}

/** ISO -> "14:32:10". */
export function clock(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "–";
  return d.toISOString().slice(11, 19);
}

/** ISO -> "14:32:10.412". */
export function clockMs(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "–";
  return d.toISOString().slice(11, 23);
}

/** Countdown to an epoch-ms instant: "1h 27m" / "12m 05s" / "now". */
export function countdown(epochMs: number | null | undefined, nowMs = Date.now()): string {
  if (!isNum(epochMs)) return "–";
  const dt = Math.max(0, epochMs - nowMs);
  if (dt < 1000) return "now";
  const h = Math.floor(dt / 3_600_000);
  const m = Math.floor((dt % 3_600_000) / 60_000);
  const s = Math.floor((dt % 60_000) / 1000);
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

/** Uptime seconds -> "30m 42s" / "2h 05m". */
export function uptime(sec: number | null | undefined): string {
  if (!isNum(sec)) return "–";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

/** Short hash: "559909e8…c150". */
export function shortHash(h: string | null | undefined, head = 8, tail = 4): string {
  if (!h) return "–";
  return h.length <= head + tail + 1 ? h : `${h.slice(0, head)}…${h.slice(-tail)}`;
}

/** Any observed/limit value from a CheckResult, printed compactly. */
export function anyVal(v: unknown): string {
  if (v === null || v === undefined) return "–";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return Number.isInteger(v) ? F0.format(v) : nf(0, 3).format(v);
  if (typeof v === "string") return v;
  try {
    const s = JSON.stringify(v);
    return s.length > 40 ? s.slice(0, 39) + "…" : s;
  } catch {
    return String(v);
  }
}

export const F = { F0, F1, F2 };

// Palette (validated on the dark surface, see UI_GUIDE.md). Series colours are
// fixed by ENTITY: perp is always blue, DEX always orange, never by rank.
export const SERIES = {
  perp: "#3987e5",
  dex: "#d95926",
  edge: "#199e70",
  funding: "#c98500",
} as const;

// Status colours are reserved for status (never a data series); always paired with an icon + label.
export const STATUS = {
  good: "#0ca30c",
  warning: "#fab219",
  serious: "#ec835a",
  critical: "#d03b3b",
} as const;

export const SURFACE = {
  page: "#07090f",
  card: "#0b0f19",
  raised: "#111827",
  hairline: "#1f2937",
  grid: "#141a26",
  text: "#e5e7eb",
  text2: "#9ca3af",
  muted: "#6b7280",
} as const;

/** Human label for a DataSource provenance tag. */
export function sourceLabel(s: string | null | undefined): string {
  switch (s) {
    case "bsc-mainnet-chain":
      return "BSC mainnet chain";
    case "binance-futures-mainnet":
      return "Binance Futures mainnet";
    case "binance-futures-testnet":
      return "Binance Futures testnet";
    case "binance-spot-mirror":
      return "Binance spot mirror";
    case "paper":
      return "paper fill";
    case "replay":
      return "replay";
    case "simulated":
      return "simulated";
    default:
      return s ?? "unknown";
  }
}
