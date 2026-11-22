#!/usr/bin/env python3
"""Estimate the spot price at which a Derive portfolio-margin (PM2) subaccount is liquidated.

Derive's margin simulator (public/get_margin) only prices a portfolio at *today's*
spot. To find the liquidation point we re-implement the documented PM2 maintenance
margin (https://docs.derive.xyz/portfolio-margin.md and -parameters.md):

  MM = MtM + sum_ccy [ 0.8 * maxLoss_ccy + perp/option/risk-cancelling contingencies ]
           + plain-collateral haircuts
  maxLoss = min(regular 23-scenario grid, dampened tail ladder, skew rails, forward loss)

check it against Derive's live MM at today's spot, then shift spot (forwards move with
spot, implied vols sticky by strike, instant move) and solve for MM = 0.

Optional what-ifs are applied first, priced at mark (bid/ask with --fill touch):
  --trade NAME=QTY        buy (+) / sell (-) an option or perp, cash at mark
  --settle-expiry YYYYMMDD settle every leg of that expiry at today's forward

Read-only: nothing is traded.

  python3 liquidation_check.py --subaccount 65813
  python3 liquidation_check.py --subaccount 65813 --trade ETH-20270129-1400-P=-40
"""
from __future__ import annotations

import argparse
import copy
import math
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

from basis_projection import Portfolio, black76, parse_option_name
from derive_portfolio import BASE_URL_DEFAULT, DeriveClient, _f

YEAR = 365.0
MM_FACTOR = 0.8

# Per-currency PM2 parameters (portfolio-margin-parameters.md).
PARAMS: dict[str, dict[str, Any]] = {
    "ETH": dict(step=0.04, up=0.40, down=0.25, minvol=0.50, perp=0.0075, opt=0.0015, fwd=0.04, add=0.5,
                tails=[(-0.66, 0.14), (-0.33, 0.28), (0.5, 0.1848), (1.0, 0.0924), (2.0, 0.0231),
                       (3.0, 0.010267), (4.0, 0.005775), (5.0, 0.003696)]),
    "BTC": dict(step=0.035, up=0.40, down=0.25, minvol=0.50, perp=0.0075, opt=0.0015, fwd=0.035, add=0.5,
                tails=[(-0.66, 0.122624), (-0.33, 0.245), (0.5, 0.161741), (1.0, 0.080871), (2.0, 0.020218),
                       (3.0, 0.008976), (4.0, 0.005044), (5.0, 0.003228)]),
    "SOL": dict(step=0.06, up=0.50, down=0.30, minvol=0.60, perp=0.01, opt=0.0025, fwd=0.06, add=0.25,
                tails=[(-0.66, 0.272711), (-0.33, 0.545422), (0.5, 0.36), (1.0, 0.18), (2.0, 0.045022),
                       (3.0, 0.02), (4.0, 0.011244), (5.0, 0.0072)]),
    "XAUT": dict(step=0.025, up=0.30, down=0.15, minvol=0.20, perp=0.01, opt=0.0025, fwd=0.025, add=0.5,
                 tails=[(-0.66, 0.091667), (-0.33, 0.181667), (0.5, 0.12), (1.0, 0.06), (2.0, 0.015),
                        (3.0, 0.006667), (4.0, 0.00375), (5.0, 0.0024)]),
}
MULT_FACTOR = 2.0
# Risk-cancelling collateral (shocked inside the currency bucket) with MM haircuts.
RC_HAIRCUT = {
    "ETH": {"ETH": 0.06, "WETH": 0.06, "WSTETH": 0.086, "WEETH": 0.156},
    "BTC": {"BTC": 0.126, "WBTC": 0.126, "CBBTC": 0.126, "LBTC": 0.186},
    "SOL": {"SOL": 0.11, "JITOSOL": 0.11},
}
# Plain collateral MM haircuts (credited at value * (1 - haircut)); USDC is the cash asset.
PLAIN_HAIRCUT = {"USDC": 0.0, "DRV": 0.85, "XAUT": 0.05, "USDT": 0.02, "USDE": 0.15, "SUSDE": 0.2,
                 "ETH": 0.2, "WSTETH": 0.2, "BTC": 0.25, "SOL": 0.25}
PLAIN_UNDERLYING = {"XAUT": "XAUT", "ETH": "ETH", "WSTETH": "ETH", "BTC": "BTC", "SOL": "SOL", "JITOSOL": "SOL"}
GRID = [(4, "U"), (3, "U"), (3, "S"), (3, "D"), (2, "U"), (2, "S"), (2, "D"), (1, "U"), (1, "S"), (1, "D"),
        (0, "U"), (0, "S"), (0, "D"), (-1, "U"), (-1, "S"), (-1, "D"), (-2, "U"), (-2, "S"), (-2, "D"),
        (-3, "U"), (-3, "S"), (-3, "D"), (-4, "U")]


@dataclass
class Opt:
    name: str
    ccy: str
    expiry: float
    strike: float
    cp: str
    amount: float
    iv: float


@dataclass
class State:
    now_s: float
    spot: dict[str, float]
    fwd: dict[tuple[str, float], float]
    df: dict[tuple[str, float], float]
    perps: dict[str, list[Any]]  # name -> [ccy, amount, mark]
    opts: list[Opt]
    coll: dict[str, list[float]]  # asset -> [amount, price]
    mtm: float
    rc_ccys: set[str] = field(default_factory=set)  # currencies whose RC collateral is shocked in-bucket

    def tau(self, expiry: float) -> float:
        return max((expiry - self.now_s) / 86400.0 / YEAR, 0.0)

    def opt_value(self, o: Opt, fwd_mult: float = 1.0, iv: Optional[float] = None) -> float:
        key = (o.ccy, o.expiry)
        return black76(self.fwd[key] * fwd_mult, o.strike, self.tau(o.expiry), o.iv if iv is None else iv,
                       self.df[key], o.cp)

    def rc_asset_ccy(self, asset: str) -> Optional[str]:
        for ccy, table in RC_HAIRCUT.items():
            if asset in table and ccy in self.rc_ccys:
                return ccy
        return None


# --------------------------------------------------------------------------- #
# Loading and what-ifs
# --------------------------------------------------------------------------- #
class Loader:
    def __init__(self, client: DeriveClient):
        self.c = client
        self.tickers: dict[str, Any] = {}

    def ticker(self, name: str) -> dict[str, Any]:
        if name not in self.tickers:
            self.tickers[name] = self.c.post("public/get_ticker", {"instrument_name": name}, auth=False)
        return self.tickers[name]

    def add_option(self, st: State, name: str, qty: float) -> None:
        ccy, exp, k, cp = parse_option_name(name)
        pr = self.ticker(name).get("option_pricing") or {}
        st.fwd.setdefault((ccy, exp), _f(pr.get("f")))
        st.df.setdefault((ccy, exp), _f(pr.get("df"), 1.0) or 1.0)
        for o in st.opts:
            if o.name == name:
                o.amount += qty
                break
        else:
            st.opts.append(Opt(name, ccy, exp, k, cp, qty, _f(pr.get("i"))))
        st.opts = [o for o in st.opts if abs(o.amount) > 1e-12]


def load_state(ld: Loader, watch: dict[str, Any], now_s: float) -> State:
    st = State(now_s=now_s, spot={}, fwd={}, df={}, perps={}, opts=[], coll={},
               mtm=_f(watch.get("subaccount_value")))
    for p in watch.get("positions") or []:
        name = p["instrument_name"]
        ccy = name.split("-")[0]
        st.spot.setdefault(ccy, _f(p.get("index_price")))
        st.rc_ccys.add(ccy)
        if p.get("instrument_type") == "perp":
            st.perps[name] = [ccy, _f(p["amount"]), _f(p["mark_price"])]
        else:
            ld.add_option(st, name, _f(p["amount"]))
    for c in watch.get("collaterals") or []:
        st.coll[c["asset_name"].upper()] = [_f(c["amount"]), _f(c["mark_price"])]
    # RC tokens only cancel risk in the universe hosting their currency's derivatives (Prime for
    # ETH/BTC, Alt for SOL); XAUT is plain collateral in the RWA universe.
    return st


def apply_trade(ld: Loader, st: State, name: str, qty: float, fill: str) -> float:
    """Trade at mark (or touch); returns cash paid. Perp P&L stays in MtM unchanged at mark."""
    tk = ld.ticker(name)
    px = _f(tk.get("M"))
    if fill == "touch":
        px = (_f(tk.get("a")) if qty > 0 else _f(tk.get("b"))) or px
    if name.endswith("-PERP"):
        ccy = name.split("-")[0]
        cur = st.perps.get(name, [ccy, 0.0, px])
        cur[1] += qty
        st.perps[name] = cur
        cost = abs(qty) * abs(px - _f(tk.get("M")))  # only slippage vs mark changes MtM
    else:
        ld.add_option(st, name, qty)
        cost = qty * px
        st.mtm += qty * _f(tk.get("M")) - cost  # MtM moves by (mark - fill) * qty
        st.coll.setdefault("USDC", [0.0, 1.0])[0] -= cost
        return cost
    st.mtm -= cost
    st.coll.setdefault("USDC", [0.0, 1.0])[0] -= cost
    return cost


def settle_expiry(st: State, ymd: str) -> float:
    """Settle all options of an expiry at today's forward (intrinsic); returns cash received."""
    cash = 0.0
    keep = []
    for o in st.opts:
        if o.name.split("-")[1] == ymd:
            f = st.fwd[(o.ccy, o.expiry)]
            intrinsic = max(f - o.strike, 0.0) if o.cp == "C" else max(o.strike - f, 0.0)
            value_now = o.amount * st.opt_value(o)
            cash += o.amount * intrinsic
            st.mtm += o.amount * intrinsic - value_now
        else:
            keep.append(o)
    st.opts = keep
    st.coll.setdefault("USDC", [0.0, 1.0])[0] += cash
    return cash


# --------------------------------------------------------------------------- #
# PM2 maintenance margin
# --------------------------------------------------------------------------- #
def _vol_mult(kind: str, p: dict[str, Any], tau: float) -> float:
    if kind == "S":
        return 1.0
    ratio = (30 / YEAR) / max(1 / YEAR, tau)
    power = 0.3 if tau <= 30 / YEAR else 0.13
    return 1 + p["up"] * ratio ** power if kind == "U" else 1 - p["down"] * ratio ** power


def _discount(v: float, rate: float, tau: float) -> float:
    r = max(rate, 0.0)
    if v > 0:
        return 0.98 * math.exp(-0.10 * tau)
    return min(math.exp(r * tau), 1.02 * math.exp(0.10 * tau))


def mm_breakdown(st: State) -> dict[str, Any]:
    out: dict[str, Any] = {"mtm": st.mtm, "ccy": {}}
    total = st.mtm
    ccys = {o.ccy for o in st.opts} | {v[0] for v in st.perps.values()}
    for ccy in sorted(ccys):
        p = PARAMS.get(ccy)
        if p is None:
            continue
        s0 = st.spot[ccy]
        perps = [v for v in st.perps.values() if v[0] == ccy]
        rc = [(a, amt * px) for a, (amt, px) in st.coll.items() if st.rc_asset_ccy(a) == ccy]
        exps: dict[float, list[Opt]] = {}
        for o in st.opts:
            if o.ccy == ccy:
                exps.setdefault(o.expiry, []).append(o)
        ev = {e: sum(o.amount * st.opt_value(o) for o in os) for e, os in exps.items()}
        rate = {e: -math.log(st.df[(ccy, e)]) / max(st.tau(e), 1e-9) for e in exps}

        def scen(shock: float, kind: str) -> float:
            loss = sum(v[1] * v[2] * shock for v in perps) + sum(val * shock for _, val in rc)
            for e, os in exps.items():
                tau = st.tau(e)
                vm = _vol_mult(kind, p, tau)
                sev = sum(o.amount * st.opt_value(o, 1 + shock, max(o.iv * vm, p["minvol"]) if kind != "S" else o.iv)
                          for o in os)
                loss += _discount(sev, rate[e], tau) * sev - ev[e]
            return loss

        regular = min(scen(n * p["step"], k) for n, k in GRID)
        tail = min(d * scen(s, "U") for s, d in p["tails"])
        skew = 0.0
        fwd_loss = 0.0
        for e, os in exps.items():
            tau = st.tau(e)
            cap = 0.25 - 0.1 * math.sqrt(tau)
            kstar = max(0.01, 4 * math.sqrt(tau) * 0.6)
            f = st.fwd[(ccy, e)]
            worst = 0.0
            for rail, d in (("lin", 1), ("lin", -1), ("abs", 1), ("abs", -1)):
                sev = 0.0
                for o in os:
                    k = math.log(o.strike / f)
                    base = (max(min(cap * k / kstar, cap), -cap) if rail == "lin" else min(cap * abs(k) / kstar, cap))
                    sev += o.amount * st.opt_value(o, 1.0, o.iv * (1 + d * base))
                worst = min(worst, _discount(sev, rate[e], tau) * sev - ev[e])
            skew += worst
            # Forward loss is charged on undiscounted expiry values: this reproduces Derive's live MM
            # to within ~$75 on the books tested, while applying the static discount here does not.
            ups = sum(o.amount * st.opt_value(o, 1 + p["fwd"]) for o in os)
            dns = sum(o.amount * st.opt_value(o, 1 - p["fwd"]) for o in os)
            basis = min(0.0, ups - ev[e], dns - ev[e])
            fwd_loss += (p["add"] + MULT_FACTOR * tau) * basis
        max_loss = min(regular, tail, skew, fwd_loss)

        perp_cont = -sum(abs(v[1]) for v in perps) * p["perp"] * s0
        net_by_strike: dict[tuple[float, float], float] = {}
        for o in st.opts:
            if o.ccy == ccy:
                net_by_strike[(o.expiry, o.strike)] = net_by_strike.get((o.expiry, o.strike), 0.0) + o.amount
        opt_cont = sum(min(0.0, n) for n in net_by_strike.values()) * p["opt"] * s0
        rc_cont = -sum(max(val, 0.0) * RC_HAIRCUT[ccy][a] for a, val in rc) if ccy in RC_HAIRCUT else 0.0
        bucket = MM_FACTOR * max_loss + perp_cont + opt_cont + rc_cont
        out["ccy"][ccy] = dict(regular=regular, tail=tail, skew=skew, forward=fwd_loss, max_loss=max_loss,
                               perp_cont=perp_cont, opt_cont=opt_cont, rc_cont=rc_cont, bucket=bucket)
        total += bucket
    plain = 0.0
    for a, (amt, px) in st.coll.items():
        if st.rc_asset_ccy(a) is None and amt > 0:
            plain -= amt * px * PLAIN_HAIRCUT.get(a, 0.0)
    out["plain_cont"] = plain
    out["mm"] = total + plain
    return out


def shifted(st: State, moves: dict[str, float]) -> State:
    """Instant spot move per currency; forwards scale with spot, IVs sticky by strike."""
    new = copy.deepcopy(st)
    d_mtm = 0.0
    for ccy, x in moves.items():
        if ccy not in new.spot:
            continue
        new.spot[ccy] *= 1 + x
        for key in list(new.fwd):
            if key[0] == ccy:
                new.fwd[key] *= 1 + x
        for v in new.perps.values():
            if v[0] == ccy:
                d_mtm += v[1] * v[2] * x
                v[2] *= 1 + x
        for a, c in new.coll.items():
            und = new.rc_asset_ccy(a) or PLAIN_UNDERLYING.get(a)
            if und == ccy:
                d_mtm += c[0] * c[1] * x
                c[1] *= 1 + x
    for o_old, o_new in zip(st.opts, new.opts):
        d_mtm += o_new.amount * (new.opt_value(o_new) - st.opt_value(o_old))
    new.mtm = st.mtm + d_mtm
    return new


def liquidation_move(st: State, ccys: list[str], direction: int, max_move: float) -> Optional[float]:
    """Smallest move (fraction) in `direction` at which MM < 0, or None within max_move."""
    if mm_breakdown(st)["mm"] < 0:
        return 0.0
    step, x, prev = 0.01, 0.0, 0.0
    while x < max_move:
        x = min(x + step, max_move)
        if mm_breakdown(shifted(st, {c: direction * x for c in ccys}))["mm"] < 0:
            lo, hi = prev, x
            for _ in range(20):
                mid = 0.5 * (lo + hi)
                if mm_breakdown(shifted(st, {c: direction * mid for c in ccys}))["mm"] < 0:
                    hi = mid
                else:
                    lo = mid
            return direction * hi
        prev = x
    return None


# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Estimate PM2 liquidation spot levels for a Derive subaccount.")
    ap.add_argument("--subaccount", type=int, required=True)
    ap.add_argument("--trade", nargs="*", default=[], metavar="NAME=QTY", help="What-if trades at mark.")
    ap.add_argument("--settle-expiry", nargs="*", default=[], metavar="YYYYMMDD",
                    help="Settle all legs of these expiries at today's forward first.")
    ap.add_argument("--fill", choices=["mark", "touch"], default="mark")
    ap.add_argument("--base-url", default=BASE_URL_DEFAULT)
    args = ap.parse_args(argv)

    import time
    client = DeriveClient(None, None, args.base_url)
    ld = Loader(client)
    watch = client.post("public/margin_watch", {"subaccount_id": args.subaccount}, auth=False)
    now_s = time.time()
    st = load_state(ld, watch, now_s)
    derive_mm = _f(watch.get("maintenance_margin"))
    model_now = mm_breakdown(st)["mm"]

    pf = Portfolio(watch)  # exact Derive engine for the what-if at today's prices
    for ymd in args.settle_expiry:
        cash = settle_expiry(st, ymd)
        for p in list(watch.get("positions") or []):
            if p["instrument_type"] == "option" and p["instrument_name"].split("-")[1] == ymd:
                pf.trade_option(p["instrument_name"], -_f(p["amount"]))
        pf.usdc(cash)
    for t in args.trade:
        name, qty = t.split("=", 1)
        q = float(qty)
        cost = apply_trade(ld, st, name, q, args.fill)
        if name.endswith("-PERP"):
            pf.perp_mark.setdefault(name, _f(ld.ticker(name).get("M")))
            pf.trade_perp(name, q)
            pf.usdc(-cost)
        else:
            pf.trade_option(name, q)
            pf.usdc(-cost)
    whatif = bool(args.trade or args.settle_expiry)
    engine_mm = None
    if whatif:
        req = pf.request(type("B", (), {"watch": watch})())
        r = client.post("public/get_margin", req, auth=False)
        engine_mm = _f(r.get("pre_maintenance_margin"))

    bd = mm_breakdown(st)
    print("=" * 84)
    print(f"Subaccount {args.subaccount} | spot " + ", ".join(f"{c} {s:,.2f}" for c, s in st.spot.items()))
    print(f"Derive MM now ${derive_mm:,.0f} | model MM now ${model_now:,.0f} "
          f"(model error {model_now - derive_mm:+,.0f})")
    if whatif:
        print(f"What-if: settle {args.settle_expiry or '-'}; trades {args.trade or '-'} (fill={args.fill})")
        print(f"  Derive engine MM after what-if ${engine_mm:,.0f} | model ${bd['mm']:,.0f}")
    for ccy, b in bd["ccy"].items():
        print(f"  {ccy}: maxLoss {b['max_loss']:,.0f} (regular {b['regular']:,.0f}, tail {b['tail']:,.0f}, "
              f"skew {b['skew']:,.0f}, fwd {b['forward']:,.0f}); contingencies "
              f"{b['perp_cont'] + b['opt_cont'] + b['rc_cont']:,.0f}")
    # Calibrate: carry the model error at today's spot as a constant offset.
    offset = (engine_mm if whatif else derive_mm) - bd["mm"]
    print(f"Offset to Derive applied to shocked results: {offset:+,.0f}")

    def mm_at(moves: dict[str, float]) -> float:
        return mm_breakdown(shifted(st, moves))["mm"] + offset

    ccys = [c for c in bd["ccy"]]
    print("\nMaintenance margin vs instant spot move (IV sticky by strike):")
    moves = [-0.40, -0.30, -0.25, -0.20, -0.15, -0.10, -0.05, 0.0, 0.05, 0.10, 0.20, 0.30]
    print("  move      " + "".join(f"{m * 100:>+8.0f}%" for m in moves))
    for label, cs in [(f"{'+'.join(ccys)} together", ccys)] + ([(c + " only", [c]) for c in ccys] if len(ccys) > 1 else []):
        print(f"  {label:<10}" + "".join(f"{mm_at({c: m for c in cs}):>+9,.0f}" for m in moves))

    print("\nLiquidation points (MM = 0):")
    st_cal = copy.deepcopy(st)  # fold the calibration offset into MtM
    st_cal.mtm += offset
    for label, cs in [("together", ccys)] + ([(c + " only", [c]) for c in ccys] if len(ccys) > 1 else []):
        for direction, name in ((-1, "down"), (1, "up")):
            x = liquidation_move(st_cal, cs, direction, 0.9 if direction < 0 else 3.0)
            if x is None:
                print(f"  {label:<10} {name:<5} none within {'-90%' if direction < 0 else '+300%'}")
            else:
                lv = ", ".join(f"{c} {st.spot[c] * (1 + x):,.0f}" for c in cs)
                print(f"  {label:<10} {name:<5} {x * 100:+.1f}%  ->  {lv}")
    print("=" * 84)
    print("Model notes: documented PM2 MM rebuilt and offset to Derive's own figure at today's spot; instant\n"
          "moves (no time decay or funding), forwards move 1:1 with spot, IVs unchanged per strike. A crash\n"
          "usually comes with higher vol, which helps long puts and hurts short puts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
