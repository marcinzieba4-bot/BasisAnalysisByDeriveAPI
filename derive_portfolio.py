#!/usr/bin/env python3
"""Fetch and analyse a Derive subaccount / portfolio (Derive v3 API).

Why this exists
---------------
Pulls the full portfolio for a Derive subaccount and prints a readable
summary plus a machine-readable JSON blob: positions, collaterals, equity,
margin headroom, net delta per currency, and a basis / carry view (perp
funding vs the option-implied forward curve vs collateral lending rates).

Derive v2 -> v3
---------------
On 2026-10-06 Derive retired the v2 API (https://api.lyra.finance, which now
answers every request with HTTP 503) together with Derive Chain, and moved to
the v3 API at https://api.derive.xyz/v3 with settlement on Ethereum L1. The
503s that looked like a US geo-block were the retired host; v3 is reachable
from the US. What changed for this script:

  * base URL            -> https://api.derive.xyz/v3
  * auth headers        -> X-DeriveWallet / X-DeriveTimestamp / X-DeriveSignature
  * wallet              -> the owner EOA (no separate "Derive Wallet" any more)
  * public/get_ticker   -> slim ticker: I (index), M (mark), f (hourly funding),
                           option_pricing.{f (forward), df, d, t, ...}

Credential handling
-------------------
Private REST endpoints are authenticated by EIP-191 signing the current
timestamp (ms) with a *session key* registered to the wallet (a read-only
"account_info" key is enough), or the owner key itself. This script NEVER
prints the key. It discovers the key from the environment:

  1. Explicit env var names (first match wins):
       DERIVE_API, DERIVE_PRIVATE_KEY, DERIVE_SESSION_KEY, DERIVE_API_KEY,
       LYRA_PRIVATE_KEY, SESSION_PRIVATE_KEY
  2. Or pass --key-env NAME to point at a specific variable.
  3. Or, with --scan-env, any env var whose value looks like a 32-byte
     hex private key (0x + 64 hex) is tried; the script reports only the
     derived *public address* of each candidate, never the key.

Usage
-----
  export DERIVE_API=0x...                    # the registered session key
  python3 derive_portfolio.py --subaccount 65812 --json-out portfolio_65812.json

If --wallet is omitted the owner wallet is looked up from the session key
(public/get_wallets_from_session_key); if --subaccount is omitted the first
subaccount the key may read is used.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
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
CASH_CURRENCIES = {"USDC", "USDT", "DAI"}
HOURS_PER_YEAR = 24 * 365


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
        # Owner wallet used in the X-DeriveWallet header. In v3 this is the
        # owner EOA / multisig; resolve_wallet() looks it up when not given.
        self.wallet = wallet
        self.session = requests.Session()
        self.session.headers.update({"accept": "application/json", "content-type": "application/json"})

    def _auth_headers(self) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        sig = self.acct.sign_message(encode_defunct(text=ts)).signature.hex()
        if not sig.startswith("0x"):
            sig = "0x" + sig
        return {"X-DeriveWallet": self.wallet or self.signer, "X-DeriveTimestamp": ts, "X-DeriveSignature": sig}

    def post(self, path: str, body: dict[str, Any], auth: bool = True) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = self._auth_headers() if auth else {}
        r = self.session.post(url, data=json.dumps(body), headers=headers, timeout=30)
        text = r.text
        try:
            data = r.json()
        except ValueError:
            hint = ""
            if r.status_code == 503 and "lyra.finance" in self.base_url:
                hint = (" api.lyra.finance is the retired Derive v2 API; use the v3 base URL "
                        f"{BASE_URL_DEFAULT} (the default).")
            elif r.status_code == 503:
                hint = " Derive API unavailable (maintenance?) - check status / try again."
            raise SystemExit(f"{path}: HTTP {r.status_code}, non-JSON body: {text[:200]}{hint}")
        if isinstance(data, dict) and data.get("error"):
            raise SystemExit(f"{path}: API error {json.dumps(data['error'])[:400]}")
        if r.status_code >= 400:
            raise SystemExit(f"{path}: HTTP {r.status_code}: {json.dumps(data)[:400]}")
        return data.get("result", data) if isinstance(data, dict) else data

    def resolve_wallet(self) -> str:
        """Owner wallet for the signing key: given, looked up, or the signer itself."""
        if self.wallet:
            return self.wallet
        try:
            res = self.post("public/get_wallets_from_session_key", {"public_session_key": self.signer}, auth=False)
            wallets = res.get("wallets") or []
        except SystemExit:
            wallets = []
        if len(wallets) > 1:
            raise SystemExit(f"Session key {self.signer} is registered to several wallets {wallets}; pass --wallet.")
        self.wallet = wallets[0] if wallets else self.signer
        return self.wallet


# --------------------------------------------------------------------------- #
# Market data helpers
# --------------------------------------------------------------------------- #
def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def annualised_funding(hourly_rate: float) -> float:
    """Derive perp funding is quoted per hour; APR ~ rate * 24 * 365."""
    return hourly_rate * HOURS_PER_YEAR


def underlying_of(instrument: str) -> str:
    return instrument.split("-")[0]


def funding_stats(client: DeriveClient, instrument: str, days: int = 30) -> dict[str, Any]:
    """Average hourly funding over the trailing 1/7/30 days, as APR."""
    now_ms = int(time.time() * 1000)
    res = client.post(
        "public/get_funding_rate_history",
        {"instrument_name": instrument, "start_timestamp": now_ms - days * 86_400_000,
         "end_timestamp": now_ms, "period": 3600},
        auth=False,
    )
    hist = [(h["timestamp"], _f(h["funding_rate"])) for h in res.get("funding_rate_history", [])]
    out: dict[str, Any] = {"samples": len(hist)}
    for d in (1, 7, 30):
        rates = [r for ts, r in hist if ts >= now_ms - d * 86_400_000]
        if rates:
            out[f"apr_{d}d_pct"] = round(annualised_funding(statistics.mean(rates)) * 100, 3)
            out[f"min_apr_{d}d_pct"] = round(annualised_funding(min(rates)) * 100, 3)
    return out


def lending_rates(client: DeriveClient, currencies: set[str], risk_universe_id: Any) -> dict[str, dict[str, float]]:
    """{asset: {borrow_apy, supply_apy, pm2_mm_discount, pm2_im_discount}} for this risk universe."""
    out: dict[str, dict[str, float]] = {}
    for cur in sorted(currencies):
        try:
            res = client.post("public/get_currency", {"currency": cur}, auth=False)
        except SystemExit:
            continue
        for spot in res.get("spot") or []:
            for u in spot.get("universes") or []:
                if u.get("risk_universe_id") != risk_universe_id:
                    continue
                lending = u.get("lending") or {}
                out[spot.get("name")] = {
                    "borrow_apy": _f(lending.get("borrow_apy")),
                    "supply_apy": _f(lending.get("supply_apy")),
                    "pm2_mm_discount": _f(u.get("pm2_mm_discount")),
                    "pm2_im_discount": _f(u.get("pm2_im_discount")),
                }
    return out


def what_if(client: DeriveClient, sub_id: int, spec: str) -> dict[str, Any]:
    """Simulate margin after hypothetical changes via private/get_margin.

    spec: 'ETH-PERP=-30,WEETH=-86.16,WSTETH=76.44' - names with a '-' are
    perp/option instruments (position deltas), others collateral assets.
    """
    positions, collaterals = [], []
    for part in spec.split(","):
        if "=" not in part:
            continue
        name, amt = (s.strip() for s in part.split("=", 1))
        change = {"amount": f"{_f(amt):.6f}"}
        if "-" in name:
            positions.append({"instrument_name": name, **change})
        else:
            collaterals.append({"asset_name": name.upper(), **change})
    res = client.post(
        "private/get_margin",
        {"subaccount_id": sub_id, "simulated_position_changes": positions or None,
         "simulated_collateral_changes": collaterals or None},
    )
    return {"spec": spec} | {k: (_f(v) if k != "is_valid_trade" else v) for k, v in res.items() if k != "subaccount_id"}


def parse_staking_apr(spec: Optional[str]) -> dict[str, float]:
    """'WSTETH=2.7,WEETH=2.9' (percent) -> {'WSTETH': 0.027, 'WEETH': 0.029}."""
    out: dict[str, float] = {}
    for part in (spec or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = _f(v) / 100
    return out


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def _years_to(expiry_yyyymmdd: str) -> float:
    exp = datetime.strptime(expiry_yyyymmdd + "08", "%Y%m%d%H").replace(tzinfo=timezone.utc)
    return max((exp - datetime.now(timezone.utc)).total_seconds(), 0.0) / (365 * 86400)


def analyse(
    subaccount: dict[str, Any],
    tickers: dict[str, Any],
    funding: dict[str, dict[str, Any]],
    lending: dict[str, dict[str, float]],
    staking_apr: dict[str, float],
) -> dict[str, Any]:
    positions = subaccount.get("positions", []) or []
    collaterals = subaccount.get("collaterals", []) or []

    equity = _f(subaccount.get("subaccount_value"))
    mm = _f(subaccount.get("maintenance_margin"))  # net: MtM - requirement, < 0 => liquidation
    im = _f(subaccount.get("initial_margin"))  # net: < 0 => only risk-reducing trades
    projected = _f(subaccount.get("projected_margin_change"))

    delta_by_ccy: dict[str, float] = {}
    index_by_ccy: dict[str, float] = {}
    gross_notional = 0.0
    perp_rows, option_rows, collateral_rows = [], [], []
    carry = {"perp_funding_now": 0.0, "perp_funding_7d": 0.0, "perp_funding_30d": 0.0,
             "lending": 0.0, "staking": 0.0, "option_theta": 0.0}
    depeg_per_pct = 0.0
    forwards: dict[str, dict[str, Any]] = {}

    for c in collaterals:
        asset = c.get("asset_name")
        amount = _f(c.get("amount"))
        value = _f(c.get("mark_value"))
        dccy = c.get("delta_currency") or asset
        # Collateral delta is per unit, in units of delta_currency (wstETH ~1.25 ETH).
        if dccy not in CASH_CURRENCIES:
            unit_delta = _f(c.get("delta"), 1.0)
            delta_by_ccy[dccy] = delta_by_ccy.get(dccy, 0.0) + amount * unit_delta
            if unit_delta:
                index_by_ccy.setdefault(dccy, _f(c.get("mark_price")) / unit_delta)
        if dccy not in CASH_CURRENCIES and dccy != asset:
            depeg_per_pct += value * 0.01  # wrapped/LST collateral loses this per 1% depeg vs dccy
        rates = lending.get(asset, {})
        apy = rates.get("supply_apy", 0.0) if amount > 0 else rates.get("borrow_apy", 0.0)
        lending_usd = value * apy
        staking_usd = value * staking_apr.get(asset.upper(), 0.0) if amount > 0 else 0.0
        carry["lending"] += lending_usd
        carry["staking"] += staking_usd
        collateral_rows.append(
            {
                "asset": asset,
                "amount": amount,
                "usd_value": round(value, 2),
                "delta_ccy": dccy,
                "delta_per_unit": _f(c.get("delta"), 1.0),
                "lending_apy_pct": round(apy * 100, 3),
                "lending_usd_per_year": round(lending_usd, 2),
                "pm2_mm_discount": rates.get("pm2_mm_discount"),
                "pending_interest": _f(c.get("pending_interest")),
                "cumulative_interest": _f(c.get("cumulative_interest")),
                "unrealized_pnl": round(_f(c.get("unrealized_pnl")), 2),
            }
        )

    for p in positions:
        name = p.get("instrument_name", "")
        ccy = underlying_of(name)
        amount = _f(p.get("amount"))
        mark = _f(p.get("mark_price"))
        idx = _f(p.get("index_price"))
        delta = _f(p.get("delta"), 1.0)  # per-unit delta (1 for perps)
        upnl = _f(p.get("unrealized_pnl"))
        tk = tickers.get(name, {})
        idx = idx or _f(tk.get("I"))
        if idx:
            index_by_ccy[ccy] = idx
        notional = abs(amount) * idx
        gross_notional += notional
        delta_by_ccy[ccy] = delta_by_ccy.get(ccy, 0.0) + amount * delta

        if p.get("instrument_type") == "perp" or name.endswith("-PERP"):
            hourly = _f(tk.get("f"))
            fs = funding.get(name, {})
            basis = ((mark - idx) / idx) if idx else 0.0
            # Longs pay positive funding, shorts receive it.
            carry["perp_funding_now"] += -amount * idx * annualised_funding(hourly)
            carry["perp_funding_7d"] += -amount * idx * fs.get("apr_7d_pct", 0.0) / 100
            carry["perp_funding_30d"] += -amount * idx * fs.get("apr_30d_pct", 0.0) / 100
            perp_rows.append(
                {
                    "instrument": name,
                    "amount": amount,
                    "mark": mark,
                    "index": idx,
                    "basis_pct": round(basis * 100, 4),
                    "funding_hourly_pct": round(hourly * 100, 6),
                    "funding_apr_pct": round(annualised_funding(hourly) * 100, 3),
                    "funding_apr_7d_pct": fs.get("apr_7d_pct"),
                    "funding_apr_30d_pct": fs.get("apr_30d_pct"),
                    "funding_min_apr_30d_pct": fs.get("min_apr_30d_pct"),
                    "notional_usd": round(notional, 2),
                    "unrealized_pnl": round(upnl, 2),
                    "pending_funding": round(_f(p.get("pending_funding")), 2),
                    "cumulative_funding": round(_f(p.get("cumulative_funding")), 2),
                }
            )
        else:
            theta = _f(p.get("theta"))
            carry["option_theta"] += amount * theta * 365
            op = tk.get("option_pricing") or {}
            expiry = name.split("-")[1] if name.count("-") >= 3 else ""
            fwd = _f(op.get("f"))
            if expiry and fwd and idx and expiry not in forwards:
                t = _years_to(expiry)
                forwards[expiry] = {
                    "expiry": expiry,
                    "years": round(t, 4),
                    "forward": fwd,
                    "index": idx,
                    "basis_pct": round((fwd / idx - 1) * 100, 3),
                    "basis_apr_pct": round((fwd / idx - 1) / t * 100, 3) if t else None,
                    "discount_factor": _f(op.get("df")),
                }
            option_rows.append(
                {
                    "instrument": name,
                    "amount": amount,
                    "mark": mark,
                    "delta": delta,
                    "position_delta": round(amount * delta, 4),
                    "theta_usd_per_day": round(amount * theta, 2),
                    "notional_usd": round(notional, 2),
                    "unrealized_pnl": round(upnl, 2),
                }
            )

    # Perp funding vs dated-forward basis: the spread a short-perp / long-forward
    # (or long-spot financed) book earns.
    ref = next((r for r in perp_rows), None)
    ref_apr = (ref or {}).get("funding_apr_30d_pct")
    for f in forwards.values():
        f["perp_minus_forward_apr_pct"] = (
            round(ref_apr - f["basis_apr_pct"], 3) if ref_apr is not None and f["basis_apr_pct"] is not None else None
        )

    delta_usd_per_pct = sum(d * index_by_ccy.get(c, 0.0) * 0.01 for c, d in delta_by_ccy.items())
    carry_total_30d = carry["perp_funding_30d"] + carry["lending"] + carry["staking"] + carry["option_theta"]
    carry_total_now = carry["perp_funding_now"] + carry["lending"] + carry["staking"] + carry["option_theta"]

    return {
        "equity_usd": round(equity, 2),
        "margin": {
            "margin_type": subaccount.get("margin_type"),
            "risk_universe_id": subaccount.get("risk_universe_id"),
            "maintenance_headroom_usd": round(mm, 2),
            "initial_headroom_usd": round(im, 2),
            "maintenance_headroom_pct_equity": round(mm / equity * 100, 2) if equity else None,
            "projected_mm_after_next_expiry_usd": round(mm + projected, 2),
            "below_initial_margin": im < 0,
            "is_under_liquidation": subaccount.get("is_under_liquidation"),
        },
        "net_delta": {c: round(d, 4) for c, d in delta_by_ccy.items()},
        "net_delta_usd_per_1pct_move": round(delta_usd_per_pct, 2),
        "depeg_usd_per_1pct": round(depeg_per_pct, 2),
        "depeg_pct_to_mm_zero": round(mm / depeg_per_pct, 2) if depeg_per_pct and mm > 0 else None,
        "gross_notional_usd": round(gross_notional, 2),
        "num_positions": len(positions),
        "carry_usd_per_year": {k: round(v, 2) for k, v in carry.items()}
        | {"total_at_funding_now": round(carry_total_now, 2), "total_at_funding_30d": round(carry_total_30d, 2),
           "total_30d_pct_equity": round(carry_total_30d / equity * 100, 2) if equity else None},
        "forward_curve": [forwards[k] for k in sorted(forwards)],
        "perps": perp_rows,
        "options": option_rows,
        "collaterals": collateral_rows,
    }


def print_summary(wallet: str, sub_id: int, a: dict[str, Any]) -> None:
    line = "=" * 78
    m = a["margin"]
    print(line)
    print(f"Derive portfolio  |  wallet {wallet}  |  subaccount {sub_id}  |  {m['margin_type']}")
    print(f"Snapshot: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(line)
    print(f"Equity (subaccount value): ${a['equity_usd']:,.2f}   Gross notional: ${a['gross_notional_usd']:,.0f}")
    print(f"Maintenance headroom: ${m['maintenance_headroom_usd']:,.2f} ({m['maintenance_headroom_pct_equity']}% of equity)"
          f"   Initial headroom: ${m['initial_headroom_usd']:,.2f}"
          + ("  [BELOW IM: risk-reducing trades only]" if m["below_initial_margin"] else ""))
    print(f"MM after next expiry settles: ${m['projected_mm_after_next_expiry_usd']:,.2f}")
    deltas = "  ".join(f"{c}={d:+,.2f}" for c, d in a["net_delta"].items())
    print(f"Net delta: {deltas}   (${a['net_delta_usd_per_1pct_move']:+,.0f} per +1% move)")
    if a["depeg_usd_per_1pct"]:
        print(f"LST/wrapped depeg: -${a['depeg_usd_per_1pct']:,.0f} per 1% depeg"
              f"  -> MM headroom gone at ~{a['depeg_pct_to_mm_zero']}% depeg")
    print(f"Positions: {a['num_positions']}")
    if a["collaterals"]:
        print("\nCollaterals:")
        for c in a["collaterals"]:
            print(f"  {c['asset']:>8}  amount={c['amount']:<20,.6f} ${c['usd_value']:>14,.2f}"
                  f"  lendAPY={c['lending_apy_pct']:>6}%  ${c['lending_usd_per_year']:>+11,.0f}/yr")
    if a["perps"]:
        print("\nPerps (basis / funding):")
        for r in a["perps"]:
            print(
                f"  {r['instrument']:<12} amt={r['amount']:<10} basis={r['basis_pct']:>7}%  "
                f"fundingAPR now={r['funding_apr_pct']}% 7d={r['funding_apr_7d_pct']}% "
                f"30d={r['funding_apr_30d_pct']}% (min {r['funding_min_apr_30d_pct']}%)  "
                f"notional=${r['notional_usd']:,.0f}  uPnL=${r['unrealized_pnl']:,.2f}  "
                f"pendingFunding=${r['pending_funding']:,.2f}"
            )
    if a["forward_curve"]:
        print("\nForward curve (option-implied) vs perp funding (30d):")
        for f in a["forward_curve"]:
            print(f"  {f['expiry']}  T={f['years']:.2f}y  F={f['forward']:,.1f}  basis={f['basis_pct']}%"
                  f"  ({f['basis_apr_pct']}% APR)  perp-fwd spread={f['perp_minus_forward_apr_pct']}% APR")
    if a["options"]:
        print("\nOptions:")
        for r in a["options"]:
            print(
                f"  {r['instrument']:<22} amt={r['amount']:<8} delta={r['delta']:<8.4f} "
                f"posDelta={r['position_delta']:<9} theta/day=${r['theta_usd_per_day']:<8} "
                f"uPnL=${r['unrealized_pnl']:,.2f}"
            )
    cy = a["carry_usd_per_year"]
    print("\nCarry (USD / year, at current rates):")
    print(f"  perp funding     now ${cy['perp_funding_now']:>+12,.0f}   7d ${cy['perp_funding_7d']:>+12,.0f}"
          f"   30d ${cy['perp_funding_30d']:>+12,.0f}")
    print(f"  lending          ${cy['lending']:>+12,.0f}")
    print(f"  staking (--staking-apr) ${cy['staking']:>+12,.0f}")
    print(f"  option theta     ${cy['option_theta']:>+12,.0f}")
    print(f"  TOTAL            now ${cy['total_at_funding_now']:>+12,.0f}   30d ${cy['total_at_funding_30d']:>+12,.0f}"
          f"  ({cy['total_30d_pct_equity']}% of equity)")
    print(line)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch & analyse a Derive subaccount (v3 API).")
    ap.add_argument("--wallet", help="Owner wallet (EOA/multisig) for X-DeriveWallet; looked up if omitted.")
    ap.add_argument("--subaccount", type=int, help="Subaccount id, e.g. 65812.")
    ap.add_argument("--key-env", help="Name of env var holding the session private key.")
    ap.add_argument("--scan-env", action="store_true", help="Scan env for a key-shaped value.")
    ap.add_argument("--base-url", default=os.environ.get("DERIVE_BASE_URL", BASE_URL_DEFAULT))
    ap.add_argument("--json-out", help="Write the full JSON result to this file.")
    ap.add_argument("--no-tickers", action="store_true", help="Skip per-instrument ticker / funding fetch.")
    ap.add_argument("--staking-apr", help="Staking yield of LST collateral in %%, e.g. 'WSTETH=2.7,WEETH=2.9'.")
    ap.add_argument("--what-if", action="append", default=[],
                    help="Simulate margin after changes, e.g. 'ETH-PERP=-30,WEETH=-86.16,WSTETH=76.44'. Repeatable.")
    args = ap.parse_args(argv)

    pk, src_var, signer = discover_key(args.key_env, args.scan_env)
    print(f"[auth] using session key from env var '{src_var}'  (signer address {signer})", file=sys.stderr)

    client = DeriveClient(pk, args.wallet, args.base_url)

    # Sanity: public clock (also proves the API host is reachable).
    try:
        client.post("public/get_time", {}, auth=False)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2

    wallet = client.resolve_wallet()
    print(f"[auth] wallet {wallet}", file=sys.stderr)

    # Resolve subaccount(s).
    sub_id = args.subaccount
    if sub_id is None:
        subs = client.post("private/get_subaccounts", {"wallet": wallet})
        ids = subs.get("subaccount_ids") or subs.get("subaccounts") or []
        print(f"[info] subaccounts readable by this key for {wallet}: {ids}", file=sys.stderr)
        if not ids:
            return 3
        sub_id = ids[0] if not isinstance(ids[0], dict) else ids[0].get("subaccount_id")

    subaccount = client.post("private/get_subaccount", {"subaccount_id": sub_id})

    tickers: dict[str, Any] = {}
    funding: dict[str, dict[str, Any]] = {}
    lending: dict[str, dict[str, float]] = {}
    if not args.no_tickers:
        for p in subaccount.get("positions", []) or []:
            name = p.get("instrument_name")
            if name and name not in tickers:
                try:
                    tickers[name] = client.post("public/get_ticker", {"instrument_name": name}, auth=False)
                    if p.get("instrument_type") == "perp" or name.endswith("-PERP"):
                        funding[name] = funding_stats(client, name)
                except SystemExit as e:
                    print(f"[warn] {name}: {e}", file=sys.stderr)
        currencies = {c.get("currency") or c.get("asset_name") for c in subaccount.get("collaterals", []) or []}
        lending = lending_rates(client, {c for c in currencies if c}, subaccount.get("risk_universe_id"))

    analysis = analyse(subaccount, tickers, funding, lending, parse_staking_apr(args.staking_apr))
    print_summary(wallet, sub_id, analysis)
    if args.what_if:
        analysis["what_if"] = [what_if(client, sub_id, spec) for spec in args.what_if]
        print("What-if margin (net: positive = healthy):")
        for w in analysis["what_if"]:
            print(f"  {w['spec']:<48} IM {w.get('pre_initial_margin', 0):>+11,.0f} -> {w.get('post_initial_margin', 0):>+11,.0f}"
                  f"   MM {w.get('pre_maintenance_margin', 0):>+11,.0f} -> {w.get('post_maintenance_margin', 0):>+11,.0f}"
                  f"   valid={w.get('is_valid_trade')}")

    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "wallet": wallet,
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
