import type { TokenPnL } from "../api";
import {
  formatTokenAmount,
  formatUsd,
  formatUsdPrice,
  pnlColorClass,
} from "../format";

interface Props {
  tokens: TokenPnL[];
}

function TokenIcon({ token }: { token: TokenPnL["token"] }) {
  if (token.image) {
    return (
      <img
        className="token-icon"
        src={token.image}
        alt={token.symbol}
        loading="lazy"
        onError={(e) => {
          (e.currentTarget as HTMLImageElement).style.display = "none";
        }}
      />
    );
  }
  return <span className="token-icon token-icon--placeholder">{token.symbol.slice(0, 2)}</span>;
}

export function TokenTable({ tokens }: Props) {
  if (!tokens.length) {
    return <p className="empty">No token activity found in this wallet's swap history.</p>;
  }
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th className="col-token">Token</th>
            <th className="num">Bought</th>
            <th className="num">Sold</th>
            <th className="num">Balance</th>
            <th className="num">Avg buy</th>
            <th className="num">Price</th>
            <th className="num">Realized</th>
            <th className="num">Unrealized</th>
            <th className="num">Total PnL</th>
            <th className="num">Swaps</th>
          </tr>
        </thead>
        <tbody>
          {tokens.map((row) => (
            <tr key={row.token.asset_id}>
              <td className="col-token">
                <TokenIcon token={row.token} />
                <div className="token-meta">
                  <strong>{row.token.symbol}</strong>
                  <small>{row.token.name}</small>
                </div>
              </td>
              <td className="num">{formatTokenAmount(row.total_bought)}</td>
              <td className="num">{formatTokenAmount(row.total_sold)}</td>
              <td className="num">{formatTokenAmount(row.current_balance)}</td>
              <td className="num">{formatUsdPrice(row.avg_buy_price_usd || null)}</td>
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
              <td className="num">{row.swap_count}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
