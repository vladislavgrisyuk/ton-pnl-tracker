# TON PnL Tracker Agent Handoff

This document is for the next coding agent working on this repository. It explains what was investigated, what was fixed, why the current implementation behaves the way it does, and what pitfalls to avoid.

## Project context

The project is a TON wallet PnL tracker.

- **Repo path:** `f:/Projects/Ton-tracker/ton-pnl-tracker`
- **Backend:** FastAPI, Python, under `backend/`
- **Frontend:** Vite + React + TypeScript, under `frontend/`
- **Backend dev venv:** `backend/.venv`
- **Typical backend URL:** `http://127.0.0.1:8000`
- **Typical frontend URL:** `http://127.0.0.1:5173`

The main endpoint is:

```http
GET /api/wallet/{address}/pnl
```

It fetches account events and balances, extracts swaps, annotates USD values, and computes FIFO realized/unrealized PnL.

## User objective

The user is trying to make PnL correct for wallets that trade TON jettons. The main issue observed was:

> The UI shows no buys or no sells for many tokens even though TonAPI / tonviewer raw event history clearly contains trades.

The user specifically noticed this with a wallet where many tokens were traded, but PnL rows showed zero buy/sell values or wrong realized/unrealized PnL.

A frequently used sample wallet during debugging:

```text
Friendly: EQBd4IX7CAWL0341QUtkcLI-q_CGoWxQK4VpSB7EMDTwzwKW
Raw:      0:5de085fb08058bd37e35414b6470b23eabf086a16c502b8569481ec43034f0cf
```

## External data source

The backend can use either public TonAPI or tonviewer.com proxy.

Relevant settings live in:

```text
backend/ton_pnl/settings.py
```

Important settings:

```python
tonapi_base_url: str = "https://tonapi.io"
tonapi_token: str | None = None
tonapi_headers: dict[str, str]
tonapi_cookies: dict[str, str]
tonviewer_passphrase: str = "tv22-asrr11"
max_events_per_wallet: int = 1000
tonapi_rps: float = 2.0
tonapi_event_batch_size: int = 30
```

The user said they are effectively using the `tonviewer.com` proxy. In that mode, responses can be CryptoJS-style AES-wrapped. The client detects tonviewer mode by checking whether the configured base URL contains `tonviewer.com` and decrypts via:

```text
backend/ton_pnl/tonviewer_crypt.py
```

The decrypt passphrase currently defaults to:

```text
tv22-asrr11
```

If tonviewer changes their frontend encryption/passphrase, this can break.

## RPS and batching behavior

The user requested frontend control over request rate and event batch size.

Frontend now has controls:

- **RPS**
- **Batch**

These are sent as query parameters:

```http
/api/wallet/{address}/pnl?rps=30&batch_size=30
```

Backend query params in `backend/ton_pnl/main.py`:

```python
async def wallet_pnl(
    address: str,
    refresh: bool = False,
    limit: int | None = None,
    rps: float | None = None,
    batch_size: int | None = None,
) -> PnLReport:
```

Validation:

- `rps` must be `> 0`
- `batch_size` must be between `1` and `100`

### Important clarification from the user

The user did **not** want a sequential delay like “one request every 33ms for RPS=30”. They wanted:

> No more than 30 requests per second, but requests should be sent together while the RPS window allows it.

So `TonApiClient` now uses a burst/window limiter, not simple spacing.

Current semantics:

- If `rps=30`, up to 30 upstream requests may start immediately within a rolling 1-second window.
- The 31st request waits until the window frees capacity.
- Independent requests are allowed to run concurrently.

Implemented in:

```text
backend/ton_pnl/tonapi.py
```

Main pieces:

```python
self._request_times: deque[float] = deque()
self._rate_lock = asyncio.Lock()
```

and `_throttle()` removes timestamps older than the active window, then either allows a new request or sleeps until capacity is available.

### What is actually parallelized

In `wallet_pnl`, these TonAPI requests are started concurrently under the same `TonApiClient` limiter:

```python
account, events, jetton_balances = await asyncio.gather(
    ton.get_account(normalized_address),
    ton.iter_events(normalized_address, limit=effective_batch_size, max_events=limit),
    ton.get_jetton_balances(normalized_address),
)
```

### Why event pages are still sequential

TonAPI event pagination uses `before_lt` / `next_from`. Page N+1 depends on `next_from` returned by page N:

```text
page 1 -> next_from -> page 2 -> next_from -> page 3
```

Because of that, history pages cannot safely be fetched all at once unless another API shape exists. `batch_size=30` means each `/events` page asks for 30 events.

## Address normalization pitfalls

TON addresses can appear as:

- raw: `0:<64 hex chars>`
- friendly: `EQ...` or `UQ...`

The code must normalize addresses consistently, especially jetton master addresses and wallet addresses.

Address utilities:

```text
backend/ton_pnl/address.py
```

Important helper:

```python
friendly_to_raw(address: str) -> str
```

Backend endpoint now tries to normalize the wallet address early:

```python
normalized_address = _to_raw_address(address)
```

Jetton addresses are normalized inside swap parsing too.

## pTON / Proxy TON bug and fix

A previous bug was that STON.fi pTON wrapper addresses were hardcoded incorrectly and not normalized. This caused pTON to appear as a separate token instead of TON, producing wrong PnL.

Relevant file:

```text
backend/ton_pnl/swaps.py
```

Current proxy TON addresses:

```python
PROXY_TON_ADDRESSES = {
    # STON.fi v1 pTON (EQCM3B12QK1e4yZSf8GtBRT0aLMNyEsBc_DhVfRRtOEffLez)
    "0:8cdc1d7640ad5ee326527fc1ad0514f468b30dc84b0173f0e155f451b4e11f7c",
    # STON.fi v2 pTON (EQBnFjAn9_hWWatVuCFnFohgHNzx7mdPx_u7Gndaiyaco6IO)
    "0:67163027f7f85659ab55b821671688601cdcf1ee674fc7fbbb1a775a8b269ca3",
}
```

`_jetton_token()` normalizes the address and collapses proxy TON to native TON sentinel:

```python
_TON_TOKEN = TokenInfo(asset_id=TON_ASSET_ID, symbol="TON", ...)
```

Do not remove this behavior.

## TonAPI event formats observed

The biggest discovery: **not all real trades arrive as `JettonSwap` actions**.

Originally, the backend only parsed `JettonSwap`, which missed many buys/sells.

### Format 1: explicit `JettonSwap`

This is the easiest case. TonAPI action shape is roughly:

```json
{
  "type": "JettonSwap",
  "status": "ok",
  "JettonSwap": {
    "dex": "stonfi",
    "amount_in": "...",
    "amount_out": "...",
    "ton_in": 123000000,
    "ton_out": null,
    "user_wallet": { "address": "0:..." },
    "router": { "address": "0:..." },
    "jetton_master_in": { "address": "0:...", "symbol": "...", "decimals": 9 },
    "jetton_master_out": { "address": "0:...", "symbol": "...", "decimals": 9 }
  }
}
```

Handled by:

```python
normalize_swap(event, action)
```

Filtering rule:

If `wallet_address` is supplied, `extract_swaps()` only keeps `JettonSwap` actions where `JettonSwap.user_wallet.address` matches the target wallet.

### Format 2: `FlawedJettonTransfer` buy

Many buy events from DeDust / DTrade-like flows arrived as:

- `SmartContractExec` from the wallet to a contract with `ton_attached`
- `FlawedJettonTransfer` where recipient is the user wallet
- sometimes a separate `TonTransfer` fee with comment `DTrade fee / DeDust`

Observed example shape from the user/sample wallet:

```text
EVENT 1777981604 9937ae34fc410105804598ac4ddfbb9d811be3ac3869c3e156cba1fa6ed2f2b7
  SmartContractExec ok
   exec amount 5200000000 executor 0:5de...f0cf contract 0:dae...8d20
  FlawedJettonTransfer ok
   flawed FM sent_amount 48672323457883 received_amount 38062985940916
   sender 0:37ac...2d35 recipient 0:5de...f0cf
  TonTransfer ok
   ton amount 50000000 sender 0:5de...f0cf recipient 0:93c...650c
   comment DTrade fee / DeDust
```

Interpretation:

- `SmartContractExec.ton_attached = 5.2 TON` is the purchase cost basis.
- `FlawedJettonTransfer.received_amount` is the token amount received.
- The separate `TonTransfer` with comment `DTrade fee / DeDust` is a fee and should **not** be used as swap amount.

Handled by:

```python
normalize_flawed_transfer_buy(event, action, wallet_address)
```

It returns a pseudo-swap:

```text
TON -> token
```

### Format 3: plain `JettonTransfer` buy

Some buys arrived as a normal `JettonTransfer`, not `FlawedJettonTransfer`:

```text
EVENT 1778015983 f0d7a4a3f02a3ed8217342a2a1d31927193c21b14b32daf114ff5b0a018deef4
 exec ton_attached 25200000000 executor 0:5de...f0cf contract 0:fd4...5768
 jetton PYONYA amount 2328338475963555
   sender 0:fd4...5768 recipient 0:5de...f0cf
 ton 147127263 sender 0:fd4...5768 recipient 0:5de...f0cf
 ton 250000000 sender 0:5de...f0cf recipient 0:93c...650c comment DTrade fee / DeDust
```

Interpretation:

- Incoming jetton transfer to wallet + `SmartContractExec.ton_attached` from wallet = purchase.
- Cost basis is `ton_attached`, not the fee transfer.

Handled by:

```python
normalize_transfer_swap(event, action, wallet_address)
```

For incoming jetton:

```text
TON -> token
```

### Format 4: plain `JettonTransfer` sell

Some sells arrived as outgoing `JettonTransfer` plus incoming `TonTransfer` from the same contract:

```text
EVENT 1778016236 196248c370d364b8791babc9b9ee4b474cd100a08bd096bcdba0b7ccb6f81fda
 jetton PYONYA amount 1164169237981777
   sender 0:5de...f0cf recipient 0:fd4...5768
 ton 18348651156 sender 0:fd4...5768 recipient 0:5de...f0cf
 ton 197256330 sender 0:fd4...5768 recipient 0:5de...f0cf
 ton 247027027 sender 0:5de...f0cf recipient 0:93c...650c comment DTrade fee / DeDust
```

Interpretation:

- Outgoing jetton transfer from wallet to contract = selling token.
- Incoming TON transfers from that same contract to the wallet are proceeds.
- Current implementation sums matching `TonTransfer` actions where:
  - `TonTransfer.sender == JettonTransfer.recipient`
  - `TonTransfer.recipient == wallet`

This returns pseudo-swap:

```text
token -> TON
```

## Duplicate prevention

When an event already contains explicit `JettonSwap`, the fallback `JettonTransfer` parser is skipped for that event:

```python
has_explicit_swap = any(action.get("type") == "JettonSwap" for action in event.get("actions") or [])
```

Then plain `JettonTransfer` fallback only runs if:

```python
action_type == "JettonTransfer" and target and not has_explicit_swap
```

This is important to avoid double-counting swaps in traces that contain both summary and transfer-level actions.

`FlawedJettonTransfer` fallback currently can run even if no explicit swap exists. Be careful if future TonAPI events include both explicit swap and flawed transfer for the same action; add duplicate protection if observed.

## PnL computation overview

Core PnL logic:

```text
backend/ton_pnl/pnl.py
```

It uses FIFO matching of buys and sells.

A `Swap` has:

- `asset_in`
- `asset_out`
- `amount_in`
- `amount_out`
- optional `ton_in`
- optional `ton_out`
- optional `usd_value`

If swaps are missing, FIFO cost basis becomes wrong. Missing buys often make sold tokens look like zero-cost positions or cause rows with no meaningful PnL.

The parser fixes above feed more complete pseudo-swaps into the existing FIFO engine.

## Pricing notes

Pricing logic:

```text
backend/ton_pnl/pricing.py
```

The backend annotates swaps with USD values using TON legs or GeckoTerminal data.

Current prices are fetched only for held-or-traded tokens:

```python
held_or_traded = list(
    {a for s in swaps for a in (s.asset_in.asset_id, s.asset_out.asset_id)}
    | set(balances.keys())
)
```

If GeckoTerminal fails, the report returns warnings and uses empty current prices.

## Frontend notes

Relevant frontend files:

```text
frontend/src/App.tsx
frontend/src/api.ts
frontend/src/App.css
```

`api.ts` serializes query params:

- `refresh`
- `limit`
- `rps`
- `batch_size`

`App.tsx` has state for:

```typescript
const [rps, setRps] = useState(() => positiveParam("rps", DEFAULT_RPS));
const [batchSize, setBatchSize] = useState(() => positiveParam("batch_size", DEFAULT_BATCH_SIZE));
```

Defaults:

```typescript
const DEFAULT_RPS = "2";
const DEFAULT_BATCH_SIZE = "30";
```

The UI also writes these values back into the URL so the current tuning can be shared/reloaded.

## Tests added/updated

Main test file:

```text
backend/tests/test_pnl.py
```

Important added tests:

- pTON collapse to TON
- `FlawedJettonTransfer` buy extraction
- plain `JettonTransfer` buy/sell extraction

Commands used for validation:

```powershell
# backend
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check ton_pnl tests
.\.venv\Scripts\python.exe -m ruff format ton_pnl tests

# frontend
npm run build
npm run lint
```

Known successful validation at the end of the latest session:

- Backend tests: `13 passed`
- Ruff check: passed
- Frontend build: passed
- Frontend lint: passed

## Useful diagnostic scripts

During debugging, short one-off scripts were run from `backend/` using the venv:

```powershell
@'
import asyncio
from collections import defaultdict
from ton_pnl.tonapi import TonApiClient
from ton_pnl.swaps import extract_swaps

ADDR = '0:5de085fb08058bd37e35414b6470b23eabf086a16c502b8569481ec43034f0cf'

async def main():
    async with TonApiClient() as ton:
        events = await ton.iter_events(ADDR, max_events=1000)
    swaps = extract_swaps(events, wallet_address=ADDR)
    print('events', len(events), 'swaps', len(swaps))
    stats = defaultdict(lambda: [0.0, 0.0, 0])
    for s in swaps:
        stats[s.asset_out.symbol][0] += s.amount_out
        stats[s.asset_out.symbol][2] += 1
        stats[s.asset_in.symbol][1] += s.amount_in
        stats[s.asset_in.symbol][2] += 1
    for sym, (b, sold, cnt) in sorted(stats.items(), key=lambda x: x[0]):
        print(f'{sym:14} bought={b:.8f} sold={sold:.8f} count={cnt}')

asyncio.run(main())
'@ | .\.venv\Scripts\python.exe -
```

After parsing fixes, this sample wallet showed roughly:

```text
events 41 swaps 39
1TON           bought=105224.91919072 sold=105224.91919072 count=2
BCHERRY        bought=280554.92861044 sold=241239.76739878 count=8
DURIKOVICH     bought=19836.41234381 sold=19757.06669444 count=3
FM             bought=38062.98594092 sold=37910.73399715 count=2
Gram           bought=149648.60737813 sold=149050.01294862 count=6
KOT            bought=208847.91287326 sold=208847.91287326 count=2
LOGO           bought=31552.81020976 sold=31426.59896892 count=2
PYONYA         bought=2328338.47596355 sold=2328338.47596356 count=4
REDO           bought=586.21207815 sold=462.71298448 count=8
TON            bought=226.53122596 sold=176.50000000 count=39
TonTon         bought=256888.51222264 sold=715233.06248454 count=2
```

API check example:

```powershell
$addr='EQBd4IX7CAWL0341QUtkcLI-q_CGoWxQK4VpSB7EMDTwzwKW'
$r=Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/wallet/$addr/pnl?refresh=true&rps=30&batch_size=30"
"swaps=$($r.swap_count) total=$($r.total_pnl_usd) realized=$($r.realized_pnl_usd) unrealized=$($r.unrealized_pnl_usd)"
```

## Current sample API outcome

For the sample wallet after parser fixes, API returned non-zero PnL for tokens that previously looked empty. One observed run:

```text
swaps=39
total≈196.99
realized≈176.39
unrealized≈20.60
```

Notable token rows included:

```text
PYONYA       realized +124.31
BCHERRY      total    +63.47
TON          total    +30.62
DURIKOVICH   realized +12.85
KOT          realized -6.96
1TON         realized -6.74
TonTon       realized -6.54
LOGO/FM/etc  non-zero parsed buys/sells
```

Exact numbers can shift due to price source and current balances.

## Potential pitfalls and next work

### 1. False positives in transfer fallback

Plain `JettonTransfer` can represent:

- real trade
- wallet transfer
- airdrop
- contract migration
- claim
- LP activity

The fallback intentionally requires TON legs in the same event:

- buy: incoming jetton + `SmartContractExec.ton_attached` from wallet
- sell: outgoing jetton + incoming `TonTransfer` from same contract

Still, false positives are possible. If a new wallet looks wrong, inspect raw event action types before changing FIFO.

### 2. Fees are not fully modeled

`DTrade fee / DeDust` transfers are currently not incorporated as extra cost/proceeds adjustment. The parser avoids treating them as swap amount.

This means PnL may differ from a fee-inclusive accounting system. If the user wants exact net PnL including platform fees, implement explicit fee accounting.

### 3. TonAPI / tonviewer schemas are unstable

Action shapes can change or vary by DEX/router. Avoid strict parsing. Existing parsing uses defensive `.get()` patterns and `_to_int()`.

### 4. Event page concurrency is limited by pagination

Do not try to parallelize pages using guessed `before_lt`. Current sequential pagination is correct because `next_from` comes from the previous page.

### 5. Cache key includes RPS and batch size

The cache key currently includes:

```python
cache_key = f"{address}:{limit or 'default'}:{effective_rps}:{effective_batch_size}"
```

Strictly speaking, RPS does not change data and does not need to be part of the cache key. Batch size should also not change final data if pagination works correctly. It was included so user tuning changes trigger fresh fetches unless `refresh=false`. A future agent may choose to simplify this.

### 6. Existing unrelated modified files

At one point `git status` showed these modified files:

```text
backend/tests/test_pnl.py
backend/ton_pnl/main.py
backend/ton_pnl/swaps.py
backend/ton_pnl/tonapi.py
backend/ton_pnl/tonviewer_crypt.py
frontend/package-lock.json
frontend/src/App.css
frontend/src/App.tsx
frontend/src/api.ts
```

`tonviewer_crypt.py` may have been touched only by `ruff format`. `frontend/package-lock.json` may have changed from earlier dependency install/update. Review diffs before committing if you want a clean changeset.

## Recommended workflow for next agent

1. Read this file.
2. Run:

```powershell
git status --short
```

3. Inspect diffs for files you plan to touch.
4. If investigating PnL, dump raw TonAPI event actions before changing parser logic.
5. Add targeted unit tests for every new event shape.
6. Run backend tests and ruff.
7. Run frontend build/lint if UI/API params changed.

## Mental model

The most important mental model is:

> TonAPI's high-level `JettonSwap` action is convenient but incomplete. Some real trades are only visible as transfer-level traces. The backend normalizes these different trace formats into one internal `Swap` model so the FIFO PnL engine can stay simple.

Keep `Swap` normalization tolerant, address-normalized, and duplicate-safe.
