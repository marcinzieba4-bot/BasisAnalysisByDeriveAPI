#!/usr/bin/env python3
"""Plan moving USDC debt from one Derive v3 subaccount to another (read-only).

The loop this plans: withdraw collateral (e.g. WSTETH) from a subaccount whose
USDC borrow is expensive (e.g. a PRIME-universe ETH book), deposit it in a
subaccount of a cheaper lending universe (e.g. ALT, where XRP trades), borrow
USDC there against it and send the USDC back to repay the expensive debt.

Lending pools are per (asset, risk universe), so the two universes charge
different borrow rates. Each round is limited by the source subaccount's
initial-margin headroom: the collateral leaves at the source's margin credit
and comes back as `ltv * value` USDC, so every unit moved costs the source a
little headroom and the rounds shrink. This script asks Derive's own margin
engine (private/get_margin) how far you can still go in one step instead of
looping by hand, and reports what that does to the destination's liquidation
distance and to your interest bill. Nothing is signed or traded.

  python3 debt_shift.py --from 65812 --to 70848 --asset WSTETH
  python3 debt_shift.py --from 65812 --to 70848 --ltv 0.5 --perp ETH-PERP

The key (default env var DERIVE_API) must be able to read the source
subaccount; the destination is read through public/margin_watch, so it may
belong to a subaccount the key cannot read.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Any, Optional

import requests
from eth_account import Account
from eth_account.messages import encode_defunct

BASE_URL = os.environ.get("DERIVE_BASE_URL", "https://api.derive.xyz/v3")
HEX_KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
CALL_GAP_S = 0.45  # private/get_margin is rate limited (non-matching bucket)


class Derive:
    def __init__(self, key_env: str):
        raw = (os.environ.get(key_env) or "").strip()
        if not HEX_KEY_RE.match(raw):
            raise SystemExit(f"env var {key_env} is unset or not a 0x+64hex key")
        self.acct = Account.from_key(raw if raw.startswith("0x") else "0x" + raw)
        self.http = requests.Session()
        wallets = self.call("public/get_wallets_from_session_key",
                            {"public_session_key": self.acct.address.lower()}, auth=False).get("wallets") or []
        if len(wallets) != 1:
            raise SystemExit(f"session key {self.acct.address} maps to wallets {wallets}")
        self.wallet = wallets[0]

    def call(self, method: str, params: dict[str, Any], auth: bool = True) -> Any:
        for attempt in range(6):
            headers = {"content-type": "application/json"}
            if auth:
                ts = str(int(time.time() * 1000))
                sig = self.acct.sign_message(encode_defunct(text=ts)).signature.hex()
                headers.update({"X-DeriveWallet": self.wallet, "X-DeriveTimestamp": ts,
                                "X-DeriveSignature": sig if sig.startswith("0x") else "0x" + sig})
            r = self.http.post(f"{BASE_URL}/{method}", json=params, headers=headers, timeout=60)
            data = r.json()
            err = data.get("error") if isinstance(data, dict) else None
            if err and "Rate limit" in str(err.get("message", "")):
                time.sleep(1.0 + attempt)
                continue
            if err:
                raise SystemExit(f"{method}: {err}")
            return data.get("result", data)
        raise SystemExit(f"{method}: still rate limited")


def fnum(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def usdc_borrow_apy(currencies: list[dict[str, Any]]) -> dict[int, float]:
    for c in currencies:
        if c["currency"] == "USDC":
            return {u["risk_universe_id"]: fnum(u["lending"]["borrow_apy"])
                    for s in c["spot"] for u in s["universes"]}
    return {}


def collateral_discounts(universes: list[dict[str, Any]], universe_id: int, margin_type: str,
                         asset: str) -> tuple[float, float]:
    """(im_credit, mm_credit) per $1 of `asset` under that manager.

    get_risk_universes reports SM discounts as credit (0.72 = 72% counted) and
    PM2 discounts as haircut (0.28 = 28% taken off), so normalise to credit.
    """
    for u in universes:
        if u["risk_universe_id"] != universe_id:
            continue
        for m in u["managers"]:
            if m["margin_type"] != margin_type:
                continue
            for col in m["collaterals"]:
                if col["name"] == asset:
                    im, mm = fnum(col["im_discount"]), fnum(col["mm_discount"])
                    return (1 - im, 1 - mm) if margin_type == "PM2" else (im, mm)
    raise SystemExit(f"{asset} is not collateral for {margin_type} in universe {universe_id}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Plan shifting USDC debt between Derive subaccounts (read-only).")
    ap.add_argument("--from", dest="src", type=int, required=True, help="subaccount with expensive debt")
    ap.add_argument("--to", dest="dst", type=int, required=True, help="subaccount in the cheaper universe")
    ap.add_argument("--asset", default="WSTETH", help="collateral to move (default WSTETH)")
    ap.add_argument("--ltv", type=float, help="USDC borrowed per $1 moved (default: destination IM credit)")
    ap.add_argument("--buffer", type=float, default=0.0, help="IM headroom to leave in the source, USD")
    ap.add_argument("--perp", help="also test shorting/buying this perp in the source, e.g. ETH-PERP")
    ap.add_argument("--key-env", default="DERIVE_API")
    args = ap.parse_args(argv)

    d = Derive(args.key_env)
    src = d.call("private/get_subaccount", {"subaccount_id": args.src})
    dst = d.call("public/margin_watch", {"subaccount_id": args.dst}, auth=False)
    universes = d.call("public/get_risk_universes", {}, auth=False)
    apy = usdc_borrow_apy(d.call("public/get_all_currencies", {}, auth=False))

    held = {c["asset_name"]: c for c in src.get("collaterals", [])}
    if args.asset not in held:
        raise SystemExit(f"{args.src} holds no {args.asset}")
    px = fnum(held[args.asset]["mark_price"])
    have = fnum(held[args.asset]["amount"])
    src_debt = -min(0.0, fnum(held.get("USDC", {}).get("amount")))
    im_credit, mm_credit = collateral_discounts(universes, dst["risk_universe_id"], dst["margin_type"], args.asset)
    ltv = args.ltv if args.ltv is not None else im_credit
    r_src, r_dst = apy.get(src["risk_universe_id"], 0.0), apy.get(dst["risk_universe_id"], 0.0)

    def src_margin(n: float, perp: float = 0.0) -> tuple[float, float]:
        time.sleep(CALL_GAP_S)
        body: dict[str, Any] = {"subaccount_id": args.src, "simulated_collateral_changes": [
            {"asset_name": args.asset, "amount": f"{-n:.6f}"},
            {"asset_name": "USDC", "amount": f"{ltv * n * px:.6f}"}]}
        if perp:
            body["simulated_position_changes"] = [{"instrument_name": args.perp, "amount": f"{perp:.6f}"}]
        r = d.call("private/get_margin", body)
        return fnum(r["post_initial_margin"]), fnum(r["post_maintenance_margin"])

    im0, mm0 = src_margin(0.0)
    print(f"source {args.src} ({src['margin_type']}, universe {src['risk_universe_id']}): "
          f"{have:.4f} {args.asset} @ {px:,.2f}, USDC debt ${src_debt:,.0f} at {r_src:.2%}; "
          f"IM headroom ${im0:,.0f}, MM headroom ${mm0:,.0f}")
    print(f"dest   {args.dst} ({dst['margin_type']}, universe {dst['risk_universe_id']}): "
          f"IM headroom ${fnum(dst['initial_margin']):,.0f}, MM headroom ${fnum(dst['maintenance_margin']):,.0f}; "
          f"USDC borrow {r_dst:.2%}; {args.asset} credit IM {im_credit:.3f} / MM {mm_credit:.3f}; plan LTV {ltv:.3f}")

    # Largest n with source IM >= buffer (IM falls monotonically with n here).
    lo, hi = 0.0, have
    if src_margin(hi)[0] >= args.buffer:
        lo = hi
    else:
        for _ in range(14):
            mid = (lo + hi) / 2
            if src_margin(mid)[0] >= args.buffer:
                lo = mid
            else:
                hi = mid
    n = lo
    im_n, mm_n = src_margin(n)
    moved_usdc = ltv * n * px
    print(f"\nmax in one step: move {n:.4f} {args.asset} (${n * px:,.0f}) and send ${moved_usdc:,.0f} USDC back"
          f" -> source IM ${im_n:,.0f}, MM ${mm_n:,.0f}")
    print(f"interest saved: ${moved_usdc * (r_src - r_dst):,.0f}/yr at today's rates ({r_src:.2%} -> {r_dst:.2%})")

    # Destination after the move, using its per-collateral numbers from margin_watch.
    cols = {c["asset_name"]: c for c in dst.get("collaterals", [])}
    a_val = fnum(cols.get(args.asset, {}).get("mark_value")) + n * px
    usdc = fnum(cols.get("USDC", {}).get("amount")) - moved_usdc
    dst_mm = fnum(dst["maintenance_margin"]) + n * px * mm_credit - moved_usdc
    dst_im = fnum(dst["initial_margin"]) + n * px * im_credit - moved_usdc
    print(f"dest after: {args.asset} ${a_val:,.0f}, USDC {usdc:,.0f}; IM headroom ~${dst_im:,.0f}, MM headroom ~${dst_mm:,.0f}")
    if a_val > 0 and not dst.get("positions"):
        drop = dst_mm / (mm_credit * a_val)
        print(f"dest liquidates if {args.asset} falls ~{drop:.1%} (nothing in that subaccount hedges it)")

    if args.perp:
        print(f"\nsource IM with {args.perp} change, moving {n:.4f} more {args.asset} as above:")
        for p in (-30, -20, -10, 10):
            print(f"  {args.perp} {p:+}: IM ${src_margin(n, p)[0]:,.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
