# BasisAnalysisByDeriveAPI

Tooling to pull and analyse a [Derive](https://www.derive.xyz/) (formerly Lyra)
subaccount: positions, collaterals, equity, margin headroom, net delta, and a
basis / carry view (perp funding vs the option-implied forward curve vs
collateral lending rates), plus what-if margin simulations.

## Derive v3 (the "geo-block" was the retired v2 API)

On **2026-10-06** Derive retired the v2 API (`https://api.lyra.finance`) and
Derive Chain, and moved to the **v3 API at `https://api.derive.xyz/v3`** with
settlement on Ethereum L1 (Derive Chain's last `Matching` trade was
2026-10-06 17:29 UTC; v3 funding history starts at 18:00). The old host now
answers every request with `HTTP 503` — that, not a US geo-block, is what the
earlier 503s were. v3 is reachable from the US. See Derive's
[v2 → v3 breaking changes](https://docs.derive.xyz/migrating/breaking-changes).

What changed for this tool: base URL, `X-Derive*` auth headers (were
`X-Lyra*`), the wallet is now the **owner EOA** (no separate "Derive Wallet"),
and `public/get_ticker` returns the slim ticker (`I`, `M`, `f` funding,
`option_pricing.f` forward).

## Credentials

Private REST endpoints are authenticated by EIP-191 signing the current
timestamp (ms) with a **session key** registered to the wallet — a read-only
key with the `account_info` scope is enough — or the owner key itself. The
script reads the key from the environment and **never prints it** — see
`--key-env` / `--scan-env` in `derive_portfolio.py`.

```bash
pip install -r requirements.txt

export DERIVE_API=0x...        # registered session key (read-only is fine)

python3 derive_portfolio.py \
  --subaccount 65812 \
  --staking-apr WSTETH=2.6,WEETH=2.8 \
  --what-if "ETH-PERP=-30" \
  --what-if "WEETH=-86.16,WSTETH=76.44" \
  --json-out portfolio_65812.json
```

`--wallet` is optional: the owner wallet is looked up from the session key
(`public/get_wallets_from_session_key`). Without `--subaccount` the first
subaccount the key may read is used.

## Output

- A readable summary (equity; maintenance / initial margin headroom and the
  projection after the next option expiry; net delta per currency and $ per
  1% move; LST/wrapped-collateral depeg sensitivity; per-perp basis and
  current / 7d / 30d funding APR; option-implied forward curve with the
  perp-minus-forward spread; options with position delta and theta; a carry
  table in USD/year).
- `--staking-apr` adds LST staking yield to the carry (not provided by the
  API). `--what-if` calls `private/get_margin` to show IM/MM before and after
  hypothetical position (`INSTRUMENT=amount`) and collateral (`ASSET=amount`)
  changes — read-only, nothing is traded.
- The full JSON (`analysis` + `raw_subaccount`) to stdout or `--json-out`.

## Auth scheme reference

Headers on each authenticated request:

| Header              | Value                                                      |
|---------------------|------------------------------------------------------------|
| `X-DeriveWallet`    | owner wallet address (EOA / multisig)                      |
| `X-DeriveTimestamp` | current UTC time in **milliseconds** (string)              |
| `X-DeriveSignature` | `personal_sign(timestamp)` by the session key (EIP-191)    |

If your account uses a different signing scheme, the script surfaces the raw
API error so it can be adjusted.
