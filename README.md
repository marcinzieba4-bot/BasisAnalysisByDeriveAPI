# BasisAnalysisByDeriveAPI

Tooling to pull and analyse a [Derive](https://www.derive.xyz/) (formerly Lyra)
subaccount: positions, collaterals, equity, margin, per-currency net delta, a
funding/basis view for perps, and P&L attribution for a synthetic-forward vs
perp basis trade.

## Derive v3 (since 2026-10-06)

Derive migrated to v3 on 2026-10-06 (snapshot 18:00 UTC). Derive Chain was
retired; matching stays off-chain and settlement/margin is proven on Ethereum
L1. What changed for this tooling:

| | v2 (retired) | v3 |
|---|---|---|
| REST base | `https://api.lyra.finance` (HTTP 503 from Cloudflare) | `https://api.derive.xyz/v3` |
| Auth headers | `X-LyraWallet`, `X-LyraTimestamp`, `X-LyraSignature` | `X-DeriveWallet`, `X-DeriveTimestamp`, `X-DeriveSignature` |
| Wallet header | LightAccount smart wallet | owner EOA (resolved from the session key) |
| Session keys | v2 keys | **not carried over** — register a new v3 key |
| Subaccount ids | | carried over from v2 |

On-chain history before the cutover is still readable on Derive Chain
(`https://rpc.derive.xyz`, explorer `https://explorer.derive.xyz`); nothing
settles there after 2026-10-06.

## Credentials

Private endpoints are authenticated by an EIP-191 signature of the request
timestamp (milliseconds), made by a **session key** registered for the owner
wallet (or the owner key itself). The scripts read the key from the
environment and **never print it**; they report only its public address.

```bash
pip install -r requirements.txt

python3 derive_portfolio.py --key-env DERIVE_4842_API --json-out portfolio.json
```

The owner wallet is looked up from the key (`public/get_wallets_from_session_key`)
unless `--wallet` is given. Without `--subaccount`, every subaccount is listed
and the largest one is analysed. Default key variable names are listed in
`derive_portfolio.py`; `--scan-env` finds a key-shaped variable by itself.

## Basis trade P&L (`basis_pnl.py`)

For a trade made of a synthetic long forward (buy call + sell put, same strike
and expiry) against a short perp:

```bash
python3 basis_pnl.py --key-env DERIVE_4842_API --subaccount 61222 \
    --since 2026-10-28T15:45:00Z
```

It reports:

- the value change since `--since`, split into fees, filled-vs-mark (spread
  paid on entry), mark drift on the new legs, and the rest of the book;
- the basis locked in (forward bought vs perp sold) against expected funding
  to expiry at the current and trailing-average funding rate, with the
  break-even funding APR;
- per-currency net delta for the old book and the new trade;
- what Derive's margin engine says if the options expire while the perps are
  still open — close or roll the perps at expiry.

## Output

- A readable summary on stdout (status lines on stderr).
- The full JSON to stdout or `--json-out`. `*.json` is git-ignored so account
  data stays out of the repo.
