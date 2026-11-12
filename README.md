# BasisAnalysisByDeriveAPI

Tooling to pull and analyse a [Derive](https://www.derive.xyz/) (formerly Lyra)
subaccount: positions, collaterals, equity, net delta, and a funding/basis view
for perps.

## Derive v3 and debt-shift planner (`debt_shift.py`)

Derive moved to v3 on 2026-10-06: REST base `https://api.derive.xyz/v3`,
`X-DeriveWallet` / `X-DeriveTimestamp` / `X-DeriveSignature` headers, owner
EOA as the wallet. v3 is reachable from the US; the 503s below came from the
retired v2 host. (`derive_portfolio.py` on this branch is still the v2
version; v3 ports live on the other `claude/*` branches.)

USDC lending pools are per risk universe, so borrowing in ALT (XRP, SOL, …)
can be far cheaper than in PRIME (BTC/ETH). `debt_shift.py` plans the loop
"move collateral from the PRIME subaccount to an ALT subaccount, borrow USDC
there, send it back to repay": it asks `private/get_margin` for the most you
can move in one step, and prints the interest saved and the destination's
liquidation distance. Read-only — nothing is signed or traded.

```bash
python3 debt_shift.py --from 65812 --to 70848 --asset WSTETH            # max at the IM limit
python3 debt_shift.py --from 65812 --to 70848 --ltv 0.5 --buffer 5000   # safer target
```

## The geo-block (v2 only), and why you ran this from the EU

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
