# BasisAnalysisByDeriveAPI

Tooling to pull and analyse a [Derive](https://www.derive.xyz/) (formerly Lyra)
subaccount: positions, collaterals, equity, net delta, and a funding/basis view
for perps.

## The geo-block, and why you run this from the EU

Derive's REST API (`https://api.lyra.finance`) is behind Cloudflare and
**geo/WAF-blocks some egress regions**. A US-based host currently gets
`HTTP 503` from Cloudflare for every endpoint, public ones included. Run this
from a region that is *not* blocked — e.g. one of the EU EC2 instances
(Stockholm / Paris) — and it works.

## Credentials

Derive private REST endpoints are authenticated by signing the request
timestamp with a **session key**: an Ethereum private key that the owner wallet
has registered on Derive (or the owner EOA key itself). The script reads this
key from the environment and **never prints it** — see `--key-env` / `--scan-env`
in `derive_portfolio.py`.

```bash
pip install -r requirements.txt

export DERIVE_PRIVATE_KEY=0x...        # registered session key (or owner EOA key)

python3 derive_portfolio.py \
  --wallet 0xfDB01a2b6c447F79c0DE5279bc42C1C4963867Bf \
  --subaccount 65812 \
  --json-out portfolio_65812.json
```

If the key lives in a differently-named env var, point at it with
`--key-env NAME`, or let the script find it with `--scan-env` (it reports only
the derived public address of each key-shaped value, never the key).

## Output

- A readable summary to stderr/stdout (equity, net delta, per-perp basis &
  annualised funding, options, collaterals).
- The full JSON (`analysis` + `raw_subaccount`) to stdout or `--json-out`.

## Auth scheme reference

Headers on each authenticated request:

| Header            | Value                                                        |
|-------------------|--------------------------------------------------------------|
| `X-LyraWallet`    | owner wallet address                                         |
| `X-LyraTimestamp` | current UTC time in **milliseconds** (string)                |
| `X-LyraSignature` | `personal_sign(timestamp)` by the session key (EIP-191)      |

If your account uses a different signing scheme, the script surfaces the raw
API error so it can be adjusted.

## BTC vs ETH for the monthly roll (`roll_compare.py`)

For the levered basis trade, a synthetic long forward (buy call + sell put,
same strike, monthly expiry) against a short perp, this compares the
underlyings on what decides the trade. It uses public data only (no key) and
the Derive **v3** API (`https://api.derive.xyz/v3`):

- the implied financing of the next monthly expiries (option forward vs index),
  at mark and at the executable top of book for the synthetic, plus fees;
- perp funding now / 7d / 30d, and how often it sits at the 10.95% APR anchor,
  below it, or negative. Derive funding is a premium term plus a static
  0.00125%/h. A balanced perp pays shorts exactly 10.95% APR, while one at a
  discount pays less or turns negative;
- PM2 margin for the hedged book (public `get_margin` simulation): maximum
  leverage at initial margin and leverage at liquidation;
- the carry and return on equity at the leverage you pass;
- a GO / NO-GO roll gate per currency, from 7-day funding and the share of
  hours at the anchor. Defaults are BTC ≥ 8% and 70%, ETH ≥ 9.5% and 80%;
  override them with `--gate BTC=8:70 ETH=9.5:80`. It also says whether ETH's
  net carry beats BTC's by at least 1 point. Net carry here uses screen-taker
  roll costs, so it is conservative next to RFQ execution.

`--history` replays every monthly roll since launch from Derive Chain archive
state: the option forward feed, the perp's `aggregatedFunding`, the spot and
perp feeds, and settlement prices. The chain stopped settling at the v3 cutover,
but `https://rpc.derive.xyz` still serves historical state. After the cutover,
funding comes from the v3 funding history. `--path` adds a sample every day
inside each window to replay mark-to-market and maintenance margin at each
leverage. The public RPC is rate-limited to about 6 calls/s, so `--history`
takes a couple of minutes and `--path` about 25 minutes the first time. Raw
samples are cached in `roll_history_cache.json`.

`--breakdown` itemises what the long forward costs, per $ notional:

- **financing by strike**: the forward premium at mark and executable, the
  premium prepaid for an ITM call, the USDC borrowed to pay it at each
  leverage, and that interest (borrow APY plus the supply APY the equity no
  longer earns), giving a total. Option marks discount prepaid premium at
  ~3.5–4%, below Derive's USDC borrow rate, so deep-ITM strikes do not lower
  financing;
- **roll cost by method**:
  - 2-way: close the old synthetic and open the new one, keeping the perp;
  - 3-way: close and reopen the call, put and perp;
  - expire + perp bridge: the old synthetic settles while an extra short perp
    hedges it.

  Each is costed as screen taker and as maker. The close is costed on a 1–3
  day expiry at the same moneyness, where strikes below ~0.9× index have no
  two-sided book;
- **margin of each path** (PM2): the bridge needs roughly twice the normal
  initial margin for the hours around expiry. A perp left naked after
  settlement needs ~15% (BTC) / ~17% (ETH);
- **public RFQ fills** of 2-leg synthetics over the last `--rfq-days`.

```bash
python3 roll_compare.py                                   # live snapshot
python3 roll_compare.py --breakdown --leverage 20 25      # financing + roll costs
python3 roll_compare.py --history --path --leverage 20 25 30
```
