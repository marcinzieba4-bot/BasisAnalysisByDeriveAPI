#!/usr/bin/env python3
"""Fetch and analyse a Derive (formerly Lyra) subaccount / portfolio.

Why this exists
---------------
Derive's REST API (https://api.lyra.finance) geo/WAF-blocks some regions
(a US egress currently gets HTTP 503 from Cloudflare). Run this from a
region that is not blocked (e.g. an EU EC2 box) and it will authenticate,
pull the full portfolio for a subaccount, and print a readable summary plus
a machine-readable JSON blob (positions, balances, equity, net delta, and a
funding/basis view for perps).

Credential handling
-------------------
Derive private REST endpoints are authenticated by signing the request
timestamp with a *session key* (an Ethereum private key that the owner
wallet has registered, or the owner EOA key itself). This script NEVER
prints the key. It discovers the key from the environment:

  1. Explicit env var names (first match wins):
       DERIVE_PRIVATE_KEY, DERIVE_SESSION_KEY, DERIVE_API_KEY,
       LYRA_PRIVATE_KEY, SESSION_PRIVATE_KEY
  2. Or pass --key-env NAME to point at a specific variable.
  3. Or, with --scan-env, any env var whose value looks like a 32-byte
     hex private key (0x + 64 hex) is tried; the script reports only the
     derived *public address* of each candidate, never the key.

Usage
-----
  export DERIVE_PRIVATE_KEY=0x...            # the registered session key
  python3 derive_portfolio.py \
      --wallet 0xfDB01a2b6c447F79c0DE5279bc42C1C4963867Bf \
      --subaccount 65812 \
      --json-out portfolio_65812.json

If the owner wallet address is omitted, the script derives candidate
wallets from the key / asks the API for subaccounts of the signer.
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

BASE_URL_DEFAULT = "https://api.lyra.finance"
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
        # Owner wallet used in the X-LyraWallet header. Defaults to signer
        # (works when the signer *is* the owner EOA); override with --wallet
        # when a separate session key signs on behalf of the owner.
        self.wallet = wallet or self.signer
        self.session = requests.Session()
        self.session.headers.update({"accept": "application/json", "content-type": "application/json"})

    def _auth_headers(self) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        sig = self.acct.sign_message(encode_defunct(text=ts)).signature.hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig
        return {"X-LyraWallet": self.wallet, "X-LyraTimestamp": ts, "X-LyraSignature": sig}

    def post(self, path: str, body: dict[str, Any], auth: bool = True) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = self._auth_headers() if auth else {}
        r = self.session.post(url, data=json.dumps(body), headers=headers, timeout=30)
        text = r.text
        if r.status_code == 503 and "cloudflare" in text.lower():
            raise SystemExit(
                f"HTTP 503 from Cloudflare on {path}. This egress is still geo/WAF "
                "blocked by Derive — run this from an unblocked region (EU EC2)."
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
    net_delta = 0.0
    gross_notional = 0.0
    perp_rows, option_rows, collateral_rows = [], [], []

    for c in collaterals:
        collateral_rows.append(
            {
                "asset": c.get("asset_name"),
                "amount": _f(c.get("amount")),
                "usd_value": _f(c.get("mark_value")),
            }
        )

    for p in positions:
        name = p.get("instrument_name", "")
        amount = _f(p.get("amount"))
        mark = _f(p.get("mark_price"))
        idx = _f(p.get("index_price"))
        delta = _f(p.get("delta"))
        upnl = _f(p.get("unrealized_pnl"))
        notional = abs(amount) * (mark or idx)
        gross_notional += notional
        net_delta += delta if delta else amount  # options carry delta; perps delta≈amount

        tk = tickers.get(name, {})
        if name.endswith("-PERP") or p.get("instrument_type") == "perp":
            hourly = _f(tk.get("funding_rate") or (tk.get("perp_details") or {}).get("funding_rate"))
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
                }
            )
        else:
            option_rows.append(
                {
                    "instrument": name,
                    "amount": amount,
                    "mark": mark,
                    "delta": delta,
                    "notional_usd": round(notional, 2),
                    "unrealized_pnl": round(upnl, 2),
                }
            )

    return {
        "equity_usd": round(equity, 2),
        "net_delta": round(net_delta, 4),
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
    print(f"Net delta: {a['net_delta']}   Gross notional: ${a['gross_notional_usd']:,.2f}")
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
    ap.add_argument("--wallet", help="Owner wallet address (X-LyraWallet header).")
    ap.add_argument("--subaccount", type=int, help="Subaccount id, e.g. 65812.")
    ap.add_argument("--key-env", help="Name of env var holding the session private key.")
    ap.add_argument("--scan-env", action="store_true", help="Scan env for a key-shaped value.")
    ap.add_argument("--base-url", default=os.environ.get("DERIVE_BASE_URL", BASE_URL_DEFAULT))
    ap.add_argument("--json-out", help="Write the full JSON result to this file.")
    ap.add_argument("--no-tickers", action="store_true", help="Skip per-instrument ticker fetch.")
    args = ap.parse_args(argv)

    pk, src_var, signer = discover_key(args.key_env, args.scan_env)
    print(f"[auth] using session key from env var '{src_var}'  (signer address {signer})", file=sys.stderr)

    client = DeriveClient(pk, args.wallet, args.base_url)

    # Sanity: public clock (also proves the region is not blocked).
    try:
        client.post("public/get_time", {}, auth=False)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2

    # Resolve subaccount(s).
    sub_id = args.subaccount
    if sub_id is None:
        subs = client.post("private/get_subaccounts", {"wallet": client.wallet})
        ids = subs.get("subaccount_ids") or subs.get("subaccounts") or []
        print(f"[info] subaccounts for {client.wallet}: {ids}", file=sys.stderr)
        if not ids:
            return 3
        sub_id = ids[0] if not isinstance(ids[0], dict) else ids[0].get("subaccount_id")

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
