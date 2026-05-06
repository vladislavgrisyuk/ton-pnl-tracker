/** Display helpers for tokens and PnL values. */

const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const USD_PRECISE = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 4,
  maximumFractionDigits: 6,
});

const TOKEN = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 0,
  maximumFractionDigits: 4,
});

/** Format a USD amount with sign so callers can render PnL deltas. */
export function formatUsd(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return USD.format(value);
}

/** Compact USD price display, used for current spot prices in the token table. */
export function formatUsdPrice(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  if (Math.abs(value) >= 1) return USD.format(value);
  return `$${USD_PRECISE.format(value)}`;
}

export function formatTokenAmount(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  if (value !== 0 && Math.abs(value) < 0.0001) return value.toExponential(2);
  return TOKEN.format(value);
}

export function formatTimestamp(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

export function shortAddress(addr: string): string {
  if (!addr) return "";
  if (addr.length <= 14) return addr;
  return `${addr.slice(0, 6)}…${addr.slice(-6)}`;
}

export function pnlColorClass(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value) || Math.abs(value) < 0.005) return "pnl-flat";
  return value > 0 ? "pnl-positive" : "pnl-negative";
}
