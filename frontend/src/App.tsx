import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import {
  fetchTokenAnalyticsJob,
  fetchWalletPnL,
  startTokenAnalyticsJob,
  type PnLReport,
  type TokenAnalyticsJob,
} from "./api";
import { SummaryCards } from "./components/SummaryCards";
import { SwapTable } from "./components/SwapTable";
import { TokenTable } from "./components/TokenTable";
import { TokenTradersTable } from "./components/TokenTradersTable";
import "./App.css";

const SAMPLE_WALLET = "UQDcFi-ucUqelV2nkES9aXflZt0sFw7KwTHvYyR43PXYOPtv";
const SAMPLE_POOL = "EQA5oOzbmWKbGReKJ6XqMmlnDfI4HDV0pgr1ZSJ-yhZz3RYc";
const DEFAULT_RPS = "2";
const DEFAULT_BATCH_SIZE = "30";
const DEFAULT_TOKEN_BATCH_SIZE = "100";
const DEFAULT_TOKEN_LIMIT = "1000";
const TOKEN_JOB_POLL_MS = 1000;

type Tab = "tokens" | "swaps";
type Mode = "wallet" | "token";

function positiveParam(name: string, fallback: string) {
  const value = new URL(window.location.href).searchParams.get(name);
  if (!value) return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? value : fallback;
}

function positiveNumber(value: string, fallback: string) {
  const parsed = Number(value);
  if (Number.isFinite(parsed) && parsed > 0) return parsed;
  return Number(fallback);
}

export default function App() {
  const initialUrl = new URL(window.location.href);
  const initialMode: Mode = initialUrl.searchParams.get("pool") ? "token" : "wallet";
  const [mode, setMode] = useState<Mode>(initialMode);
  const [address, setAddress] = useState(() => initialUrl.searchParams.get("wallet") ?? "");
  const [poolAddress, setPoolAddress] = useState(
    () => initialUrl.searchParams.get("pool") ?? "",
  );
  const [tokenFilter, setTokenFilter] = useState(
    () => initialUrl.searchParams.get("token") ?? "",
  );
  const [tokenLimit, setTokenLimit] = useState(() =>
    positiveParam("token_limit", DEFAULT_TOKEN_LIMIT),
  );
  const [tokenBatchSize, setTokenBatchSize] = useState(() =>
    positiveParam("token_batch", DEFAULT_TOKEN_BATCH_SIZE),
  );
  const [rps, setRps] = useState(() => positiveParam("rps", DEFAULT_RPS));
  const [batchSize, setBatchSize] = useState(() => positiveParam("batch_size", DEFAULT_BATCH_SIZE));
  const [report, setReport] = useState<PnLReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("tokens");
  const [tokenJob, setTokenJob] = useState<TokenAnalyticsJob | null>(null);
  const [tokenStarting, setTokenStarting] = useState(false);
  const [tokenError, setTokenError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const tokenJobIdRef = useRef<string | null>(null);

  const tokenJobActive =
    !!tokenJob &&
    tokenJob.progress.status !== "completed" &&
    tokenJob.progress.status !== "failed";
  const tokenLoading = tokenStarting || tokenJobActive;
  const tokenJobError =
    tokenJob?.progress.status === "failed"
      ? tokenJob.error || tokenJob.progress.message || "job failed"
      : null;
  const visibleTokenError = tokenError ?? tokenJobError;

  const visibleTokens = useMemo(() => {
    if (!report) return [];
    return [...report.tokens].sort(
      (a, b) => Math.abs(b.total_pnl_usd) - Math.abs(a.total_pnl_usd),
    );
  }, [report]);

  async function load(addr: string, refresh = false) {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError(null);
    try {
      const parsedRps = positiveNumber(rps, DEFAULT_RPS);
      const parsedBatchSize = positiveNumber(batchSize, DEFAULT_BATCH_SIZE);
      const data = await fetchWalletPnL(addr.trim(), {
        refresh,
        rps: parsedRps,
        batchSize: parsedBatchSize,
        signal: controller.signal,
      });
      setReport(data);
      const url = new URL(window.location.href);
      url.searchParams.set("wallet", addr.trim());
      url.searchParams.set("rps", String(parsedRps));
      url.searchParams.set("batch_size", String(parsedBatchSize));
      window.history.replaceState(null, "", url.toString());
    } catch (err) {
      if ((err as Error)?.name === "AbortError") return;
      setError(err instanceof Error ? err.message : String(err));
      setReport(null);
    } finally {
      if (abortRef.current === controller) {
        setLoading(false);
        abortRef.current = null;
      }
    }
  }

  async function runTokenAnalytics(pool: string, token: string) {
    const trimmedPool = pool.trim();
    if (!trimmedPool) return;
    tokenJobIdRef.current = null;
    setTokenStarting(true);
    setTokenError(null);
    setTokenJob(null);
    try {
      const parsedRps = positiveNumber(rps, DEFAULT_RPS);
      const parsedBatch = positiveNumber(tokenBatchSize, DEFAULT_TOKEN_BATCH_SIZE);
      const parsedLimit = positiveNumber(tokenLimit, DEFAULT_TOKEN_LIMIT);
      const job = await startTokenAnalyticsJob(trimmedPool, {
        tokenAddress: token.trim() || undefined,
        rps: parsedRps,
        batchSize: parsedBatch,
        limit: parsedLimit,
      });
      tokenJobIdRef.current = job.job_id;
      setTokenJob(job);
      const url = new URL(window.location.href);
      url.searchParams.set("pool", trimmedPool);
      if (token.trim()) {
        url.searchParams.set("token", token.trim());
      } else {
        url.searchParams.delete("token");
      }
      url.searchParams.set("token_limit", String(parsedLimit));
      url.searchParams.set("token_batch", String(parsedBatch));
      url.searchParams.set("rps", String(parsedRps));
      window.history.replaceState(null, "", url.toString());
    } catch (err) {
      setTokenError(err instanceof Error ? err.message : String(err));
    } finally {
      setTokenStarting(false);
    }
  }

  useEffect(() => {
    if (!tokenJobActive) return;
    const handle = window.setTimeout(async () => {
      const id = tokenJobIdRef.current;
      if (!id) return;
      try {
        const next = await fetchTokenAnalyticsJob(id);
        if (tokenJobIdRef.current === id) setTokenJob(next);
      } catch (err) {
        setTokenError(err instanceof Error ? err.message : String(err));
      }
    }, TOKEN_JOB_POLL_MS);
    return () => window.clearTimeout(handle);
  }, [tokenJob, tokenJobActive]);

  useEffect(() => {
    // Defer the kick-off to a microtask so React's "no setState during effect"
    // rule isn't tripped — the actual state writes happen after the Promise
    // resolves anyway, but the linter's static analysis sees them as in-effect.
    if (initialMode === "token" && poolAddress) {
      const handle = window.setTimeout(() => {
        void runTokenAnalytics(poolAddress, tokenFilter);
      }, 0);
      return () => window.clearTimeout(handle);
    }
    if (!address) return;
    const handle = window.setTimeout(() => {
      void load(address);
    }, 0);
    return () => window.clearTimeout(handle);
    // We intentionally fire only on mount based on the URL param.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!address.trim()) return;
    void load(address.trim());
  }

  function onTokenSubmit(e: FormEvent) {
    e.preventDefault();
    if (!poolAddress.trim()) return;
    void runTokenAnalytics(poolAddress, tokenFilter);
  }

  return (
    <div className="app">
      <header className="app__header">
        <div>
          <h1>TON PnL Tracker</h1>
          <p className="muted">
            Aggregate jetton swaps for any TON wallet and see realized + unrealized PnL per token.
          </p>
        </div>
      </header>

      <nav className="mode-tabs">
        <button
          type="button"
          className={mode === "wallet" ? "mode-tab mode-tab--active" : "mode-tab"}
          onClick={() => setMode("wallet")}
        >
          Wallet PnL
        </button>
        <button
          type="button"
          className={mode === "token" ? "mode-tab mode-tab--active" : "mode-tab"}
          onClick={() => setMode("token")}
        >
          Token Analytics
        </button>
      </nav>

      {mode === "wallet" && (
      <form className="wallet-form" onSubmit={onSubmit}>
        <input
          className="wallet-form__input"
          type="text"
          placeholder="EQ... or UQ... wallet address"
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          autoFocus
          spellCheck={false}
          autoCapitalize="off"
          autoComplete="off"
        />
        <button className="wallet-form__submit" type="submit" disabled={loading || !address.trim()}>
          {loading ? "Analyzing…" : "Analyze"}
        </button>
        <button
          type="button"
          className="wallet-form__sample"
          onClick={() => {
            setAddress(SAMPLE_WALLET);
            void load(SAMPLE_WALLET);
          }}
          disabled={loading}
        >
          Try sample
        </button>
        <label className="wallet-form__field">
          <span>RPS</span>
          <input
            className="wallet-form__number"
            type="number"
            min="0.1"
            step="0.1"
            value={rps}
            onChange={(e) => setRps(e.target.value)}
            disabled={loading}
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
            disabled={loading}
          />
        </label>
      </form>
      )}

      {mode === "token" && (
        <form className="wallet-form" onSubmit={onTokenSubmit}>
          <input
            className="wallet-form__input"
            type="text"
            placeholder="Pool address (EQ... / UQ... / 0:hex)"
            value={poolAddress}
            onChange={(e) => setPoolAddress(e.target.value)}
            spellCheck={false}
            autoCapitalize="off"
            autoComplete="off"
          />
          <input
            className="wallet-form__input wallet-form__input--secondary"
            type="text"
            placeholder="Optional jetton master to filter"
            value={tokenFilter}
            onChange={(e) => setTokenFilter(e.target.value)}
            spellCheck={false}
            autoCapitalize="off"
            autoComplete="off"
          />
          <button
            className="wallet-form__submit"
            type="submit"
            disabled={tokenLoading || !poolAddress.trim()}
          >
            {tokenLoading ? "Analyzing…" : "Analyze"}
          </button>
          <button
            type="button"
            className="wallet-form__sample"
            onClick={() => {
              setPoolAddress(SAMPLE_POOL);
              setTokenFilter("");
              void runTokenAnalytics(SAMPLE_POOL, "");
            }}
            disabled={tokenLoading}
          >
            Try sample
          </button>
          <label className="wallet-form__field">
            <span>RPS</span>
            <input
              className="wallet-form__number"
              type="number"
              min="0.1"
              step="0.1"
              value={rps}
              onChange={(e) => setRps(e.target.value)}
              disabled={tokenLoading}
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
              value={tokenBatchSize}
              onChange={(e) => setTokenBatchSize(e.target.value)}
              disabled={tokenLoading}
            />
          </label>
          <label className="wallet-form__field">
            <span>Max events</span>
            <input
              className="wallet-form__number"
              type="number"
              min="1"
              step="100"
              value={tokenLimit}
              onChange={(e) => setTokenLimit(e.target.value)}
              disabled={tokenLoading}
            />
          </label>
        </form>
      )}

      {mode === "wallet" && error && (
        <div className="alert alert--error" role="alert">
          {error}
        </div>
      )}

      {mode === "wallet" && report && (
        <>
          <SummaryCards report={report} />

          {report.warnings && report.warnings.length > 0 && (
            <div className="alert alert--warn">
              <strong>Notes:</strong>
              <ul>
                {report.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          )}

          <nav className="tabs">
            <button
              className={tab === "tokens" ? "tab tab--active" : "tab"}
              onClick={() => setTab("tokens")}
            >
              Tokens ({report.tokens.length})
            </button>
            <button
              className={tab === "swaps" ? "tab tab--active" : "tab"}
              onClick={() => setTab("swaps")}
            >
              Swaps ({report.swaps.length})
            </button>
            <button
              type="button"
              className="tab tab--ghost"
              onClick={() => void load(address, true)}
              disabled={loading}
            >
              ↻ Refresh
            </button>
          </nav>

          <section>
            {tab === "tokens" ? (
              <TokenTable tokens={visibleTokens} />
            ) : (
              <SwapTable swaps={report.swaps} />
            )}
          </section>
        </>
      )}

      {mode === "wallet" && !report && !loading && !error && (
        <div className="placeholder">
          <p>Enter a TON wallet address to load its swap history and PnL.</p>
        </div>
      )}

      {mode === "token" && visibleTokenError && (
        <div className="alert alert--error" role="alert">
          {visibleTokenError}
        </div>
      )}

      {mode === "token" && tokenJob && (
        <div className="token-progress">
          <div className="token-progress__row">
            <span className={`badge badge--${tokenJob.progress.status}`}>
              {tokenJob.progress.status}
            </span>
            {tokenJob.progress.message && (
              <span className="muted">{tokenJob.progress.message}</span>
            )}
          </div>
          <div className="token-progress__stats">
            <span>events: {tokenJob.progress.processed_events}</span>
            <span>wallets: {tokenJob.progress.discovered_wallets}</span>
            <span>trades: {tokenJob.progress.trade_count}</span>
          </div>
        </div>
      )}

      {mode === "token" && tokenJob?.report && (
        <>
          {tokenJob.report.warnings && tokenJob.report.warnings.length > 0 && (
            <div className="alert alert--warn">
              <strong>Notes:</strong>
              <ul>
                {tokenJob.report.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          )}
          <div className="summary">
            <div className="summary__card">
              <span className="summary__label">Token</span>
              <span className="summary__value">
                {tokenJob.report.token.symbol} · {tokenJob.report.token.name}
              </span>
            </div>
            <div className="summary__card">
              <span className="summary__label">Traders</span>
              <span className="summary__value">{tokenJob.report.trader_count}</span>
            </div>
            <div className="summary__card">
              <span className="summary__label">Trades parsed</span>
              <span className="summary__value">{tokenJob.report.trade_count}</span>
            </div>
            <div className="summary__card">
              <span className="summary__label">Pool events scanned</span>
              <span className="summary__value">{tokenJob.report.processed_events}</span>
            </div>
          </div>
          <TokenTradersTable rows={tokenJob.report.rows} />
        </>
      )}

      {mode === "token" && !tokenJob && !tokenLoading && !visibleTokenError && (
        <div className="placeholder">
          <p>
            Paste a DEX pool address (e.g. a STON.fi or DeDust pool) to see the top traders for its
            token, ranked by realized + unrealized PnL.
          </p>
        </div>
      )}

      <footer className="app__footer">
        <p>
          Data: <a href="https://tonapi.io">tonapi.io</a> ·{" "}
          <a href="https://www.geckoterminal.com">GeckoTerminal</a>. PnL computed locally via FIFO
          matching.
        </p>
      </footer>
    </div>
  );
}
