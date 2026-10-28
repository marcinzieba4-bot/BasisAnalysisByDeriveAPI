#!/usr/bin/env python3
"""Attribute the P&L of a Derive basis trade: long synthetic forward vs short perp.

The trade, per currency: buy a call and sell a put at the same strike and
expiry (a synthetic long forward) and sell the perp against it. You pay the
forward's premium over the perp ("basis") and collect perp funding until the
options expire.

Given the time the trade was opened (--since), this script:
  1. splits the subaccount's value change since then into trading fees,
     filled-vs-mark (spread paid on entry), mark drift on the new legs, and
     the rest of the book;
  2. compares the basis locked in against expected funding to expiry, at the
     current and the trailing-average funding rate, and gives the break-even
     funding APR;
  3. reports the book's per-currency net delta (old book vs new trade);
  4. asks Derive's margin engine what happens if the options expire while
     the perps stay open.

Usage
-----
  python3 basis_pnl.py --key-env DERIVE_4842_API --subaccount 61222 \\
      --since 2026-10-28T15:45:00Z
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from derive_portfolio import BASE_URL_DEFAULT, DeriveClient, _f, discover_key


def _ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def _utc(ms: Any) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc)


def value_at(client: DeriveClient, sub_id: int, t0: datetime) -> float:
    """Subaccount value at the last 15-minute point at or before t0."""
    rows = client.paged(
        "private/get_subaccount_value_history",
        {"subaccount_id": sub_id, "start_timestamp": _ms(t0) - 3_600_000,
         "end_timestamp": _ms(t0) + 60_000, "period": 900},
        "subaccount_value_history",
    )
    rows = [r for r in rows if int(r["timestamp"]) <= _ms(t0)]
    if not rows:
        raise SystemExit("No value history at --since; pass --value-at-since.")
    return _f(max(rows, key=lambda r: int(r["timestamp"]))["subaccount_value"])


def _sign(trade: dict[str, Any]) -> float:
    return 1.0 if trade["direction"] == "buy" else -1.0


def build_legs(trades: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    legs: dict[str, dict[str, float]] = {}
    for r in trades:
        leg = legs.setdefault(r["instrument_name"], {"qty": 0.0, "cost": 0.0, "fees": 0.0, "vs_mark": 0.0})
        q = _sign(r) * _f(r["trade_amount"])
        leg["qty"] += q
        leg["cost"] += q * _f(r["trade_price"])
        leg["fees"] += _f(r["trade_fee"])
        leg["vs_mark"] += q * (_f(r["mark_price"]) - _f(r["trade_price"]))  # >0: filled better than mark
    return legs


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Attribute P&L of a synthetic-forward vs perp basis trade.")
    ap.add_argument("--subaccount", type=int, required=True)
    ap.add_argument("--since", required=True, help="Trade start, ISO-8601 UTC, e.g. 2026-10-28T15:45:00Z.")
    ap.add_argument("--value-at-since", type=float, help="Override subaccount value at --since.")
    ap.add_argument("--funding-lookback-days", type=int, default=21)
    ap.add_argument("--wallet", help="Owner wallet address (X-DeriveWallet header).")
    ap.add_argument("--key-env", help="Name of env var holding the session private key.")
    ap.add_argument("--scan-env", action="store_true", help="Scan env for a key-shaped value.")
    ap.add_argument("--base-url", default=os.environ.get("DERIVE_BASE_URL", BASE_URL_DEFAULT))
    ap.add_argument("--json-out", help="Write the full JSON result to this file.")
    args = ap.parse_args(argv)

    pk, src_var, signer = discover_key(args.key_env, args.scan_env)
    print(f"[auth] using session key from env var '{src_var}'  (signer address {signer})", file=sys.stderr)
    client = DeriveClient(pk, args.wallet, args.base_url)
    sid = args.subaccount
    t0 = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)

    trades = client.paged("private/get_trade_history",
                          {"subaccount_id": sid, "from_timestamp": _ms(t0), "to_timestamp": _ms(now)}, "trades")
    legs = build_legs(trades)
    if not legs:
        raise SystemExit(f"No fills on subaccount {sid} since {t0.isoformat()}.")
    v0 = args.value_at_since if args.value_at_since is not None else value_at(client, sid, t0)
    sa = client.post("private/get_subaccount", {"subaccount_id": sid})
    v1 = _f(sa["subaccount_value"])
    tick = {}
    for n in legs:
        try:
            tick[n] = client.post("public/get_ticker", {"instrument_name": n}, auth=False)
        except SystemExit:
            raise SystemExit(f"No live ticker for {n} (expired?). Pick a --since after its expiry.")
    pos = {p["instrument_name"]: p for p in sa.get("positions", [])}

    # ---- 1. value change attribution ------------------------------------------------------------
    rows, fees, vs_mark, mtm = [], 0.0, 0.0, 0.0
    for n, leg in legs.items():
        mark = _f(tick[n]["M"])
        leg_mtm = leg["qty"] * mark - leg["cost"] - leg["fees"]
        fees += leg["fees"]
        vs_mark += leg["vs_mark"]
        mtm += leg_mtm
        avg_fill = leg["cost"] / leg["qty"] if abs(leg["qty"]) > 1e-12 else float("nan")  # round trip
        rows.append({"instrument": n, "qty": leg["qty"], "avg_fill": avg_fill, "mark": mark,
                     "fees": leg["fees"], "filled_vs_mark": leg["vs_mark"], "mtm": leg_mtm})
    attribution = {
        "value_at_since": v0, "value_now": v1, "change": v1 - v0,
        "fees": -fees, "filled_vs_mark": vs_mark, "mark_drift": mtm - (vs_mark - fees),
        "new_trade_total": mtm, "rest_of_book": (v1 - v0) - mtm,
    }

    # ---- 2. locked basis vs funding, per synthetic + perp pair ----------------------------------
    lookback_start = _ms(now) - args.funding_lookback_days * 86_400_000
    pairs = []
    for n in legs:
        parts = n.split("-")
        if len(parts) != 4 or parts[3] != "C":
            continue
        ccy, _expiry, strike, _ = parts
        put, perp = f"{ccy}-{parts[1]}-{strike}-P", f"{ccy}-PERP"
        if any(abs(legs.get(x, {}).get("qty", 0.0)) < 1e-12 for x in (n, put, perp)):
            continue
        inst = client.post("public/get_instrument", {"instrument_name": n}, auth=False)
        expiry = datetime.fromtimestamp(int(inst["option_details"]["expiry"]), timezone.utc)
        days = (expiry - now).total_seconds() / 86400
        qty = legs[n]["qty"]
        fwd_locked = float(strike) + legs[n]["cost"] / qty - legs[put]["cost"] / legs[put]["qty"]
        perp_avg = legs[perp]["cost"] / legs[perp]["qty"]
        basis = fwd_locked - perp_avg
        notional = qty * _f(tick[perp]["M"])
        hist = client.post("public/get_funding_rate_history",
                           {"instrument_name": perp, "start_timestamp": lookback_start,
                            "end_timestamp": _ms(now), "period": 3600}, auth=False)
        trailing = statistics.mean(_f(r["funding_rate"]) for r in hist["funding_rate_history"])
        current = _f(tick[perp]["f"])
        entry_fees = legs[n]["fees"] + legs[put]["fees"] + legs[perp]["fees"]
        taker_rate = legs[perp]["fees"] / abs(legs[perp]["cost"]) if legs[perp]["cost"] else 0.0003
        exit_fee = notional * taker_rate
        fixed = basis * qty + entry_fees + exit_fee
        pairs.append({
            "currency": ccy, "call": n, "put": put, "perp": perp, "qty": qty, "expiry": expiry.isoformat(),
            "days_to_expiry": days, "forward_locked": fwd_locked, "perp_sold": perp_avg,
            "basis_per_unit": basis, "basis_apr_pct": basis / perp_avg * 365 / days * 100,
            "basis_cost": -basis * qty, "entry_fees": -entry_fees, "est_exit_fee": -exit_fee,
            "funding_apr_now_pct": current * 24 * 365 * 100,
            "funding_apr_trailing_pct": trailing * 24 * 365 * 100,
            "net_to_expiry_now": notional * current * 24 * days - fixed,
            "net_to_expiry_trailing": notional * trailing * 24 * days - fixed,
            "breakeven_funding_apr_pct": fixed / (notional * days / 365) * 100,
        })

    # ---- 3. per-currency delta: old book vs new trade -------------------------------------------
    delta = {}
    for p in sa.get("positions", []):
        n, ccy = p["instrument_name"], p["instrument_name"].split("-")[0]
        per_unit = _f(p.get("delta"), 1.0)
        new_qty = legs.get(n, {}).get("qty", 0.0)
        d = delta.setdefault(ccy, {"old": 0.0, "new": 0.0})
        d["old"] += (_f(p["amount"]) - new_qty) * per_unit
        d["new"] += new_qty * per_unit
    for c in sa.get("collaterals", []):
        ccy = c.get("delta_currency")
        if ccy and ccy != "USDC":
            delta.setdefault(ccy, {"old": 0.0, "new": 0.0})["old"] += _f(c["amount"]) * _f(c.get("delta"), 1.0)

    # ---- 4. margin if the options expire and the perps stay open --------------------------------
    expire = [{"instrument_name": n, "amount": str(-legs[n]["qty"])}
              for n in legs if not n.endswith("-PERP") and n in pos]
    expiry_check = client.post("private/get_margin", {"subaccount_id": sid, "simulated_position_changes": expire})

    # ---- report ----------------------------------------------------------------------------------
    a = attribution
    print("=" * 78)
    print(f"Basis trade P&L  |  subaccount {sid}  |  since {t0:%Y-%m-%d %H:%M} UTC  |  now {now:%Y-%m-%d %H:%M} UTC")
    print("=" * 78)
    print(f"{'leg':<24}{'qty':>9}{'avg fill':>11}{'mark':>11}{'fees':>9}{'vs mark':>10}{'MtM':>10}")
    for r in rows:
        print(f"{r['instrument']:<24}{r['qty']:>9.3f}{r['avg_fill']:>11.2f}{r['mark']:>11.2f}"
              f"{-r['fees']:>9.2f}{r['filled_vs_mark']:>10.2f}{r['mtm']:>10.2f}")
    print(f"\nValue {a['value_at_since']:,.2f} -> {a['value_now']:,.2f}  ({a['change']:+,.2f})")
    print(f"  fees {a['fees']:+,.2f} | filled vs mark {a['filled_vs_mark']:+,.2f} | mark drift "
          f"{a['mark_drift']:+,.2f} | new trade {a['new_trade_total']:+,.2f} | rest of book {a['rest_of_book']:+,.2f}")
    for p in pairs:
        print(f"\n{p['currency']}: {p['qty']:g} x synthetic {p['call'].rsplit('-', 1)[0]} vs {p['perp']}  "
              f"({p['days_to_expiry']:.1f} days to expiry)")
        print(f"  forward locked {p['forward_locked']:,.2f} vs perp sold {p['perp_sold']:,.2f} -> basis "
              f"{p['basis_per_unit']:,.2f}/unit ({p['basis_apr_pct']:.2f}% APR) = {p['basis_cost']:+,.2f}")
        print(f"  funding now {p['funding_apr_now_pct']:.2f}% APR, trailing {args.funding_lookback_days}d "
              f"{p['funding_apr_trailing_pct']:.2f}% APR | break-even {p['breakeven_funding_apr_pct']:.2f}% APR")
        print(f"  expected net to expiry: {p['net_to_expiry_now']:+,.2f} at current funding, "
              f"{p['net_to_expiry_trailing']:+,.2f} at trailing funding")
    print("\nNet delta (old book / new trade / total):")
    for ccy, d in delta.items():
        print(f"  {ccy:<6} {d['old']:+9.3f} / {d['new']:+9.3f} / {d['old'] + d['new']:+9.3f}")
    post_mm = _f(expiry_check["post_maintenance_margin"])
    print(f"\nFree MM now {_f(expiry_check['pre_maintenance_margin']):,.2f}; if the options expire with the "
          f"perps still open: {post_mm:,.2f}")
    if post_mm < 0:
        print("  WARNING: that is below maintenance margin. Buy back the perps at expiry (or roll) — "
              "do not let the options settle against open perps.")
    print("=" * 78)

    if args.json_out:
        out = {"fetched_at": now.isoformat(), "subaccount_id": sid, "since": t0.isoformat(), "legs": rows,
               "attribution": attribution, "pairs": pairs, "delta": delta, "expiry_check": expiry_check}
        with open(args.json_out, "w") as fh:
            json.dump(out, fh, indent=2, default=str)
        print(f"[info] wrote {args.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
