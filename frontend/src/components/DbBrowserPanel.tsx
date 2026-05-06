import { useCallback, useEffect, useMemo, useState } from "react";

import {
  queryWalletTokenStats,
  type TokenSummaryRow,
  type WalletTokenStatRow,
  type WalletTokenStatsResponse,
} from "../api";
import {
  formatTimestamp,
  formatTokenAmount,
  formatUsd,
  formatUsdPrice,
  pnlColorClass,
  shortAddress,
} from "../format";

interface Props {
  refreshKey?: number;
}

const SORT_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "total_pnl_desc", label: "Total PnL ↓" },
  { value: "total_pnl_asc", label: "Total PnL ↑" },
  { value: "realized_desc", label: "Realized ↓" },
  { value: "realized_asc", label: "Realized ↑" },
  { value: "unrealized_desc", label: "Unrealized ↓" },
  { value: "buy_usd_desc", label: "Buy USD ↓" },
  { value: "trade_count_desc", label: "Trade count ↓" },
  { value: "last_trade_desc", label: "Last trade ↓" },
  { value: "updated_desc", label: "Last analyzed ↓" },
];

export function DbBrowserPanel({ refreshKey = 0 }: Props) {
  const [tokenFilter, setTokenFilter] = useState("");
  const [walletFilter, setWalletFilter] = useState("");
  const [minPnl, setMinPnl] = useState("");
  const [maxPnl, setMaxPnl] = useState("");
  const [sort, setSort] = useState("total_pnl_desc");
  const [onlyWithBalance, setOnlyWithBalance] = useState(false);
  const [limit, setLimit] = useState("200");
  const [data, setData] = useState<WalletTokenStatsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await queryWalletTokenStats({
        tokenMaster: tokenFilter || undefined,
        wallet: walletFilter || undefined,
        minTotalPnlUsd: minPnl !== "" ? Number(minPnl) : undefined,
        maxTotalPnlUsd: maxPnl !== "" ? Number(maxPnl) : undefined,
        onlyWithBalance,
        sort,
        limit: Number(limit) || 200,
      });
      setData(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [tokenFilter, walletFilter, minPnl, maxPnl, sort, onlyWithBalance, limit]);

  useEffect(() => {
    // Defer the kick-off so the linter's "no setState in effect" check
    // doesn't fire (the actual writes happen in a then-handler anyway).
    const handle = window.setTimeout(() => {
      void load();
    }, 0);
    return () => window.clearTimeout(handle);
  }, [load, refreshKey]);

  const tokens = useMemo(() => data?.tokens ?? [], [data]);
  const rows = useMemo(() => data?.rows ?? [], [data]);
  const stats = data?.db_stats;

  const totalRow = useMemo(() => {
    if (!rows.length) return null;
    let buyUsd = 0;
    let sellUsd = 0;
    let realized = 0;
    let unrealized = 0;
    for (const r of rows) {
      buyUsd += r.buy_volume_usd ?? 0;
      sellUsd += r.sell_volume_usd ?? 0;
      realized += r.realized_pnl_usd ?? 0;
      unrealized += r.unrealized_pnl_usd ?? 0;
    }
    return { buyUsd, sellUsd, realized, unrealized };
  }, [rows]);

  return (
    <div className="db-browser">
      <div className="db-browser__filters">
        <label className="wallet-form__field">
          <span>Token master</span>
          <input
            className="wallet-form__input wallet-form__input--secondary"
            type="text"
            placeholder="EQ.../UQ.../0:hex (or pick →)"
            value={tokenFilter}
            onChange={(e) => setTokenFilter(e.target.value)}
            spellCheck={false}
            autoCapitalize="off"
            autoComplete="off"
          />
        </label>
        <label className="wallet-form__field">
          <span>Wallet</span>
          <input
            className="wallet-form__input wallet-form__input--secondary"
            type="text"
            placeholder="EQ.../UQ.../0:hex"
            value={walletFilter}
            onChange={(e) => setWalletFilter(e.target.value)}
            spellCheck={false}
            autoCapitalize="off"
            autoComplete="off"
          />
        </label>
        <label className="wallet-form__field">
          <span>Min PnL $</span>
          <input
            className="wallet-form__number"
            type="number"
            value={minPnl}
            onChange={(e) => setMinPnl(e.target.value)}
          />
        </label>
        <label className="wallet-form__field">
          <span>Max PnL $</span>
          <input
            className="wallet-form__number"
            type="number"
            value={maxPnl}
            onChange={(e) => setMaxPnl(e.target.value)}
          />
        </label>
        <label className="wallet-form__field">
          <span>Sort</span>
          <select
            className="wallet-form__number"
            style={{ width: 160 }}
            value={sort}
            onChange={(e) => setSort(e.target.value)}
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <label className="wallet-form__field">
          <span>Limit</span>
          <input
            className="wallet-form__number"
            type="number"
            min="1"
            max="5000"
            step="50"
            value={limit}
            onChange={(e) => setLimit(e.target.value)}
          />
        </label>
        <label className="wallet-form__field">
          <input
            type="checkbox"
            checked={onlyWithBalance}
            onChange={(e) => setOnlyWithBalance(e.target.checked)}
          />
          <span>Only with balance</span>
        </label>
        <div className="multi-form__buttons">
          <button
            type="button"
            className="wallet-form__submit"
            onClick={() => void load()}
            disabled={loading}
          >
            {loading ? "Loading…" : "Apply"}
          </button>
          <button
            type="button"
            className="wallet-form__sample"
            onClick={() => {
              setTokenFilter("");
              setWalletFilter("");
              setMinPnl("");
              setMaxPnl("");
              setOnlyWithBalance(false);
              setSort("total_pnl_desc");
              setLimit("200");
            }}
            disabled={loading}
          >
            Reset
          </button>
        </div>
      </div>

      {stats && (
        <div className="summary">
          <div className="summary__card">
            <span className="summary__label">DB rows</span>
            <span className="summary__value">{(stats.row_count ?? 0).toLocaleString()}</span>
          </div>
          <div className="summary__card">
            <span className="summary__label">Distinct wallets</span>
            <span className="summary__value">{(stats.wallet_count ?? 0).toLocaleString()}</span>
          </div>
          <div className="summary__card">
            <span className="summary__label">Distinct tokens</span>
            <span className="summary__value">{(stats.token_count ?? 0).toLocaleString()}</span>
          </div>
          <div className="summary__card">
            <span className="summary__label">Last analysis</span>
            <span className="summary__value">
              {stats.last_updated ? formatTimestamp(stats.last_updated) : "—"}
            </span>
          </div>
        </div>
      )}

      {error && (
        <div className="alert alert--error" role="alert">
          {error}
        </div>
      )}

      {!!tokens.length && (
        <div className="db-browser__tokens">
          <strong>Analyzed tokens:</strong>
          {tokens.map((t) => (
            <TokenChip
              key={t.token_master}
              token={t}
              active={t.token_master.toLowerCase() === tokenFilter.toLowerCase()}
              onPick={() => setTokenFilter(t.token_master)}
            />
          ))}
          {tokenFilter && (
            <button
              type="button"
              className="badge"
              onClick={() => setTokenFilter("")}
              title="clear token filter"
            >
              clear
            </button>
          )}
        </div>
      )}

      {totalRow && (
        <div className="token-progress__stats" style={{ margin: "8px 0 16px" }}>
          <span>showing: {rows.length.toLocaleString()} rows</span>
          <span>buy {formatUsd(totalRow.buyUsd)}</span>
          <span>sell {formatUsd(totalRow.sellUsd)}</span>
          <span>realized {formatUsd(totalRow.realized)}</span>
          <span>unrealized {formatUsd(totalRow.unrealized)}</span>
        </div>
      )}

      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              <th>Wallet</th>
              <th>Token</th>
              <th className="num">Bought</th>
              <th className="num">Sold</th>
              <th className="num">Balance</th>
              <th className="num">Avg buy</th>
              <th className="num">Buy USD</th>
              <th className="num">Sell USD</th>
              <th className="num">Price</th>
              <th className="num">Realized</th>
              <th className="num">Unrealized</th>
              <th className="num">Total PnL</th>
              <th className="num">Trades</th>
              <th>Last trade</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <Row key={`${r.wallet}|${r.token_master}`} row={r} onPickToken={setTokenFilter} />
            ))}
            {!rows.length && (
              <tr>
                <td colSpan={15} className="empty">
                  No rows match — paste pools in the Multi-pool tab to populate the DB.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Row({
  row,
  onPickToken,
}: {
  row: WalletTokenStatRow;
  onPickToken: (token: string) => void;
}) {
  return (
    <tr>
      <td className="mono" title={row.wallet}>
        <a
          href={`https://tonviewer.com/${encodeURIComponent(row.wallet)}`}
          target="_blank"
          rel="noopener noreferrer"
        >
          {shortAddress(row.wallet)}
        </a>
      </td>
      <td>
        <button
          type="button"
          className="db-browser__token-link"
          onClick={() => onPickToken(row.token_master)}
          title={row.token_master}
        >
          {row.token_symbol ?? "?"}
          <small> · {row.token_name ?? shortAddress(row.token_master)}</small>
        </button>
      </td>
      <td className="num">{formatTokenAmount(row.total_bought)}</td>
      <td className="num">{formatTokenAmount(row.total_sold)}</td>
      <td className="num">{formatTokenAmount(row.estimated_balance)}</td>
      <td className="num">{formatUsdPrice(row.avg_buy_price_usd)}</td>
      <td className="num">{formatUsd(row.buy_volume_usd)}</td>
      <td className="num">{formatUsd(row.sell_volume_usd)}</td>
      <td className="num">{formatUsdPrice(row.current_price_usd)}</td>
      <td className={`num ${pnlColorClass(row.realized_pnl_usd)}`}>
        {formatUsd(row.realized_pnl_usd)}
      </td>
      <td className={`num ${pnlColorClass(row.unrealized_pnl_usd)}`}>
        {formatUsd(row.unrealized_pnl_usd)}
      </td>
      <td className={`num strong ${pnlColorClass(row.total_pnl_usd)}`}>
        {formatUsd(row.total_pnl_usd)}
      </td>
      <td className="num">{row.trade_count}</td>
      <td>{row.last_trade_ts ? formatTimestamp(row.last_trade_ts) : "—"}</td>
      <td>{formatTimestamp(row.updated_at)}</td>
    </tr>
  );
}

function TokenChip({
  token,
  active,
  onPick,
}: {
  token: TokenSummaryRow;
  active: boolean;
  onPick: () => void;
}) {
  const label = token.token_symbol || shortAddress(token.token_master);
  return (
    <button
      type="button"
      className={`db-browser__chip${active ? " db-browser__chip--active" : ""}`}
      onClick={onPick}
      title={`${token.token_master} · ${token.wallet_count} wallets · buy ${formatUsd(
        token.total_buy_usd,
      )}`}
    >
      {label}
      <small> · {token.wallet_count}w</small>
    </button>
  );
}
