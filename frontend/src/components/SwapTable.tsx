import { useState } from "react";

import type { Swap } from "../api";
import { formatTimestamp, formatTokenAmount, formatUsd } from "../format";

const PAGE_SIZE = 25;

interface Props {
  swaps: Swap[];
}

export function SwapTable({ swaps }: Props) {
  const [page, setPage] = useState(0);
  if (!swaps.length) {
    return <p className="empty">No swaps to display.</p>;
  }
  // Newest swaps first feels more natural in a transaction log.
  const ordered = [...swaps].sort((a, b) => b.timestamp - a.timestamp);
  const totalPages = Math.ceil(ordered.length / PAGE_SIZE);
  const safePage = Math.min(page, totalPages - 1);
  const slice = ordered.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>DEX</th>
            <th>Sold</th>
            <th>Received</th>
            <th className="num">USD value</th>
          </tr>
        </thead>
        <tbody>
          {slice.map((s) => (
            <tr key={`${s.event_id}-${s.timestamp}-${s.asset_in.asset_id}`}>
              <td>{formatTimestamp(s.timestamp)}</td>
              <td>
                <span className="badge">{s.dex}</span>
              </td>
              <td>
                <span className="amount-leg amount-leg--out">
                  {formatTokenAmount(s.amount_in)} {s.asset_in.symbol}
                </span>
              </td>
              <td>
                <span className="amount-leg amount-leg--in">
                  {formatTokenAmount(s.amount_out)} {s.asset_out.symbol}
                </span>
              </td>
              <td className="num">{formatUsd(s.usd_value ?? null)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {totalPages > 1 && (
        <div className="pagination">
          <button onClick={() => setPage((p) => Math.max(0, p - 1))} disabled={safePage === 0}>
            ← Prev
          </button>
          <span>
            Page {safePage + 1} / {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            disabled={safePage >= totalPages - 1}
          >
            Next →
          </button>
        </div>
      )}
    </div>
  );
}
