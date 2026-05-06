import type { TokenTraderRow } from "../api";
import {
  formatTimestamp,
  formatTokenAmount,
  formatUsd,
  formatUsdPrice,
  pnlColorClass,
  shortAddress,
} from "../format";

interface Props {
  rows: TokenTraderRow[];
}

export function TokenTradersTable({ rows }: Props) {
  if (!rows.length) {
    return <p className="empty">No token trades found in the fetched pool events.</p>;
  }
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>Wallet</th>
            <th className="num">Bought</th>
            <th className="num">Sold</th>
            <th className="num">Balance</th>
            <th className="num">Buy USD</th>
            <th className="num">Sell USD</th>
            <th className="num">Avg buy</th>
            <th className="num">Price</th>
            <th className="num">Realized</th>
            <th className="num">Unrealized</th>
            <th className="num">Total PnL</th>
            <th className="num">Trades</th>
            <th>Last trade</th>
            <th>Flags</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.wallet}>
              <td className="mono" title={row.wallet}>
                <a
                  href={`https://tonviewer.com/${row.wallet}`}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {shortAddress(row.wallet)}
                </a>
              </td>
              <td className="num">{formatTokenAmount(row.total_bought)}</td>
              <td className="num">{formatTokenAmount(row.total_sold)}</td>
              <td className="num">{formatTokenAmount(row.estimated_balance)}</td>
              <td className="num">{formatUsd(row.buy_volume_usd)}</td>
              <td className="num">{formatUsd(row.sell_volume_usd)}</td>
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
              <td className="num">{row.trade_count}</td>
              <td>{row.last_trade_ts ? formatTimestamp(row.last_trade_ts) : "—"}</td>
              <td>
                {row.only_sells && <span className="badge badge--warn">only sells</span>}
                {!row.only_sells && row.sold_more_than_bought && (
                  <span className="badge badge--warn">sold &gt; bought</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
