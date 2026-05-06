/**
 * Thin client around the FastAPI backend. We resolve the base URL from
 * `VITE_API_BASE_URL` (set in `.env` for prod) and fall back to the dev-server
 * default so `npm run dev` works out of the box.
 */

export const TON_ASSET_ID = "TON";

const RAW_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";
export const API_BASE = RAW_BASE.replace(/\/+$/, "");

export interface TokenInfo {
  asset_id: string;
  symbol: string;
  name: string;
  decimals: number;
  image?: string | null;
}

export interface Swap {
  timestamp: number;
  event_id: string;
  dex: string;
  asset_in: TokenInfo;
  asset_out: TokenInfo;
  amount_in_raw: string;
  amount_out_raw: string;
  amount_in: number;
  amount_out: number;
  ton_in?: number | null;
  ton_out?: number | null;
  usd_value?: number | null;
}

export interface TokenPnL {
  token: TokenInfo;
  total_bought: number;
  total_sold: number;
  current_balance: number;
  avg_buy_price_usd: number;
  cost_basis_remaining_usd: number;
  realized_pnl_usd: number;
  unrealized_pnl_usd: number;
  total_pnl_usd: number;
  current_price_usd: number | null;
  current_value_usd: number;
  swap_count: number;
}

export interface PnLReport {
  wallet: string;
  swap_count: number;
  realized_pnl_usd: number;
  unrealized_pnl_usd: number;
  total_pnl_usd: number;
  tokens: TokenPnL[];
  swaps: Swap[];
  warnings?: string[] | null;
}

export interface FetchPnLOptions {
  refresh?: boolean;
  limit?: number;
  signal?: AbortSignal;
}

export async function fetchWalletPnL(
  address: string,
  { refresh, limit, signal }: FetchPnLOptions = {},
): Promise<PnLReport> {
  const params = new URLSearchParams();
  if (refresh) params.set("refresh", "true");
  if (limit) params.set("limit", String(limit));
  const qs = params.toString();
  const url = `${API_BASE}/api/wallet/${encodeURIComponent(address)}/pnl${qs ? `?${qs}` : ""}`;
  const resp = await fetch(url, { signal });
  if (!resp.ok) {
    let msg = `${resp.status} ${resp.statusText}`;
    try {
      const body = (await resp.json()) as { detail?: string };
      if (body?.detail) msg = body.detail;
    } catch {
      // ignore — leave default message
    }
    throw new Error(msg);
  }
  return (await resp.json()) as PnLReport;
}
