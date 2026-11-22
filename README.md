# BasisAnalysisByDeriveAPI

Tooling to pull and analyse a [Derive](https://www.derive.xyz/) (formerly Lyra)
subaccount: positions, collaterals, equity, net delta, a funding/basis view for
perps, and a forward projection of what a funding-carry ("basis") book will earn.

## API host

Use Derive's v3 API at **`https://api.derive.xyz/v3`** (the default). It is not
geo-blocked. The legacy host `https://api.lyra.finance` now returns Cloudflare
`521`/`503` from every region tried, so there is no longer any need to run this
from an EU box.

## Credentials

Derive private REST endpoints are authenticated by signing the request
timestamp with a **session key**: an Ethereum private key that the owner wallet
has registered on Derive (or the owner EOA key itself). The scripts read this
key from the environment and **never print it**; see `--key-env` / `--scan-env`
in `derive_portfolio.py`.

To check which wallet a key is registered to (public, no auth):
`public/get_wallets_from_session_key` with `{"public_session_key": "<signer address>"}`.
An expired or unregistered key returns `14026 Session key not found`.

```bash
pip install -r requirements.txt

export DERIVE_PRIVATE_KEY=0x...        # registered session key (or owner EOA key)

python3 derive_portfolio.py \
  --wallet 0xYourOwnerWallet \
  --subaccount 12345 \
  --json-out portfolio.json
```

If the key lives in a differently-named env var, point at it with
`--key-env NAME`, or let the script find it with `--scan-env` (it reports only
the derived public address of each key-shaped value, never the key).

## Portfolio snapshot (`derive_portfolio.py`)

- A readable summary (equity, net delta per currency **including collateral**,
  per-perp basis & annualised funding, options, collaterals).
- The full JSON (`analysis` + `raw_subaccount`) to stdout or `--json-out`.

## Basis / carry projection (`basis_projection.py`)

Projects the net P&L of funding-carry books over a horizon (default 60 days):
long spot/LST collateral and synthetic long forwards (long call + short put)
against short perps that collect funding, financed with borrowed USDC.

Positions come from **public** endpoints (`public/margin_watch`), so no session
key is needed and any subaccount, or every funded subaccount of a wallet, can
be projected and totalled:

```bash
# every funded subaccount of a wallet
python3 basis_projection.py --wallet 0xYourOwnerWallet --days 60

# specific subaccounts (may span wallets)
python3 basis_projection.py --subaccount 111 222 333 --exec mid --json-out projection.json
```

It reports, per subaccount and in total, per funding scenario (30d / 7d
realised average, current rate, flat stress rates):

| Component  | Source                                                                 |
|------------|------------------------------------------------------------------------|
| funding    | perp size x spot x funding APR, hourly                                  |
| staking    | Lido stETH APR on wstETH; `--asset-apr JITOSOL=0.07` for others         |
| interest   | USDC pool borrow/supply APY on the path-dependent cash balance          |
| options    | Black-76 repricing at fixed IV: forward roll-down + theta of all legs   |
| fees+spr   | exchange fees + execution vs mark when expiring legs are rolled         |

Legs that expire inside the horizon are handled two ways: **roll** (re-open the
expiring synthetic delta at the first expiry after the horizon) or **close**
(buy back the same amount of perp). `--roll-size neutral` (default) re-hedges
only what is needed to end delta-neutral; `full` replaces the whole expiring
delta and keeps today's residual net delta.

Around the next expiry it rebuilds the portfolio explicitly and asks
`public/get_margin` for MM/IM after expiry with no action, after a roll, after
closing perp, and for a single **calendar roll package** before expiry (close
the expiring legs and open the new expiry in one trade). Negative MM means the
subaccount becomes liquidatable when that expiry settles.

`--exec mark|mid|touch` sets the roll execution price. Derive's option marks
can sit off the order-book mid, so `touch` (crossing the book) can cost far
more than `mark` suggests.

> **Simulator caveat.** `private/get_margin` with `simulated_position_changes`
> on a perp books the size change at the *existing* position's entry price,
> which credits or debits a phantom `(mark - entry) x change`. Don't trust it
> for perp changes. The explicit-portfolio `public/get_margin` used here
> reproduces live MM/IM to within about $60.

The script never places, cancels or changes anything.

## Liquidation price (`liquidation_check.py`)

Derive's margin simulator only prices a portfolio at today's spot. This script
rebuilds the documented portfolio-margin (PM2) maintenance margin (23-scenario
grid, tail ladder, skew rails, forward loss, contingencies, collateral
haircuts). On the books tested it matches Derive's live MM to within about $75.
It then moves spot and solves for MM = 0: ETH and BTC together, and each alone.
What-ifs are applied first and also checked with the exact engine at today's prices.

```bash
python3 liquidation_check.py --subaccount 12345
python3 liquidation_check.py --subaccount 12345 --trade ETH-20270129-1400-P=-40
python3 liquidation_check.py --subaccount 12345 --settle-expiry 20261127 \
    --trade ETH-20270129-1600-C=165.65 ETH-20270129-1600-P=-165.65
```

Moves are instant, forwards move 1:1 with spot, and implied vols stay sticky by strike.

## Auth scheme reference

Headers on each authenticated request (v3):

| Header              | Value                                                        |
|---------------------|--------------------------------------------------------------|
| `X-DeriveWallet`    | owner wallet address                                         |
| `X-DeriveTimestamp` | current UTC time in **milliseconds** (string)                |
| `X-DeriveSignature` | `personal_sign(timestamp)` by the session key (EIP-191)      |

If your account uses a different signing scheme, the scripts surface the raw
API error so it can be adjusted.
