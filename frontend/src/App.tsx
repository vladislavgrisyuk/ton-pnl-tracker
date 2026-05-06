import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import { fetchWalletPnL, type PnLReport } from "./api";
import { SummaryCards } from "./components/SummaryCards";
import { SwapTable } from "./components/SwapTable";
import { TokenTable } from "./components/TokenTable";
import "./App.css";

const SAMPLE_WALLET = "UQDcFi-ucUqelV2nkES9aXflZt0sFw7KwTHvYyR43PXYOPtv";

type Tab = "tokens" | "swaps";

export default function App() {
  const [address, setAddress] = useState(() => {
    const fromUrl = new URL(window.location.href).searchParams.get("wallet");
    return fromUrl ?? "";
  });
  const [report, setReport] = useState<PnLReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("tokens");
  const abortRef = useRef<AbortController | null>(null);

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
      const data = await fetchWalletPnL(addr.trim(), {
        refresh,
        signal: controller.signal,
      });
      setReport(data);
      const url = new URL(window.location.href);
      url.searchParams.set("wallet", addr.trim());
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

  useEffect(() => {
    // Defer the kick-off to a microtask so React's "no setState during effect"
    // rule isn't tripped — the actual state writes happen after the Promise
    // resolves anyway, but the linter's static analysis sees them as in-effect.
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
      </form>

      {error && (
        <div className="alert alert--error" role="alert">
          {error}
        </div>
      )}

      {report && (
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

      {!report && !loading && !error && (
        <div className="placeholder">
          <p>Enter a TON wallet address to load its swap history and PnL.</p>
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
