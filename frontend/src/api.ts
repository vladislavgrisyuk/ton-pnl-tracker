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

export interface TokenTraderRow {
  wallet: string;
  token: TokenInfo;
  total_bought: number;
  total_sold: number;
  estimated_balance: number;
  buy_volume_usd: number;
  sell_volume_usd: number;
  avg_buy_price_usd: number;
  current_price_usd: number | null;
  current_value_usd: number;
  realized_pnl_usd: number;
  unrealized_pnl_usd: number;
  total_pnl_usd: number;
  trade_count: number;
  first_trade_ts?: number | null;
  last_trade_ts?: number | null;
  only_sells: boolean;
  sold_more_than_bought: boolean;
}

export interface TokenAnalyticsProgress {
  status: "queued" | "running" | "completed" | "failed";
  processed_events: number;
  discovered_wallets: number;
  processed_wallets: number;
  trade_count: number;
  message?: string | null;
}

export interface TokenAnalyticsReport {
  pool: string;
  token: TokenInfo;
  current_price_usd: number | null;
  trader_count: number;
  trade_count: number;
  processed_events: number;
  rows: TokenTraderRow[];
  warnings?: string[] | null;
}

export interface TokenAnalyticsJob {
  job_id: string;
  progress: TokenAnalyticsProgress;
  report?: TokenAnalyticsReport | null;
  error?: string | null;
}

export interface FetchPnLOptions {
  refresh?: boolean;
  limit?: number;
  rps?: number;
  batchSize?: number;
  signal?: AbortSignal;
}

export interface StartTokenAnalyticsOptions {
  tokenAddress?: string;
  limit?: number;
  rps?: number;
  batchSize?: number;
  signal?: AbortSignal;
}

async function parseError(resp: Response): Promise<string> {
  let msg = `${resp.status} ${resp.statusText}`;
  try {
    const body = (await resp.json()) as { detail?: string };
    if (body?.detail) msg = body.detail;
  } catch {
    // ignore — leave default message
  }
  return msg;
}

export async function fetchWalletPnL(
  address: string,
  { refresh, limit, rps, batchSize, signal }: FetchPnLOptions = {},
): Promise<PnLReport> {
  const params = new URLSearchParams();
  if (refresh) params.set("refresh", "true");
  if (limit) params.set("limit", String(limit));
  if (rps && Number.isFinite(rps) && rps > 0) params.set("rps", String(rps));
  if (batchSize && Number.isFinite(batchSize) && batchSize > 0) {
    params.set("batch_size", String(batchSize));
  }
  const qs = params.toString();
  const url = `${API_BASE}/api/wallet/${encodeURIComponent(address)}/pnl${qs ? `?${qs}` : ""}`;
  const resp = await fetch(url, { signal });
  if (!resp.ok) {
    throw new Error(await parseError(resp));
  }
  return (await resp.json()) as PnLReport;
}

export async function startTokenAnalyticsJob(
  poolAddress: string,
  { tokenAddress, limit, rps, batchSize, signal }: StartTokenAnalyticsOptions = {},
): Promise<TokenAnalyticsJob> {
  const params = new URLSearchParams();
  if (tokenAddress?.trim()) params.set("token_address", tokenAddress.trim());
  if (limit && Number.isFinite(limit) && limit > 0) params.set("limit", String(limit));
  if (rps && Number.isFinite(rps) && rps > 0) params.set("rps", String(rps));
  if (batchSize && Number.isFinite(batchSize) && batchSize > 0) {
    params.set("batch_size", String(batchSize));
  }
  const qs = params.toString();
  const url = `${API_BASE}/api/token-analytics/${encodeURIComponent(poolAddress)}/jobs${qs ? `?${qs}` : ""}`;
  const resp = await fetch(url, { method: "POST", signal });
  if (!resp.ok) {
    throw new Error(await parseError(resp));
  }
  return (await resp.json()) as TokenAnalyticsJob;
}

export async function fetchTokenAnalyticsJob(
  jobId: string,
  signal?: AbortSignal,
): Promise<TokenAnalyticsJob> {
  const resp = await fetch(`${API_BASE}/api/token-analytics/jobs/${encodeURIComponent(jobId)}`, {
    signal,
  });
  if (!resp.ok) {
    throw new Error(await parseError(resp));
  }
  return (await resp.json()) as TokenAnalyticsJob;
}

// ---------- Multi-pool batch analytics + DB browser ---------------------

export interface MultiPoolChildStatus {
  pool: string;
  token_address?: string | null;
  progress: TokenAnalyticsProgress;
  report?: TokenAnalyticsReport | null;
  error?: string | null;
  persisted_rows: number;
}

export interface MultiPoolJob {
  job_id: string;
  status: "queued" | "running" | "completed" | "failed" | "partial";
  started_at?: number | null;
  finished_at?: number | null;
  children: MultiPoolChildStatus[];
  error?: string | null;
  total_persisted_rows: number;
}

export interface StartMultiPoolOptions {
  raw?: string;
  pools?: string[];
  limit?: number;
  rps?: number;
  batchSize?: number;
  maxConcurrency?: number;
  signal?: AbortSignal;
}

export async function startMultiPoolJob({
  raw,
  pools,
  limit,
  rps,
  batchSize,
  maxConcurrency,
  signal,
}: StartMultiPoolOptions): Promise<MultiPoolJob> {
  const body: Record<string, unknown> = {};
  if (raw) body.raw = raw;
  if (pools?.length) body.pools = pools;
  if (limit && Number.isFinite(limit) && limit > 0) body.limit = limit;
  if (rps && Number.isFinite(rps) && rps > 0) body.rps = rps;
  if (batchSize && Number.isFinite(batchSize) && batchSize > 0) body.batch_size = batchSize;
  if (maxConcurrency && Number.isFinite(maxConcurrency) && maxConcurrency > 0) {
    body.max_concurrency = maxConcurrency;
  }
  const resp = await fetch(`${API_BASE}/api/multi-pool-analytics/jobs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!resp.ok) {
    throw new Error(await parseError(resp));
  }
  return (await resp.json()) as MultiPoolJob;
}

export async function fetchMultiPoolJob(
  jobId: string,
  signal?: AbortSignal,
): Promise<MultiPoolJob> {
  const resp = await fetch(
    `${API_BASE}/api/multi-pool-analytics/jobs/${encodeURIComponent(jobId)}`,
    { signal },
  );
  if (!resp.ok) {
    throw new Error(await parseError(resp));
  }
  return (await resp.json()) as MultiPoolJob;
}

export interface WalletTokenStatRow {
  wallet: string;
  token_master: string;
  pool_address: string;
  token_symbol: string | null;
  token_name: string | null;
  token_decimals: number | null;
  token_image: string | null;
  total_bought: number;
  total_sold: number;
  estimated_balance: number;
  buy_volume_usd: number;
  sell_volume_usd: number;
  avg_buy_price_usd: number | null;
  current_price_usd: number | null;
  current_value_usd: number;
  realized_pnl_usd: number;
  unrealized_pnl_usd: number;
  total_pnl_usd: number;
  trade_count: number;
  first_trade_ts: number | null;
  last_trade_ts: number | null;
  only_sells: boolean;
  sold_more_than_bought: boolean;
  updated_at: number;
}

export interface TokenSummaryRow {
  token_master: string;
  token_symbol: string | null;
  token_name: string | null;
  token_image: string | null;
  token_decimals: number | null;
  wallet_count: number;
  total_buy_usd: number;
  total_sell_usd: number;
  last_updated: number | null;
}

export interface WalletTokenStatsResponse {
  rows: WalletTokenStatRow[];
  tokens: TokenSummaryRow[];
  db_stats: {
    row_count: number | null;
    wallet_count: number | null;
    token_count: number | null;
    last_updated: number | null;
  };
}

export interface QueryWalletTokenStatsOptions {
  tokenMaster?: string;
  wallet?: string;
  minTotalPnlUsd?: number;
  maxTotalPnlUsd?: number;
  onlyWithBalance?: boolean;
  sort?: string;
  limit?: number;
  offset?: number;
  signal?: AbortSignal;
}

export async function queryWalletTokenStats({
  tokenMaster,
  wallet,
  minTotalPnlUsd,
  maxTotalPnlUsd,
  onlyWithBalance,
  sort,
  limit,
  offset,
  signal,
}: QueryWalletTokenStatsOptions = {}): Promise<WalletTokenStatsResponse> {
  const params = new URLSearchParams();
  if (tokenMaster?.trim()) params.set("token_master", tokenMaster.trim());
  if (wallet?.trim()) params.set("wallet", wallet.trim());
  if (minTotalPnlUsd !== undefined && Number.isFinite(minTotalPnlUsd)) {
    params.set("min_total_pnl_usd", String(minTotalPnlUsd));
  }
  if (maxTotalPnlUsd !== undefined && Number.isFinite(maxTotalPnlUsd)) {
    params.set("max_total_pnl_usd", String(maxTotalPnlUsd));
  }
  if (onlyWithBalance) params.set("only_with_balance", "true");
  if (sort) params.set("sort", sort);
  if (limit && limit > 0) params.set("limit", String(limit));
  if (offset && offset > 0) params.set("offset", String(offset));
  const qs = params.toString();
  const resp = await fetch(`${API_BASE}/api/wallet-token-stats${qs ? `?${qs}` : ""}`, {
    signal,
  });
  if (!resp.ok) {
    throw new Error(await parseError(resp));
  }
  return (await resp.json()) as WalletTokenStatsResponse;
}
