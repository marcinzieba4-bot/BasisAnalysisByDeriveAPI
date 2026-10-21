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
