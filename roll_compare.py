#!/usr/bin/env python3
"""BTC vs ETH for the monthly basis trade on Derive: a long synthetic forward
(buy call + sell put, same strike, monthly expiry) against a short perp.

You pay the option-implied financing (the forward's premium over the index)
and collect perp funding until expiry, rolling every month. This script asks
"which underlying pays better, and how much leverage can it carry" using
public data only (no key needed):

  live      Derive v3 API, now: implied financing of the next monthly expiries
            (at mark, and at the executable top of book for the synthetic),
            perp funding now and trailing (share of hours at the 10.95% APR
            anchor, below it, negative), PM2 margin for the hedged book (max
            leverage at initial margin, leverage at liquidation), and the
            carry and return on equity that implies.

  history   Every past monthly roll since Derive launched: forward premium at
            entry, funding realised to expiry, perp premium drift, and carry.
            Before the v3 cutover (2026-10-06) everything is read from Derive
            Chain archive state: the option forward feed, the perp's
            aggregatedFunding, and the spot and perp feeds. After the cutover,
            funding comes from the v3 funding history (option forwards are not
            published there, so v3-era rolls need their entry on chain).
            --path also samples every day inside each window to replay
            mark-to-market and margin at the chosen leverage. Raw samples are
            cached in a JSON file, so re-runs only fetch what is missing.

Derive perp funding is a premium term plus a static 0.00125%/hour (10.95% APR).
The premium term is zero while the index sits inside the perp's impact bid/ask,
so a balanced perp pays shorts exactly 10.95% APR; a perp trading at a discount
pays less, or negative. The trade earns that anchor minus the option-implied
financing, so it lives or dies on how often the perp stays at the anchor.

  breakdown --breakdown itemises the cost of the long forward: financing by
            strike including the USDC borrowed to prepay an ITM call (and its
            interest), and the cost of each roll method (2-way: close and reopen
            the synthetic, keep the perp; 3-way: close and reopen all three
            legs; expire + perp bridge), as screen taker and maker, with the
            margin each path needs and recent public RFQ fills.

Usage
-----
  python3 roll_compare.py                                  # live snapshot
  python3 roll_compare.py --breakdown --leverage 20 25     # + cost breakdown
  python3 roll_compare.py --history                        # + every past roll
  python3 roll_compare.py --history --path --leverage 20 25 30
"""
from __future__ import annotations

import argparse
import bisect
import calendar
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from eth_utils import keccak

API_DEFAULT = "https://api.derive.xyz/v3"
RPC_DEFAULT = "https://rpc.derive.xyz"
ANCHOR = 0.0000125  # static interest rate per hour: 10.95% APR
HOURS = 24 * 365
MIN_MONEYNESS = 0.9  # lowest strike / index considered liquid enough to close at the next roll
FEE_CAP = 0.125  # option fee is capped at 12.5% of the option price
CUTOVER = int(datetime(2026, 10, 6, 17, 40, tzinfo=timezone.utc).timestamp())  # last v2 funding update
FIRST_ROLL = int(datetime(2023, 12, 29, 8, tzinfo=timezone.utc).timestamp())
BLOCK_REF = (46881370, 1793784355)  # Derive Chain block <-> unix time; blocks are exactly 2 s apart


def utc(t: float) -> datetime:
    return datetime.fromtimestamp(t, timezone.utc)


def last_friday(y: int, m: int) -> int:
    day = calendar.monthrange(y, m)[1]
    while calendar.weekday(y, m, day) != 4:
        day -= 1
    return int(datetime(y, m, day, 8, tzinfo=timezone.utc).timestamp())


def monthly_expiries(start: int, end: int) -> list[int]:
    d = utc(start)
    y, m, out = d.year, d.month, []
    while True:
        e = last_friday(y, m)
        if e > end:
            return out
        if e >= start:
            out.append(e)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def is_monthly(expiry: int) -> bool:
    d = utc(expiry)
    return expiry == last_friday(d.year, d.month)


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
class Derive:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"accept": "application/json", "content-type": "application/json"})

    def post(self, path: str, body: dict[str, Any]) -> Any:
        for attempt in range(5):
            r = self.s.post(f"{self.base}/{path}", data=json.dumps(body), timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        try:
            j = r.json()
        except ValueError:
            raise SystemExit(f"{path}: HTTP {r.status_code}: {r.text[:200]}")
        if isinstance(j, dict) and j.get("error"):
            raise SystemExit(f"{path}: API error {json.dumps(j['error'])[:300]}")
        return j.get("result", j)


class Chain:
    """Batched, throttled eth_call against the public Derive Chain archive RPC."""

    SIGS = ["aggregatedFunding()", "getSpot()", "getResult()", "getForwardPrice(uint64)",
            "getSettlementPrice(uint64)", "settlementFeed()", "spotFeed()", "perpFeed()"]

    def __init__(self, url: str, per_batch: int = 10, interval: float = 1.1):
        self.url, self.per_batch, self.interval = url, per_batch, interval
        self.s = requests.Session()
        self.sel = {s: "0x" + keccak(text=s)[:4].hex() for s in self.SIGS}
        self._last = 0.0

    @staticmethod
    def block_at(t: int) -> int:
        n, ts = BLOCK_REF
        return n - (ts - t) // 2

    def data(self, sig: str, arg: Optional[int] = None) -> str:
        return self.sel[sig] + ("" if arg is None else hex(arg)[2:].rjust(64, "0"))

    def calls(self, items: list[tuple[Any, str, str, int]], label: str = "") -> dict[Any, Optional[list[int]]]:
        """items: (key, to, calldata, block). Returns key -> list of 32-byte words, or None if reverted."""
        out: dict[Any, Optional[list[int]]] = {}
        pending, done = list(items), 0
        while pending:
            chunk, pending = pending[: self.per_batch], pending[self.per_batch:]
            time.sleep(max(0.0, self.interval - (time.time() - self._last)))
            body = [{"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [{"to": to, "data": data}, hex(b)]}
                    for i, (_, to, data, b) in enumerate(chunk)]
            try:
                resp = self.s.post(self.url, json=body, timeout=60).json()
            except (requests.RequestException, ValueError):
                resp = None
            self._last = time.time()
            if not isinstance(resp, list):
                pending = chunk + pending
                time.sleep(3)
                continue
            by_id, retry = {x.get("id"): x for x in resp}, []
            for i, item in enumerate(chunk):
                x = by_id.get(i)
                if x is None or ("error" in x and x["error"].get("code") == -32017):  # rate limited
                    retry.append(item)
                elif "error" in x:
                    out[item[0]] = None  # reverted: feed stale at that block, or no data
                else:
                    h = x["result"]
                    out[item[0]] = [int(h[2 + 64 * k: 66 + 64 * k], 16) for k in range((len(h) - 2) // 64)]
            if retry:
                pending = retry + pending
                time.sleep(1.5)
            done += len(chunk) - len(retry)
            if label and done % 500 < len(chunk) - len(retry):
                print(f"[chain] {label}: {done}/{len(items)}", file=sys.stderr)
        return out


def _i256(v: int) -> int:
    return v - (1 << 256) if v >= 1 << 255 else v


# --------------------------------------------------------------------------- #
# Live snapshot
# --------------------------------------------------------------------------- #
def funding_stats(rates: list[float]) -> dict[str, float]:
    n = len(rates) or 1
    return {
        "mean_apr": statistics.fmean(rates) * HOURS if rates else float("nan"),
        "at_anchor": sum(abs(r - ANCHOR) < 1e-10 for r in rates) / n,
        "below_anchor": sum(r < ANCHOR - 1e-10 for r in rates) / n,
        "negative": sum(r < 0 for r in rates) / n,
        "hourly_sd_apr": statistics.pstdev(rates) * HOURS if len(rates) > 1 else 0.0,
    }


def synthetic_quotes(api: Derive, ccy: str, expiry: int, S: float, now: float) -> dict[str, Any]:
    exp = utc(expiry).strftime("%Y%m%d")
    tick = api.post("public/get_tickers", {"currency": ccy, "instrument_type": "option", "expiry_date": exp})["tickers"]
    any_t = next(iter(tick.values()))
    F, df = float(any_t["option_pricing"]["f"]), float(any_t["option_pricing"]["df"])
    days = (expiry - now) / 86400
    best = None
    for name, c in tick.items():
        if not name.endswith("-C"):
            continue
        K = float(name.split("-")[2])
        p = tick.get(name[:-1] + "P")
        # ITM-call side, but not deep: strikes below ~0.9 x index have no two-sided book near expiry,
        # so they are cheap to open and expensive to close at the next roll.
        if p is None or K > S or K < MIN_MONEYNESS * S:
            continue
        ca, pb = float(c["a"] or 0), float(p["b"] or 0)
        if ca <= 0 or float(c["A"] or 0) <= 0 or float(p["B"] or 0) <= 0:
            continue
        cm, pm = float(c["M"]), float(p["M"])
        fwd_exec, fwd_mark = K + ca - pb, K + cm - pm
        if best is None or fwd_exec - fwd_mark < best["exec_minus_mark"]:
            best = {"strike": K, "fwd_exec": fwd_exec, "fwd_mark": fwd_mark, "exec_minus_mark": fwd_exec - fwd_mark,
                    "call": name, "put": name[:-1] + "P", "call_mark": cm, "put_mark": pm}
    return {"expiry": exp, "days": days, "forward": F, "df": df,
            "implied_apr": (F / S - 1) * 365 / days, "best": best}


def pm2_margin(api: Derive, ccy: str, q: dict[str, Any], notional: float, S: float) -> dict[str, float]:
    b = q["best"]
    n = round(notional / S, 3)
    usdc = 10 * notional
    r = api.post("public/get_margin", {
        "margin_type": "PM2", "market": ccy,
        "simulated_collaterals": [{"asset_name": "USDC", "amount": str(usdc)}],
        "simulated_positions": [{"instrument_name": b["call"], "amount": str(n)},
                                {"instrument_name": b["put"], "amount": str(-n)},
                                {"instrument_name": f"{ccy}-PERP", "amount": str(-n)}]})
    value = usdc + n * (b["call_mark"] - b["put_mark"])
    im, mm = value - float(r["post_initial_margin"]), value - float(r["post_maintenance_margin"])
    return {"days": q["days"], "im_pct": im / (n * S), "mm_pct": mm / (n * S)}


def live(api: Derive, ccy: str, now: float, notional: float, fee_rate: float) -> dict[str, Any]:
    perp = api.post("public/get_ticker", {"instrument_name": f"{ccy}-PERP"})
    S, mark, f_now = float(perp["I"]), float(perp["M"]), float(perp["f"])
    hist = api.post("public/get_funding_rate_history", {
        "instrument_name": f"{ccy}-PERP", "start_timestamp": int((now - 30 * 86400) * 1000),
        "end_timestamp": int(now * 1000), "period": 3600})["funding_rate_history"]
    rates = [float(r["funding_rate"]) for r in hist]
    ins = api.post("public/get_all_instruments", {"currency": ccy, "instrument_type": "option",
                                                  "expired": False, "page_size": 1000})["instruments"]
    expiries = sorted({int(i["option_details"]["expiry"]) for i in ins if i["is_active"]})
    monthlies = [e for e in expiries if is_monthly(e) and (e - now) > 2 * 86400][:2]
    quotes = [synthetic_quotes(api, ccy, e, S, now) for e in monthlies]
    margins = [pm2_margin(api, ccy, q, notional, S) for q in quotes if q["best"]]
    near = [e for e in expiries if 0.5 * 86400 <= e - now <= 3.5 * 86400]  # proxy for the old monthly on roll day
    old = _chain(api, ccy, near[0]) if near else {}

    def fee(price: float) -> float:  # taker fee per leg: min(rate x index, 12.5% x option price); base fee ignored
        return min(fee_rate * S, FEE_CAP * price)

    for q in quotes:
        b, days = q["best"], q["days"]
        if b:
            # Roll cost (taker, 2-way): open this synthetic + close a same-moneyness one about a day before expiry.
            # Executable financing = implied at mark + roll cost. Interest on premium prepaid for an ITM strike
            # is in --breakdown (option marks discount it at ~3.5-4%, below the USDC borrow rate).
            close = 0.0
            if old:
                _, oc, op = old[_nearest(old, b["strike"])]
                ocm, opm, ocb, opa = float(oc["M"]), float(op["M"]), _px(oc, "b"), _px(op, "a")
                if ocb is not None and opa is not None and ocb > 0.5 * ocm:
                    close = (ocm - ocb) + (opa - opm) + fee(ocm) + fee(opm)
            q["roll_cost_pct"] = (b["exec_minus_mark"] + fee(b["call_mark"]) + fee(b["put_mark"]) + close) / S
            q["implied_exec_apr"] = q["implied_apr"] + q["roll_cost_pct"] * 365 / days
    return {"currency": ccy, "index": S, "perp_mark": mark, "perp_premium": mark / S - 1,
            "funding_now_apr": f_now * HOURS, "funding_7d": funding_stats(rates[-168:]),
            "funding_30d": funding_stats(rates), "quotes": quotes, "margins": margins}


def print_live(snaps: list[dict[str, Any]], leverage: list[float]) -> None:
    print("=" * 96)
    print(f"LIVE  {utc(time.time()):%Y-%m-%d %H:%M} UTC   (carry = funding - implied financing; ROE = leverage x carry)")
    print("=" * 96)
    for s in snaps:
        f7, f30 = s["funding_7d"], s["funding_30d"]
        print(f"\n{s['currency']}: index {s['index']:,.2f}  perp {s['perp_premium'] * 1e4:+.1f}bp  funding now "
              f"{s['funding_now_apr'] * 100:+.2f}% | 7d {f7['mean_apr'] * 100:.2f}% | 30d {f30['mean_apr'] * 100:.2f}% APR")
        print(f"  30d hours: at anchor {f30['at_anchor'] * 100:.0f}%, below {f30['below_anchor'] * 100:.0f}%, "
              f"negative {f30['negative'] * 100:.0f}%, hourly sd {f30['hourly_sd_apr'] * 100:.1f}% APR")
        for q in s["quotes"]:
            b = q["best"]
            line = (f"  {q['expiry']} ({q['days']:.1f}d): implied {q['implied_apr'] * 100:.2f}% APR at mark "
                    f"(df {q['df']})")
            if b:
                line += (f"; cheapest synthetic K={b['strike']:,.0f} (>= {MIN_MONEYNESS:g}x index): with roll cost "
                         f"{q['implied_exec_apr'] * 100:.2f}% APR (open + close, taker: {q['roll_cost_pct'] * 1e4:.0f}bp)")
            print(line)
        for m in s["margins"]:
            print(f"  PM2 margin, {m['days']:.0f}d synthetic vs perp: IM {m['im_pct'] * 100:.2f}% -> max "
                  f"{1 / m['im_pct']:.1f}x | MM {m['mm_pct'] * 100:.2f}% -> liquidation at {1 / m['mm_pct']:.1f}x")
        q = s["quotes"][0] if s["quotes"] else None
        if q and q["best"]:
            for lbl, f in (("now", s["funding_now_apr"]), ("7d", f7["mean_apr"]), ("30d", f30["mean_apr"])):
                c_mark, c_exec = f - q["implied_apr"], f - q["implied_exec_apr"]
                roe = "  ".join(f"{L:g}x {L * c_exec * 100:+.0f}%" for L in leverage)
                print(f"  carry @ funding {lbl:>3}: {c_mark * 100:+.2f}% at mark, {c_exec * 100:+.2f}% executable"
                      f" -> ROE {roe}")
    print()


# --------------------------------------------------------------------------- #
# Breakdown: financing by strike (incl. the USDC borrow) and roll cost by method
# --------------------------------------------------------------------------- #
TARGET_MONEYNESS = (1.0, 0.95, 0.9, 0.85, 0.8)


def _px(t: dict[str, Any], side: str) -> Optional[float]:
    """Best bid ('b') or ask ('a') with size, else None."""
    size = float(t.get(side.upper()) or 0)
    return float(t[side]) if size > 0 and t.get(side) is not None else None


def usdc_rates(api: Derive, ccy: str) -> tuple[float, float]:
    """(borrow APY, supply APY) of USDC in the risk universe the currency trades in."""
    cur = api.post("public/get_currency", {"currency": ccy})
    uid = (cur.get("managers") or [{}])[0].get("risk_universe_id", 1)
    for u in api.post("public/get_currency", {"currency": "USDC"})["spot"][0]["universes"]:
        if u["risk_universe_id"] == uid and u.get("lending"):
            return float(u["lending"]["borrow_apy"]), float(u["lending"]["supply_apy"])
    raise SystemExit("USDC lending rates not found")


def _chain(api: Derive, ccy: str, expiry: int) -> dict[float, tuple[str, dict[str, Any], dict[str, Any]]]:
    tick = api.post("public/get_tickers", {"currency": ccy, "instrument_type": "option",
                                           "expiry_date": utc(expiry).strftime("%Y%m%d")})["tickers"]
    return {float(n.split("-")[2]): (n[:-2], tick[n], tick[n[:-1] + "P"])
            for n in tick if n.endswith("-C") and n[:-1] + "P" in tick}


def _nearest(chain: dict[float, Any], target: float) -> float:
    return min(chain, key=lambda k: abs(k - target))


def _margin_pct(api: Derive, ccy: str, positions: list[tuple[str, float]], value: float, notional: float) -> tuple[float, float]:
    usdc = 10 * notional
    r = api.post("public/get_margin", {
        "margin_type": "PM2", "market": ccy, "simulated_collaterals": [{"asset_name": "USDC", "amount": str(usdc)}],
        "simulated_positions": [{"instrument_name": n, "amount": f"{a:.6f}"} for n, a in positions]})
    v = usdc + value
    return (v - float(r["post_initial_margin"])) / notional, (v - float(r["post_maintenance_margin"])) / notional


def rfq_synthetic_stats(api: Derive, ccy: str, now: float, days: int) -> dict[str, tuple[int, float, float]]:
    """Public RFQ fills of 2-leg synthetics (call + put, same strike/expiry): net price vs mark."""
    legs: dict[str, list[dict[str, Any]]] = {}
    t, end = int((now - days * 86400) * 1000), int(now * 1000)
    while t < end:
        t2, page = min(t + 2 * 86400_000, end), 1
        while True:
            r = api.post("public/get_trade_history", {"currency": ccy, "instrument_type": "option", "from_timestamp": t,
                                                      "to_timestamp": t2, "page_size": 1000, "page": page})
            for x in r.get("trades", []):
                if x.get("rfq_id") and x.get("liquidity_role") == "taker":
                    legs.setdefault(x["rfq_id"], []).append(x)
            if page >= ((r.get("pagination") or {}).get("num_pages") or 1) or not r.get("trades"):
                break
            page += 1
        t = t2
    buckets: dict[str, list[tuple[float, float]]] = {}
    for pk in legs.values():
        if len(pk) != 2:
            continue
        a, b = sorted(pk, key=lambda x: x["instrument_name"][-1])  # C first, then P
        na, nb = a["instrument_name"].split("-"), b["instrument_name"].split("-")
        if (na[3], nb[3]) != ("C", "P") or na[1:3] != nb[1:3] or a["trade_amount"] != b["trade_amount"]:
            continue
        S, K = float(a["index_price"]), float(na[2])
        dte = (int(datetime.strptime(na[1], "%Y%m%d").replace(hour=8, tzinfo=timezone.utc).timestamp()) * 1000
               - a["timestamp"]) / 86400_000
        sgn = {"buy": 1.0, "sell": -1.0}
        net = sum(sgn[x["direction"]] * (float(x["trade_price"]) - float(x["mark_price"])) for x in (a, b)) / S
        kind = {("buy", "sell"): "open", ("sell", "buy"): "close"}.get((a["direction"], b["direction"]))
        if kind == "open" and 10 <= dte <= 40 and 0.9 <= K / S <= 1.05:
            key = "open 10-40d, K 0.9-1.05x"
        elif kind == "close" and dte <= 3:
            key = "close <=3d, K 0.9-1.05x" if 0.9 <= K / S <= 1.05 else ("close <=3d, K <0.88x" if K / S < 0.88 else "")
        else:
            key = ""
        if key:
            buckets.setdefault(key, []).append((net, float(a["trade_amount"]) * S))
    return {k: (len(v), sum(n for _, n in v), statistics.median(x for x, _ in v)) for k, v in buckets.items()}


def breakdown(api: Derive, ccy: str, now: float, leverage: list[float], notional: float,
              rates: tuple[float, float], rfq_days: int) -> dict[str, Any]:
    perp = api.post("public/get_ticker", {"instrument_name": f"{ccy}-PERP"})
    S, pbid, pask = float(perp["I"]), float(perp["b"]), float(perp["a"])
    inst = api.post("public/get_instrument", {"instrument_name": f"{ccy}-PERP"})
    p_taker, p_maker = float(inst["taker_fee_rate"]), float(inst["maker_fee_rate"])
    ins = api.post("public/get_all_instruments", {"currency": ccy, "instrument_type": "option",
                                                  "expired": False, "page_size": 1000})["instruments"]
    o_taker, o_maker = float(ins[0]["taker_fee_rate"]), float(ins[0]["maker_fee_rate"])
    expiries = sorted({int(i["option_details"]["expiry"]) for i in ins if i["is_active"]})
    monthlies = [e for e in expiries if is_monthly(e) and e - now > 7 * 86400]
    new = min(monthlies[:2], key=lambda e: abs((e - now) / 86400 - 28))  # the one closest to a fresh monthly
    near = [e for e in expiries if 0.5 * 86400 <= e - now <= 3.5 * 86400]  # proxy: old monthly on roll day
    T = (new - now) / 86400 / 365
    rb, rs = rates
    cn = _chain(api, ccy, new)
    co = _chain(api, ccy, near[0]) if near else {}

    def fee(rate: float, price: float) -> float:
        return min(rate * S, FEE_CAP * price)

    # Discount rate inside option marks: C - P = df x (F - K)  ->  df, by least squares on strikes away from F.
    F = float(next(iter(cn.values()))[1]["option_pricing"]["f"])
    pts = [(F - k, float(c["M"]) - float(p["M"])) for k, (_, c, p) in cn.items() if abs(F - k) > 0.05 * S]
    df_marks = sum(x * y for x, y in pts) / sum(x * x for x, _ in pts)
    r_marks = -math.log(df_marks) / T

    fin, roll = [], []
    for m in TARGET_MONEYNESS:
        K = _nearest(cn, m * S)
        if any(r["strike"] == K for r in fin):
            continue
        base, c, p = cn[K]
        cm, pm, ca, pb = float(c["M"]), float(p["M"]), _px(c, "a"), _px(p, "b")
        prem_m = cm - pm
        prem_x = ca - pb if ca is not None and pb is not None else None
        row = {"strike": K, "moneyness": K / S, "premium_pct": prem_m / S,
               "fwd_apr_mark": (K + prem_m - S) / S / T,
               "fwd_apr_exec": (K + prem_x - S) / S / T if prem_x is not None else None, "by_lev": {}}
        for L in leverage:
            eq = 1 / L
            borrow, used = max(prem_m / S - eq, 0.0), min(max(prem_m / S, 0.0), eq)
            interest = rb * borrow + rs * used  # APR on notional: USDC borrowed + equity no longer earning supply
            row["by_lev"][L] = {"borrow_pct": borrow, "interest_apr": interest,
                                "total_apr": row["fwd_apr_mark"] + interest}
        fin.append(row)

        # Roll legs, per unit of notional
        open_x = ((ca - cm) + (pm - pb)) / S if prem_x is not None else None
        open_fee_t = (fee(o_taker, cm) + fee(o_taker, pm)) / S
        open_fee_m = (fee(o_maker, cm) + fee(o_maker, pm)) / S
        close_x = close_fee_t = close_fee_m = None
        if co:
            Ko = _nearest(co, m * S)
            _, oc, op = co[Ko]
            ocm, opm, ocb, opa = float(oc["M"]), float(op["M"]), _px(oc, "b"), _px(op, "a")
            if abs(Ko / S - m) < 0.03:
                close_fee_t = (fee(o_taker, ocm) + fee(o_taker, opm)) / S
                close_fee_m = (fee(o_maker, ocm) + fee(o_maker, opm)) / S
                if ocb is not None and opa is not None and ocb > 0.5 * ocm:  # a real two-sided book
                    close_x = ((ocm - ocb) + (opa - opm)) / S
        roll.append({"strike": K, "moneyness": K / S, "open_cross": open_x, "open_fee_taker": open_fee_t,
                     "open_fee_maker": open_fee_m, "close_cross": close_x, "close_fee_taker": close_fee_t,
                     "close_fee_maker": close_fee_m})

    perp_rt_taker = (pask - pbid) / S + 2 * p_taker
    perp_rt_maker = 2 * p_maker
    # Margin of each roll path, as % of the position's notional (PM2 simulation).
    n = notional / S
    Kn = _nearest(cn, S)
    bn, c, p = cn[Kn]
    v_new = n * (float(c["M"]) - float(p["M"]))
    im_new, mm_new = _margin_pct(api, ccy, [(bn + "-C", n), (bn + "-P", -n), (f"{ccy}-PERP", -n)], v_new, notional)
    bridge = None
    if co:
        Ko = _nearest(co, S)
        bo, oc, op = co[Ko]
        v_old = n * (float(oc["M"]) - float(op["M"]))
        bridge = _margin_pct(api, ccy, [(bo + "-C", n), (bo + "-P", -n), (bn + "-C", n), (bn + "-P", -n),
                                        (f"{ccy}-PERP", -2 * n)], v_old + v_new, notional)
    naked = _margin_pct(api, ccy, [(f"{ccy}-PERP", -n)], 0.0, notional)
    rfq = rfq_synthetic_stats(api, ccy, now, rfq_days) if rfq_days > 0 else {}
    return {"currency": ccy, "index": S, "new_expiry": utc(new).strftime("%Y%m%d"), "days": T * 365,
            "near_expiry": utc(near[0]).strftime("%Y%m%d") if near else None,
            "near_days": (near[0] - now) / 86400 if near else None,
            "forward_apr": (F / S - 1) / T, "marks_discount_apr": r_marks, "usdc_borrow": rb, "usdc_supply": rs,
            "financing": fin, "roll": roll, "perp_rt_taker": perp_rt_taker, "perp_rt_maker": perp_rt_maker,
            "im_new": im_new, "mm_new": mm_new, "bridge_im_mm": bridge, "naked_perp_im_mm": naked, "rfq": rfq}


def print_breakdown(bds: list[dict[str, Any]], leverage: list[float], notional: float, rfq_days: int) -> None:
    def bp(x: Optional[float]) -> str:
        return "   n/a" if x is None else f"{x * 1e4:6.1f}"

    print("=" * 96)
    print(f"BREAKDOWN  {utc(time.time()):%Y-%m-%d %H:%M} UTC   per ${notional:,.0f} notional; APR = % per year of notional")
    print("=" * 96)
    for b in bds:
        print(f"\n{b['currency']}: index {b['index']:,.2f} | new monthly {b['new_expiry']} ({b['days']:.1f}d), forward "
              f"{b['forward_apr'] * 100:.2f}% APR | option marks discount prepaid premium at {b['marks_discount_apr'] * 100:.2f}%"
              f" | USDC borrow {b['usdc_borrow'] * 100:.2f}% / supply {b['usdc_supply'] * 100:.2f}%")
        print("  1) Financing of the long forward by strike (premium prepaid in USDC; equity = 1/leverage in USDC)")
        hdr = f"     {'strike':>9} {'K/S':>5} {'prepaid':>8} {'fwd@mark':>9} {'fwd@exec':>9}"
        for L in leverage:
            hdr += f" | {f'{L:g}x borrow':>12} {'$borrowed':>10} {'interest':>8} {'TOTAL':>7}"
        print(hdr)
        for r in b["financing"]:
            line = (f"     {r['strike']:>9,.0f} {r['moneyness']:>5.2f} {r['premium_pct'] * 100:>7.1f}% "
                    f"{r['fwd_apr_mark'] * 100:>8.2f}% " + (f"{r['fwd_apr_exec'] * 100:>8.2f}%" if r["fwd_apr_exec"] is not None
                                                             else f"{'n/a':>9}"))
            for L in leverage:
                x = r["by_lev"][L]
                line += (f" | {x['borrow_pct'] * 100:>11.1f}% {x['borrow_pct'] * notional:>10,.0f} "
                         f"{x['interest_apr'] * 100:>7.2f}% {x['total_apr'] * 100:>6.2f}%")
            print(line)
        print(f"  2) Roll cost per monthly roll, bp of notional (x12 = APR). Old monthly near expiry proxied by "
              f"{b['near_expiry']} ({(b['near_days'] or 0):.1f}d)")
        print(f"     {'strike':>9} {'K/S':>5} | {'open new':>8} {'+fees':>6} | {'close old':>9} {'+fees':>6} |"
              f" {'2-way':>6} {'3-way':>6} {'expire+bridge':>13} | maker: {'2-way':>6} {'3-way':>6} {'bridge':>6}")
        for r in b["roll"]:
            o = None if r["open_cross"] is None else r["open_cross"] + r["open_fee_taker"]
            c = None if r["close_cross"] is None else r["close_cross"] + r["close_fee_taker"]
            two = None if o is None or c is None else o + c
            three = None if two is None else two + b["perp_rt_taker"]
            brg = None if o is None else o + b["perp_rt_taker"]
            m2 = None if r["close_fee_maker"] is None else r["open_fee_maker"] + r["close_fee_maker"]
            m3 = None if m2 is None else m2 + b["perp_rt_maker"]
            mb = r["open_fee_maker"] + b["perp_rt_maker"]
            print(f"     {r['strike']:>9,.0f} {r['moneyness']:>5.2f} | {bp(r['open_cross']):>8} {bp(r['open_fee_taker']):>6} |"
                  f" {bp(r['close_cross']) if r['close_cross'] is not None else ('no book' if r['close_fee_taker'] is not None else 'n/a'):>9}"
                  f" {bp(r['close_fee_taker']):>6} | {bp(two):>6} {bp(three):>6} {bp(brg):>13} |"
                  f"        {bp(m2):>6} {bp(m3):>6} {bp(mb):>6}")
        print(f"     perp round trip: taker {b['perp_rt_taker'] * 1e4:.1f}bp, maker {b['perp_rt_maker'] * 1e4:.1f}bp")
        im_b = b["bridge_im_mm"]
        print(f"  3) Margin by roll path (PM2, % of notional): normal book IM {b['im_new'] * 100:.2f}% / MM {b['mm_new'] * 100:.2f}%"
              + (f" | expire+bridge (old + new synth, 2x perp) IM {im_b[0] * 100:.2f}% / MM {im_b[1] * 100:.2f}%" if im_b else "")
              + f" | naked perp after settlement IM {b['naked_perp_im_mm'][0] * 100:.1f}% / MM {b['naked_perp_im_mm'][1] * 100:.1f}%")
        if im_b:
            need = "  ".join(f"{L:g}x needs +{max(im_b[0] - 1 / L, 0) * notional:,.0f} USDC" for L in leverage)
            print(f"     expire+bridge extra collateral for the hours around expiry: {need}")
        if b["rfq"]:
            print(f"  4) Public RFQ fills, last {rfq_days}d (2-leg synthetics, median net vs mark, before fees): " + "; ".join(
                f"{k}: {v[2] * 1e4:+.1f}bp (n={v[0]}, ${v[1] / 1e6:.1f}m)" for k, v in sorted(b["rfq"].items())))
    print()


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
def resolve_addresses(api: Derive, chain: Chain, ccy: str) -> dict[str, str]:
    ins = api.post("public/get_all_instruments", {"currency": ccy, "instrument_type": "option",
                                                  "expired": False, "page_size": 1})["instruments"]
    option = ins[0]["base_asset_address"]
    perp = api.post("public/get_instrument", {"instrument_name": f"{ccy}-PERP"})["base_asset_address"]
    blk = chain.block_at(CUTOVER - 86400)
    got = chain.calls([("fwd", option, chain.data("settlementFeed()"), blk),
                       ("spot", perp, chain.data("spotFeed()"), blk),
                       ("perpfeed", perp, chain.data("perpFeed()"), blk)])
    a = {k: "0x" + hex(v[0])[2:].rjust(40, "0") for k, v in got.items() if v}
    return {"perp": perp, "forward": a["fwd"], "spot": a["spot"], "perpfeed": a["perpfeed"]}


def collect(api: Derive, chain: Chain, ccy: str, cache: dict[str, Any], with_path: bool, now: float) -> None:
    """Fill cache[ccy] with raw chain samples: s[t] = {af, spot, perp, fwd:{expiry: px}}, settle[expiry]."""
    c = cache.setdefault(ccy, {"addr": None, "s": {}, "settle": {}})
    if not c["addr"]:
        c["addr"] = resolve_addresses(api, chain, ccy)
    a = c["addr"]
    exps = monthly_expiries(FIRST_ROLL, int(now) + 40 * 86400)
    want: list[tuple[int, int]] = []  # (sample time, expiry whose forward we need, 0 = none)
    for e_prev, e in zip(exps, exps[1:]):
        if e_prev >= CUTOVER:
            break
        want.append((e_prev, e))
        if with_path:
            want += [(t, e) for t in range(e_prev + 86400, min(e, CUTOVER), 86400)]
        if e <= CUTOVER:
            want.append((e, 0))
    want.append((CUTOVER, 0))
    todo: dict[tuple[int, str, int], tuple[str, str, int]] = {}
    for t, e in want:
        row = c["s"].get(str(t), {})
        b = chain.block_at(t)
        for k, to, sig in (("af", a["perp"], "aggregatedFunding()"), ("spot", a["spot"], "getSpot()"),
                           ("perp", a["perpfeed"], "getResult()")):
            if k not in row:
                todo[(t, k, 0)] = (to, chain.data(sig), b)
        if e and str(e) not in row.get("fwd", {}):
            todo[(t, "fwd", e)] = (a["forward"], chain.data("getForwardPrice(uint64)", e), b)
    for e in exps:
        if e + 3600 < CUTOVER and str(e) not in c["settle"]:
            todo[(e, "settle", 0)] = (a["forward"], chain.data("getSettlementPrice(uint64)", e), chain.block_at(e + 3600))
    items = [(k, *v) for k, v in todo.items()]
    if not items:
        return
    print(f"[chain] {ccy}: {len(items)} reads to fetch", file=sys.stderr)
    got = chain.calls(items, ccy)
    # A feed read reverts when the feed was not updated within its heartbeat at that exact block:
    # retry a little later (5 min, 30 min, 1 h).
    for off in (300, 1800, 3600):
        redo = [(k, to, d, b + off // 2) for (k, to, d, b) in items if got.get(k) is None and k[1] != "af"]
        if not redo:
            break
        got.update({k: v for k, v in chain.calls(redo).items() if v is not None})
    for (t, kind, e), w in got.items():
        if kind == "settle":
            c["settle"][str(t)] = (w[1] / 1e18 if w and w[0] else None)
            continue
        row = c["s"].setdefault(str(t), {})
        if kind == "fwd":
            row.setdefault("fwd", {})[str(e)] = w[0] / 1e18 if w else None
        elif kind == "af":
            row["af"] = _i256(w[0]) / 1e18 if w else None
        else:
            row[kind] = w[0] / 1e18 if w else None


def v3_funding(api: Derive, ccy: str, now: float) -> tuple[list[int], list[float], dict[int, float]]:
    """Hourly v3 funding rates since the cutover, plus daily 08:00 index (daily option settlements)."""
    rows, t, end = [], CUTOVER * 1000, int(now * 1000)
    while t < end:
        t2 = min(t + 20 * 86400_000, end)
        rows += api.post("public/get_funding_rate_history", {"instrument_name": f"{ccy}-PERP", "start_timestamp": t,
                                                             "end_timestamp": t2, "period": 3600})["funding_rate_history"]
        t = t2
    seen, ts, rates = set(), [], []
    for r in sorted(rows, key=lambda r: r["timestamp"]):
        if r["timestamp"] not in seen:
            seen.add(r["timestamp"])
            ts.append(int(r["timestamp"]) // 1000)
            rates.append(float(r["funding_rate"]))
    st = api.post("public/get_option_settlement_prices", {"currency": ccy})["expiries"]
    px = {int(x["utc_expiry_sec"]): float(x["price"]) for x in st if x.get("price")}
    return ts, rates, px


def windows(ccy: str, c: dict[str, Any], v3: tuple[list[int], list[float], dict[int, float]],
            now: float) -> list[dict[str, Any]]:
    s = {int(k): v for k, v in c["s"].items()}
    settle = {int(k): v for k, v in c["settle"].items() if v}
    v3_ts, v3_rates, v3_px = v3
    settle.update({k: v for k, v in v3_px.items() if k not in settle})
    px_days = sorted(v3_px)
    af_cut = s[CUTOVER]["af"]

    def af_at(t: int) -> Optional[float]:
        if t <= CUTOVER:
            return (s.get(t) or {}).get("af")
        if t > now:
            return None
        af = af_cut
        for h, r in zip(v3_ts, v3_rates):
            if h >= t:
                break
            i = bisect.bisect_right(px_days, h) - 1
            p = v3_px[px_days[max(i, 0)]] if px_days else None
            if p is None:
                return None
            af += r * p
        return af

    out = []
    exps = monthly_expiries(FIRST_ROLL, int(now) + 40 * 86400)
    for e_prev, e in zip(exps, exps[1:]):
        r0 = s.get(e_prev)
        if e_prev >= CUTOVER or e > now or not r0:
            continue
        S0, P0, AF0, F0 = r0.get("spot"), r0.get("perp"), r0.get("af"), (r0.get("fwd") or {}).get(str(e))
        AFE, SE = af_at(e), settle.get(e)
        if None in (S0, P0, AF0, F0, AFE, SE) or AF0 == 0:
            continue
        rE = s.get(e) or {}
        PE = rE.get("perp")
        days = (e - e_prev) / 86400
        fwd_prem, funding = (F0 - S0) / S0, (AFE - AF0) / S0
        drift = ((P0 - S0) - (PE - SE)) / S0 if PE else 0.0
        path = []
        for t in range(e_prev, min(e, CUTOVER), 86400):
            r = s.get(t) or {}
            fw = (r.get("fwd") or {}).get(str(e))
            if None in (fw, r.get("perp"), r.get("af"), r.get("spot")):
                continue
            mtm = ((fw - F0) - (r["perp"] - P0) + (r["af"] - AF0)) / S0  # per unit of entry notional
            path.append((mtm, r["spot"] / S0, (e - t) / 86400))
        a = 365 / days
        out.append({"entry": utc(e_prev).date().isoformat(), "expiry": utc(e).date().isoformat(), "days": days,
                    "spot_move": SE / S0 - 1, "implied_apr": fwd_prem * a, "funding_apr": funding * a,
                    "carry_apr": (funding - fwd_prem) * a, "drift_apr": drift * a,
                    "gross_apr": (funding - fwd_prem + drift) * a, "exit_on_v3": e > CUTOVER, "path": path})
    return out


def margin_at(margins: list[dict[str, float]], days: float) -> tuple[float, float]:
    """Linear in days to expiry through the live PM2 points (front and next monthly)."""
    if len(margins) < 2:
        m = margins[0]
        return m["im_pct"], m["mm_pct"]
    a, b = margins[0], margins[1]
    f = (days - a["days"]) / (b["days"] - a["days"])
    return a["im_pct"] + f * (b["im_pct"] - a["im_pct"]), a["mm_pct"] + f * (b["mm_pct"] - a["mm_pct"])


def replay(w: dict[str, Any], margins: list[dict[str, float]], lev: float) -> Optional[dict[str, float]]:
    """Equity = 1/lev + MtM (per unit of entry notional) vs margin x spot, at each daily sample."""
    if not w["path"]:
        return None
    eq0, worst, max_lev, im_short = 1 / lev, 1e9, 0.0, False
    for mtm, spot_rel, dleft in w["path"]:
        im, mm = margin_at(margins, dleft)
        eq = eq0 + mtm
        worst = min(worst, (eq - mm * spot_rel) / eq0)
        max_lev = max(max_lev, spot_rel / eq if eq > 0 else float("inf"))
        im_short |= eq < im * spot_rel
    return {"mm_buffer_min": worst, "max_lev": max_lev, "liquidated": worst < 0, "below_im": im_short}


def print_history(res: dict[str, list[dict[str, Any]]], snaps: dict[str, dict[str, Any]], leverage: list[float]) -> None:
    print("=" * 96)
    print("HISTORY  monthly rolls: enter at the previous monthly expiry (08:00 UTC), hold to expiry")
    print("  implied = forward premium over index at entry; funding = perp funding realised to expiry;")
    print("  carry = funding - implied; drift = perp premium change entry->expiry (nets out across rolls)")
    print("=" * 96)
    for ccy, ws in res.items():
        print(f"\n{ccy}  {'entry':<11}{'expiry':<11}{'spot':>7}{'implied':>9}{'funding':>9}{'carry':>8}{'drift':>8}", end="")
        print("".join(f"{f'{L:g}x MM buf':>12}" for L in leverage))
        for w in ws:
            print(f"     {w['entry']:<11}{w['expiry']:<11}{w['spot_move'] * 100:>6.1f}%{w['implied_apr'] * 100:>8.2f}%"
                  f"{w['funding_apr'] * 100:>8.2f}%{w['carry_apr'] * 100:>7.2f}%{w['drift_apr'] * 100:>7.2f}%", end="")
            for L in leverage:
                r = replay(w, snaps[ccy]["margins"], L)
                print(f"{'':>12}" if r is None else f"{r['mm_buffer_min'] * 100:>10.0f}%{'!' if r['liquidated'] else ' '} ",
                      end="")
            print("  (exit on v3)" if w["exit_on_v3"] else "")
    print("\nSummary (APR on notional; ROE = leverage x (carry - roll cost x 12))")
    hdr = f"{'':<5}{'rolls':>6}{'implied':>9}{'funding':>9}{'carry':>8}{'sd':>7}{'worst':>8}{'<0':>5}{'cost':>7}"
    print(hdr + "".join(f"{f'ROE {L:g}x':>10}" for L in leverage) + "".join(f"{f'liq {L:g}x':>8}" for L in leverage))
    for ccy, ws in res.items():
        if not ws:
            continue
        carry = [w["carry_apr"] for w in ws]
        q = (snaps[ccy]["quotes"] or [{}])[0]
        cost = q.get("roll_cost_pct", 0.0) * 12
        line = (f"{ccy:<5}{len(ws):>6}{statistics.fmean(w['implied_apr'] for w in ws) * 100:>8.2f}%"
                f"{statistics.fmean(w['funding_apr'] for w in ws) * 100:>8.2f}%{statistics.fmean(carry) * 100:>7.2f}%"
                f"{statistics.pstdev(carry) * 100:>6.2f}%{min(carry) * 100:>7.2f}%{sum(x < 0 for x in carry):>5}"
                f"{cost * 100:>6.2f}%")
        line += "".join(f"{L * (statistics.fmean(carry) - cost) * 100:>9.0f}%" for L in leverage)
        for L in leverage:
            reps = [r for r in (replay(w, snaps[ccy]["margins"], L) for w in ws) if r]
            cell = f"{sum(r['liquidated'] for r in reps)}/{len(reps)}" if reps else "-"
            line += f"{cell:>8}"
        print(line)
        years: dict[str, list[float]] = {}
        for w in ws:
            years.setdefault(w["expiry"][:4], []).append(w["carry_apr"])
        print("      by year: " + "  ".join(f"{y} {statistics.fmean(v) * 100:+.2f}% ({len(v)})" for y, v in years.items()))
        last = carry[-12:]
        print(f"      last {len(last)} rolls: carry {statistics.fmean(last) * 100:+.2f}%, worst {min(last) * 100:+.2f}%, "
              f"{sum(x < 0 for x in last)} negative -> ROE "
              + "  ".join(f"{L:g}x {L * (statistics.fmean(last) - cost) * 100:+.0f}%" for L in leverage))
    print()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="BTC vs ETH: monthly synthetic forward vs short perp on Derive.")
    ap.add_argument("--currencies", nargs="+", default=["BTC", "ETH"])
    ap.add_argument("--leverage", nargs="+", type=float, default=[20, 25, 30], help="Notional / equity to evaluate.")
    ap.add_argument("--notional", type=float, default=1_000_000, help="USD notional for the margin simulation.")
    ap.add_argument("--fee-rate", type=float, default=0.0003, help="Option taker fee rate (of index) per leg.")
    ap.add_argument("--breakdown", action="store_true",
                    help="Financing by strike incl. the USDC borrow, and roll cost by method (2-way, 3-way, expire+bridge).")
    ap.add_argument("--rfq-days", type=int, default=14, help="With --breakdown: days of public RFQ fills to measure (0 = skip).")
    ap.add_argument("--history", action="store_true", help="Replay every past monthly roll (Derive Chain archive).")
    ap.add_argument("--path", action="store_true", help="With --history: daily samples for MtM/margin replay (slow).")
    ap.add_argument("--cache", default="roll_history_cache.json", help="Raw chain sample cache (git-ignored).")
    ap.add_argument("--json-out", help="Write live snapshot and history rows to this file.")
    ap.add_argument("--api", default=os.environ.get("DERIVE_BASE_URL", API_DEFAULT))
    ap.add_argument("--rpc", default=os.environ.get("DERIVE_CHAIN_RPC", RPC_DEFAULT))
    args = ap.parse_args(argv)

    api, now = Derive(args.api), time.time()
    snaps = {c: live(api, c, now, args.notional, args.fee_rate) for c in args.currencies}
    print_live(list(snaps.values()), args.leverage)
    out: dict[str, Any] = {"live": snaps}

    if args.breakdown:
        bds = [breakdown(api, c, now, args.leverage, args.notional, usdc_rates(api, c), args.rfq_days)
               for c in args.currencies]
        print_breakdown(bds, args.leverage, args.notional, args.rfq_days)
        out["breakdown"] = bds

    if args.history:
        chain = Chain(args.rpc)
        cache = json.load(open(args.cache)) if os.path.exists(args.cache) else {}
        res = {}
        for c in args.currencies:
            collect(api, chain, c, cache, args.path, now)
            with open(args.cache, "w") as fh:
                json.dump(cache, fh)
            res[c] = windows(c, cache[c], v3_funding(api, c, now), now)
        print_history(res, snaps, args.leverage)
        out["history"] = res
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(out, fh, indent=1, default=str)
        print(f"[info] wrote {args.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
