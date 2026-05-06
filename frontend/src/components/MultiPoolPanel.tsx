import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import {
  fetchMultiPoolJob,
  startMultiPoolJob,
  type MultiPoolChildStatus,
  type MultiPoolJob,
} from "../api";
import { formatUsd, shortAddress } from "../format";

const POLL_MS = 1000;

const SAMPLE_POOLS = [
  "EQA5oOzbmWKbGReKJ6XqMmlnDfI4HDV0pgr1ZSJ-yhZz3RYc",
  "EQA-X_yo3fzzbDbJ_0bzFWKqtRuZFIRa1sJsveZJ1YpViO3r",
].join("\n");

interface Props {
  onCompleted?: () => void;
}

export function MultiPoolPanel({ onCompleted }: Props) {
  const [raw, setRaw] = useState("");
  const [rps, setRps] = useState("3");
  const [batchSize, setBatchSize] = useState("100");
  const [limit, setLimit] = useState("1000");
  const [maxConcurrency, setMaxConcurrency] = useState("10");
  const [job, setJob] = useState<MultiPoolJob | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const lastNotifiedJobIdRef = useRef<string | null>(null);

  const isActive =
    !!job && job.status !== "completed" && job.status !== "failed" && job.status !== "partial";

  // Poll for status while the job is running. We use a fresh setTimeout per
  // tick so the previous in-flight fetch can settle before we issue the next.
  useEffect(() => {
    if (!isActive || !job) return;
    const timer = window.setTimeout(async () => {
      try {
        const next = await fetchMultiPoolJob(job.job_id);
        setJob(next);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    }, POLL_MS);
    return () => window.clearTimeout(timer);
  }, [job, isActive]);

  // Notify the parent when a job transitions to a finished state so the DB
  // browser can refresh its rows.
  useEffect(() => {
    if (!job || isActive) return;
    if (lastNotifiedJobIdRef.current === job.job_id) return;
    lastNotifiedJobIdRef.current = job.job_id;
    onCompleted?.();
  }, [job, isActive, onCompleted]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!raw.trim()) return;
    setStarting(true);
    setError(null);
    setJob(null);
    try {
      const next = await startMultiPoolJob({
        raw,
        rps: positiveNumber(rps, 3),
        batchSize: positiveNumber(batchSize, 100),
        limit: positiveNumber(limit, 1000),
        maxConcurrency: positiveNumber(maxConcurrency, 10),
      });
      setJob(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setStarting(false);
    }
  }

  function fillSample() {
    setRaw(SAMPLE_POOLS);
  }

  const aggregate = useMemo(() => {
    if (!job) return null;
    let processedEvents = 0;
    let trades = 0;
    let traders = 0;
    let persisted = 0;
    let totalBuy = 0;
    let totalSell = 0;
    for (const c of job.children) {
      processedEvents += c.progress.processed_events ?? 0;
      trades += c.progress.trade_count ?? 0;
      if (c.report) {
        traders += c.report.trader_count;
        for (const r of c.report.rows) {
          totalBuy += r.buy_volume_usd ?? 0;
          totalSell += r.sell_volume_usd ?? 0;
        }
      }
      persisted += c.persisted_rows ?? 0;
    }
    return { processedEvents, trades, traders, persisted, totalBuy, totalSell };
  }, [job]);

  return (
    <>
      <form className="multi-form" onSubmit={onSubmit}>
        <textarea
          className="multi-form__textarea"
          placeholder={
            "One pool per line (EQ.../UQ... or 0:hex)\n" +
            "Optional: append |jetton-master to pin a token, e.g.\n" +
            "EQA5oOzbm...|EQD5u2gJ..."
          }
          rows={6}
          value={raw}
          onChange={(e) => setRaw(e.target.value)}
          spellCheck={false}
          autoCapitalize="off"
          autoComplete="off"
        />
        <div className="multi-form__controls">
          <label className="wallet-form__field">
            <span>RPS / pool</span>
            <input
              className="wallet-form__number"
              type="number"
              min="0.1"
              step="0.1"
              value={rps}
              onChange={(e) => setRps(e.target.value)}
              disabled={starting || isActive}
            />
          </label>
          <label className="wallet-form__field">
            <span>Batch</span>
            <input
              className="wallet-form__number"
              type="number"
              min="1"
              max="100"
              step="1"
              value={batchSize}
              onChange={(e) => setBatchSize(e.target.value)}
              disabled={starting || isActive}
            />
          </label>
          <label className="wallet-form__field">
            <span>Max events</span>
            <input
              className="wallet-form__number"
              type="number"
              min="1"
              step="100"
              value={limit}
              onChange={(e) => setLimit(e.target.value)}
              disabled={starting || isActive}
            />
          </label>
          <label className="wallet-form__field">
            <span>Concurrency</span>
            <input
              className="wallet-form__number"
              type="number"
              min="1"
              max="20"
              step="1"
              value={maxConcurrency}
              onChange={(e) => setMaxConcurrency(e.target.value)}
              disabled={starting || isActive}
            />
          </label>
          <div className="multi-form__buttons">
            <button
              type="submit"
              className="wallet-form__submit"
              disabled={starting || isActive || !raw.trim()}
            >
              {starting || isActive ? "Analyzing…" : "Analyze all"}
            </button>
            <button
              type="button"
              className="wallet-form__sample"
              onClick={fillSample}
              disabled={starting || isActive}
            >
              Try sample
            </button>
          </div>
        </div>
      </form>

      {error && (
        <div className="alert alert--error" role="alert">
          {error}
        </div>
      )}

      {job && (
        <div className="token-progress">
          <div className="token-progress__row">
            <span className={`badge badge--${jobStatusVariant(job.status)}`}>{job.status}</span>
            <span className="muted">
              {job.children.filter((c) => c.progress.status === "completed").length} /{" "}
              {job.children.length} pools complete · {job.total_persisted_rows} rows persisted
            </span>
          </div>
          {aggregate && (
            <div className="token-progress__stats">
              <span>events scanned: {aggregate.processedEvents.toLocaleString()}</span>
              <span>trades parsed: {aggregate.trades.toLocaleString()}</span>
              <span>traders: {aggregate.traders.toLocaleString()}</span>
              <span>buy {formatUsd(aggregate.totalBuy)}</span>
              <span>sell {formatUsd(aggregate.totalSell)}</span>
            </div>
          )}
        </div>
      )}

      {job && (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>Pool</th>
                <th>Token</th>
                <th>Status</th>
                <th className="num">Events</th>
                <th className="num">Trades</th>
                <th className="num">Traders</th>
                <th className="num">Persisted</th>
                <th>Message / error</th>
              </tr>
            </thead>
            <tbody>
              {job.children.map((c) => (
                <ChildRow key={c.pool} child={c} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function ChildRow({ child }: { child: MultiPoolChildStatus }) {
  const status = child.progress.status;
  const tokenLabel = child.report?.token
    ? `${child.report.token.symbol} · ${child.report.token.name}`
    : child.token_address
      ? shortAddress(child.token_address)
      : "—";
  return (
    <tr>
      <td className="mono" title={child.pool}>
        <a
          href={`https://tonviewer.com/${encodeURIComponent(child.pool)}`}
          target="_blank"
          rel="noopener noreferrer"
        >
          {shortAddress(child.pool)}
        </a>
      </td>
      <td>{tokenLabel}</td>
      <td>
        <span className={`badge badge--${status}`}>{status}</span>
      </td>
      <td className="num">{(child.progress.processed_events ?? 0).toLocaleString()}</td>
      <td className="num">{(child.progress.trade_count ?? 0).toLocaleString()}</td>
      <td className="num">{(child.progress.discovered_wallets ?? 0).toLocaleString()}</td>
      <td className="num">{child.persisted_rows.toLocaleString()}</td>
      <td className="muted" title={child.error ?? undefined}>
        {child.error ? truncate(child.error, 80) : (child.progress.message ?? "")}
      </td>
    </tr>
  );
}

function jobStatusVariant(status: MultiPoolJob["status"]): string {
  if (status === "running" || status === "queued") return "running";
  if (status === "completed") return "completed";
  if (status === "partial") return "warn";
  return "failed";
}

function positiveNumber(value: string, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1)}…`;
}
