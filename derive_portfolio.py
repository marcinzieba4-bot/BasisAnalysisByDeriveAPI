#!/usr/bin/env python3
"""Fetch and analyse a Derive subaccount / portfolio (Derive v3 API).

Why this exists
---------------
Derive migrated to v3 on 2026-10-06 (snapshot 18:00 UTC): Derive Chain was
retired, matching stayed off-chain and settlement moved to Ethereum L1. The
old v2 REST host (https://api.lyra.finance) is dead / geo-blocked; the v3
API lives at https://api.derive.xyz/v3. V2 balances, positions and
subaccount ids carried over, but v2 session keys did not, and the
X-DeriveWallet header now carries the owner EOA (no LightAccount wallet).

This script authenticates with a session key, pulls a subaccount and prints a
readable summary plus a machine-readable JSON blob (positions, collaterals,
equity, margin, per-currency net delta, and a funding/basis view for perps).

Credential handling
-------------------
Private endpoints are authenticated by an EIP-191 signature of the request
timestamp, made by the owner wallet or one of its registered session keys.
This script NEVER prints the key. It discovers the key from the environment:

  1. Explicit env var names (first match wins):
       DERIVE_API, DERIVE_PRIVATE_KEY, DERIVE_SESSION_KEY, DERIVE_API_KEY,
       LYRA_PRIVATE_KEY, SESSION_PRIVATE_KEY
  2. Or pass --key-env NAME to point at a specific variable.
  3. Or, with --scan-env, any env var whose value looks like a 32-byte
     hex private key (0x + 64 hex) is tried; the script reports only the
     derived *public address* of each candidate, never the key.

Usage
-----
  python3 derive_portfolio.py --key-env DERIVE_4842_API --json-out portfolio.json

The owner wallet is resolved from the session key
(public/get_wallets_from_session_key) unless --wallet is given. Without
--subaccount, every subaccount is listed and the largest one is analysed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from eth_account import Account
from eth_account.messages import encode_defunct

BASE_URL_DEFAULT = "https://api.derive.xyz/v3"
HEX_KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
CANDIDATE_KEY_VARS = [
    "DERIVE_API",
    "DERIVE_PRIVATE_KEY",
    "DERIVE_SESSION_KEY",
    "DERIVE_API_KEY",
    "LYRA_PRIVATE_KEY",
    "SESSION_PRIVATE_KEY",
]


# --------------------------------------------------------------------------- #
# Credential discovery
# --------------------------------------------------------------------------- #
def _normalise_key(val: str) -> Optional[str]:
    val = val.strip()
    if not HEX_KEY_RE.match(val):
        return None
    return val if val.startswith("0x") else "0x" + val


def discover_key(key_env: Optional[str], scan_env: bool) -> tuple[str, str, str]:
    """Return (private_key, source_var_name, signer_address). Key is not logged."""
    tried: list[tuple[str, str]] = []

    names = [key_env] if key_env else list(CANDIDATE_KEY_VARS)
    for name in names:
        if not name:
            continue
        raw = os.environ.get(name)
        if raw is None:
            tried.append((name, "unset"))
            continue
        norm = _normalise_key(raw)
        if norm is None:
            tried.append((name, "present but not a 0x+64hex key"))
            continue
        addr = Account.from_key(norm).address
        return norm, name, addr

    if scan_env:
        for name, raw in sorted(os.environ.items()):
            if name in (key_env, *CANDIDATE_KEY_VARS):
                continue
            norm = _normalise_key(raw or "")
            if norm is None:
                continue
            addr = Account.from_key(norm).address
            tried.append((name, f"candidate -> signer {addr}"))
        hits = [t for t in tried if "signer" in t[1]]
        if len(hits) == 1:
            name = hits[0][0]
            norm = _normalise_key(os.environ[name])
            return norm, name, Account.from_key(norm).address  # type: ignore[arg-type]
        if len(hits) > 1:
            msg = "Multiple private-key-shaped env vars found; pass --key-env NAME:\n"
            msg += "\n".join(f"  {n}: {s}" for n, s in hits)
            raise SystemExit(msg)

    detail = "\n".join(f"  {n}: {s}" for n, s in tried) or "  (none checked)"
    raise SystemExit(
        "No Derive session private key found in the environment.\n"
        f"Checked:\n{detail}\n"
        "Set one of "
        f"{CANDIDATE_KEY_VARS}, or pass --key-env NAME, or --scan-env."
    )


# --------------------------------------------------------------------------- #
# Signed REST client
# --------------------------------------------------------------------------- #
class DeriveClient:
    def __init__(self, private_key: str, wallet: Optional[str], base_url: str):
        self.base_url = base_url.rstrip("/")
        self.acct = Account.from_key(private_key)
        self.signer = self.acct.address
        self.session = requests.Session()
        self.session.headers.update({"accept": "application/json", "content-type": "application/json"})
        # Owner wallet used in the X-DeriveWallet header. In v3 this is the
        # owner EOA; resolve it from the session key unless given explicitly.
        self.wallet = wallet or self._wallet_from_session_key()

    def _wallet_from_session_key(self) -> str:
        res = self.post("public/get_wallets_from_session_key",
                        {"public_session_key": self.signer.lower()}, auth=False)
        wallets = res.get("wallets") or []
        if len(wallets) != 1:
            raise SystemExit(
                f"Session key {self.signer} is registered for wallets {wallets}; pass --wallet."
            )
        return wallets[0]

    def _auth_headers(self) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        sig = self.acct.sign_message(encode_defunct(text=ts)).signature.hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig
        return {"X-DeriveWallet": self.wallet, "X-DeriveTimestamp": ts, "X-DeriveSignature": sig}

    def post(self, path: str, body: dict[str, Any], auth: bool = True) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        for attempt in range(4):
            headers = self._auth_headers() if auth else {}
            r = self.session.post(url, data=json.dumps(body), headers=headers, timeout=60)
            if r.status_code == 429 or (r.status_code >= 500 and "cloudflare" not in r.text.lower()):
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        text = r.text
        if r.status_code == 503 and "cloudflare" in text.lower():
            raise SystemExit(
                f"HTTP 503 from Cloudflare on {path}. This egress is geo/WAF blocked "
                "by Derive — run this from an unblocked region."
            )
        try:
            data = r.json()
        except ValueError:
            raise SystemExit(f"{path}: HTTP {r.status_code}, non-JSON body: {text[:300]}")
        if isinstance(data, dict) and data.get("error"):
            raise SystemExit(f"{path}: API error {json.dumps(data['error'])[:400]}")
        if r.status_code >= 400:
            raise SystemExit(f"{path}: HTTP {r.status_code}: {json.dumps(data)[:400]}")
        return data.get("result", data) if isinstance(data, dict) else data

    def paged(self, path: str, body: dict[str, Any], key: str, page_size: int = 1000) -> list[Any]:
        """Collect every page of a paginated history endpoint."""
        rows: list[Any] = []
        page = 1
        while True:
            res = self.post(path, {**body, "page": page, "page_size": page_size})
            batch = res.get(key, [])
            rows.extend(batch)
            num_pages = (res.get("pagination") or {}).get("num_pages") or 1
            if page >= num_pages or not batch:
                return rows
            page += 1


# --------------------------------------------------------------------------- #
# Analysis helpers
# --------------------------------------------------------------------------- #
def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def annualised_funding(hourly_rate: float) -> float:
    """Derive perp funding is quoted per hour; APR ~ rate * 24 * 365."""
    return hourly_rate * 24 * 365


def analyse(subaccount: dict[str, Any], tickers: dict[str, Any]) -> dict[str, Any]:
    positions = subaccount.get("positions", []) or []
    collaterals = subaccount.get("collaterals", []) or []

    equity = _f(subaccount.get("subaccount_value"))
    net_delta: dict[str, float] = {}
    gross_notional = 0.0
    perp_rows, option_rows, collateral_rows = [], [], []

    for c in collaterals:
        amount = _f(c.get("amount"))
        ccy = c.get("delta_currency") or c.get("currency")
        # v3 reports collateral delta per unit in its delta currency (wstETH ~1.25 ETH).
        if ccy and ccy != "USDC":
            net_delta[ccy] = net_delta.get(ccy, 0.0) + amount * _f(c.get("delta"), 1.0)
        collateral_rows.append(
            {
                "asset": c.get("asset_name"),
                "amount": amount,
                "usd_value": _f(c.get("mark_value")),
            }
        )

    for p in positions:
        name = p.get("instrument_name", "")
        ccy = name.split("-")[0]
        amount = _f(p.get("amount"))
        mark = _f(p.get("mark_price"))
        idx = _f(p.get("index_price"))
        delta = amount * _f(p.get("delta"), 1.0)  # v3 position delta is per unit
        upnl = _f(p.get("unrealized_pnl"))
        notional = abs(amount) * (mark if p.get("instrument_type") == "perp" else idx)
        gross_notional += notional
        net_delta[ccy] = net_delta.get(ccy, 0.0) + delta

        tk = tickers.get(name, {})
        if p.get("instrument_type") == "perp" or name.endswith("-PERP"):
            hourly = _f(tk.get("f"))
            basis = ((mark - idx) / idx) if idx else 0.0
            perp_rows.append(
                {
                    "instrument": name,
                    "amount": amount,
                    "mark": mark,
                    "index": idx,
                    "basis_pct": round(basis * 100, 4),
                    "funding_hourly_pct": round(hourly * 100, 6),
                    "funding_apr_pct": round(annualised_funding(hourly) * 100, 3),
                    "notional_usd": round(notional, 2),
                    "unrealized_pnl": round(upnl, 2),
                    "cumulative_funding": round(_f(p.get("cumulative_funding")), 2),
                }
            )
        else:
            option_rows.append(
                {
                    "instrument": name,
                    "amount": amount,
                    "mark": mark,
                    "delta": round(delta, 4),
                    "forward": _f((tk.get("option_pricing") or {}).get("f")) or None,
                    "notional_usd": round(notional, 2),
                    "unrealized_pnl": round(upnl, 2),
                }
            )

    return {
        "equity_usd": round(equity, 2),
        "free_initial_margin_usd": round(_f(subaccount.get("initial_margin")), 2),
        "free_maintenance_margin_usd": round(_f(subaccount.get("maintenance_margin")), 2),
        "net_delta": {k: round(v, 4) for k, v in net_delta.items()},
        "gross_notional_usd": round(gross_notional, 2),
        "num_positions": len(positions),
        "perps": perp_rows,
        "options": option_rows,
        "collaterals": collateral_rows,
    }


def print_summary(wallet: str, sub_id: int, a: dict[str, Any]) -> None:
    line = "=" * 68
    print(line)
    print(f"Derive portfolio  |  wallet {wallet}  |  subaccount {sub_id}")
    print(f"Snapshot: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(line)
    print(f"Equity (subaccount value): ${a['equity_usd']:,.2f}")
    print(f"Free IM: ${a['free_initial_margin_usd']:,.2f}   Free MM (buffer to liquidation): "
          f"${a['free_maintenance_margin_usd']:,.2f}")
    deltas = "  ".join(f"{k} {v:+.4f}" for k, v in a["net_delta"].items())
    print(f"Net delta: {deltas}   Gross notional: ${a['gross_notional_usd']:,.2f}")
    print(f"Positions: {a['num_positions']}")
    if a["collaterals"]:
        print("\nCollaterals:")
        for c in a["collaterals"]:
            print(f"  {c['asset']:>8}  amount={c['amount']:<16}  ${c['usd_value']:,.2f}")
    if a["perps"]:
        print("\nPerps (basis / funding):")
        for r in a["perps"]:
            print(
                f"  {r['instrument']:<16} amt={r['amount']:<12} "
                f"basis={r['basis_pct']:>7}%  fundingAPR={r['funding_apr_pct']:>8}%  "
                f"notional=${r['notional_usd']:,.0f}  uPnL=${r['unrealized_pnl']:,.2f}"
            )
    if a["options"]:
        print("\nOptions:")
        for r in a["options"]:
            print(
                f"  {r['instrument']:<24} amt={r['amount']:<10} delta={r['delta']:<10} "
                f"uPnL=${r['unrealized_pnl']:,.2f}"
            )
    print(line)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch & analyse a Derive subaccount.")
    ap.add_argument("--wallet", help="Owner wallet address (X-DeriveWallet header).")
    ap.add_argument("--subaccount", type=int, help="Subaccount id, e.g. 61222.")
    ap.add_argument("--key-env", help="Name of env var holding the session private key.")
    ap.add_argument("--scan-env", action="store_true", help="Scan env for a key-shaped value.")
    ap.add_argument("--base-url", default=os.environ.get("DERIVE_BASE_URL", BASE_URL_DEFAULT))
    ap.add_argument("--json-out", help="Write the full JSON result to this file.")
    ap.add_argument("--no-tickers", action="store_true", help="Skip per-instrument ticker fetch.")
    args = ap.parse_args(argv)

    pk, src_var, signer = discover_key(args.key_env, args.scan_env)
    print(f"[auth] using session key from env var '{src_var}'  (signer address {signer})", file=sys.stderr)

    # Sanity: public clock (also proves the region is not blocked).
    try:
        requests.post(f"{args.base_url.rstrip('/')}/public/get_time", json={}, timeout=30).raise_for_status()
    except requests.RequestException as e:
        print(f"public/get_time failed: {e}", file=sys.stderr)
        return 2

    client = DeriveClient(pk, args.wallet, args.base_url)
    print(f"[auth] wallet {client.wallet}", file=sys.stderr)

    # Resolve subaccount: default to the largest one by value.
    sub_id = args.subaccount
    subaccount = None
    if sub_id is None:
        subs = client.post("private/get_subaccounts", {"wallet": client.wallet.lower()})
        ids = subs.get("subaccount_ids") or []
        if not ids:
            print(f"[info] no subaccounts for {client.wallet}", file=sys.stderr)
            return 3
        snaps = {i: client.post("private/get_subaccount", {"subaccount_id": i}) for i in ids}
        for i, s in snaps.items():
            print(f"[info] subaccount {i:<8} value=${_f(s.get('subaccount_value')):>12,.2f}  "
                  f"label={s.get('label')!r}", file=sys.stderr)
        sub_id = max(snaps, key=lambda i: _f(snaps[i].get("subaccount_value")))
        subaccount = snaps[sub_id]
    if subaccount is None:
        subaccount = client.post("private/get_subaccount", {"subaccount_id": sub_id})

    tickers: dict[str, Any] = {}
    if not args.no_tickers:
        for p in subaccount.get("positions", []) or []:
            name = p.get("instrument_name")
            if name and name not in tickers:
                try:
                    tickers[name] = client.post("public/get_ticker", {"instrument_name": name}, auth=False)
                except SystemExit:
                    pass

    analysis = analyse(subaccount, tickers)
    print_summary(client.wallet, sub_id, analysis)

    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "wallet": client.wallet,
        "signer": signer,
        "subaccount_id": sub_id,
        "analysis": analysis,
        "raw_subaccount": subaccount,
    }
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(out, fh, indent=2, default=str)
        print(f"[info] wrote {args.json_out}", file=sys.stderr)
    else:
        print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
