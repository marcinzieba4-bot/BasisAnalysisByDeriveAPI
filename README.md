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

Projects the net P&L of a funding-carry book over a horizon (default 60 days):
long spot/LST collateral and synthetic long forwards (long call + short put)
against a short perp that collects funding, financed with borrowed USDC.

```bash
python3 basis_projection.py --wallet 0xYourOwnerWallet --subaccount 12345 \
  --key-env DERIVE_PRIVATE_KEY --days 60 --exec mid --json-out projection.json
```

It reports, per funding scenario (30d / 7d realised average, current rate,
flat stress rates):

| Component  | Source                                                                 |
|------------|------------------------------------------------------------------------|
| funding    | perp size x spot x funding APR, hourly                                  |
| staking    | Lido stETH APR on wstETH collateral                                     |
| interest   | USDC pool borrow/supply APY on the path-dependent cash balance          |
| options    | Black-76 repricing at fixed IV: forward roll-down + theta of all legs   |
| fees+spr   | exchange fees + execution vs mark when expiring legs are rolled         |

It compares two ways of handling legs that expire inside the horizon: **roll**
(re-open the expiring synthetic delta at the first expiry after the horizon)
or **close** (buy back the same amount of perp). It also prints a spot-shock grid
and asks Derive's read-only margin simulator (`private/get_margin`) what each
choice does to maintenance margin right after the next expiry. A negative
`maintenance_margin + projected_margin_change` means the subaccount becomes
liquidatable when that expiry settles.

`--exec mark|mid|touch` sets the roll execution price. Derive's option marks
can sit off the order-book mid, so `touch` (crossing the book) can cost far
more than `mark` suggests.

The script never places, cancels or changes anything.

## Auth scheme reference

Headers on each authenticated request (v3):

| Header              | Value                                                        |
|---------------------|--------------------------------------------------------------|
| `X-DeriveWallet`    | owner wallet address                                         |
| `X-DeriveTimestamp` | current UTC time in **milliseconds** (string)                |
| `X-DeriveSignature` | `personal_sign(timestamp)` by the session key (EIP-191)      |

If your account uses a different signing scheme, the scripts surface the raw
API error so it can be adjusted.
