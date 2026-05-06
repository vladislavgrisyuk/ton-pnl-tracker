import type { PnLReport } from "../api";
import { formatUsd, pnlColorClass, shortAddress } from "../format";

interface Props {
  report: PnLReport;
}

export function SummaryCards({ report }: Props) {
  return (
    <section className="summary">
      <div className="summary__card">
        <span className="summary__label">Wallet</span>
        <code className="summary__wallet" title={report.wallet}>
          {shortAddress(report.wallet)}
        </code>
      </div>
      <div className="summary__card">
        <span className="summary__label">Swaps analyzed</span>
        <span className="summary__value">{report.swap_count.toLocaleString()}</span>
      </div>
      <div className="summary__card">
        <span className="summary__label">Realized PnL</span>
        <span className={`summary__value ${pnlColorClass(report.realized_pnl_usd)}`}>
          {formatUsd(report.realized_pnl_usd)}
        </span>
      </div>
      <div className="summary__card">
        <span className="summary__label">Unrealized PnL</span>
        <span className={`summary__value ${pnlColorClass(report.unrealized_pnl_usd)}`}>
          {formatUsd(report.unrealized_pnl_usd)}
        </span>
      </div>
      <div className="summary__card summary__card--accent">
        <span className="summary__label">Total PnL</span>
        <span className={`summary__value summary__value--big ${pnlColorClass(report.total_pnl_usd)}`}>
          {formatUsd(report.total_pnl_usd)}
        </span>
      </div>
    </section>
  );
}
