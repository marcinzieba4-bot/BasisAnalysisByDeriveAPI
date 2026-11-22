#!/usr/bin/env python3
"""Project the net P&L of a Derive basis (perp-funding carry) book over N days.

The book this was built for is a funding-carry trade:

  long  ETH : wstETH / ETH collateral + synthetic long forwards (long call + short put)
  short ETH : ETH-PERP, which receives hourly funding while funding is positive
  financing : a negative USDC balance, borrowed at the lending pool's borrow APY

Over a horizon the book earns or pays:

  + perp funding on the short perps          rate x |size| x spot x time
  + staking yield on LST collateral          Lido stETH APR on wstETH
  - USDC borrow interest                     on the (path-dependent) negative cash balance
  - forward roll-down of synthetic longs     option-implied forward decays to spot
  +/- option time value                      short OTM puts decay, long ITM calls' extrinsic decays
  - roll costs for legs expiring in-horizon  spread vs mark + exchange fees
  +/- residual delta / gamma if spot moves   scenario grid

Two ways of handling legs that expire inside the horizon are compared:

  roll   re-open the expiring synthetic delta at the first expiry after the horizon
  close  buy back the same amount of perp short instead (no more carry on that slice)

and Derive's read-only margin simulator (private/get_margin) is asked what each
choice does to maintenance margin once the next expiry settles. Nothing is
traded or changed.

Usage
-----
  python3 basis_projection.py --wallet 0x... --subaccount 61222 \\
      --key-env DERIVE_4842_API --days 60 [--exec mid|touch|mark] [--json-out out.json]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean
from typing import Any, Optional

import requests

from derive_portfolio import BASE_URL_DEFAULT, COLLATERAL_UNDERLYING, DeriveClient, _f, discover_key

YEAR_DAYS = 365.0
HOURS_PER_YEAR = 24 * YEAR_DAYS
DAY_MS = 86_400_000
OPTION_EXPIRY_HOUR_UTC = 8  # Derive options expire 08:00 UTC
LIDO_APR_URL = "https://eth-api.lido.fi/v1/protocol/steth/apr/sma"
STAKED_ASSETS = {"WSTETH", "STETH"}  # earn the Lido stETH APR
DEFAULT_STAKING_APR = 0.025


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black76(fwd: float, strike: float, tau: float, vol: float, df: float, cp: str) -> float:
    if tau <= 0 or vol <= 0 or fwd <= 0:
        intrinsic = max(fwd - strike, 0.0) if cp == "C" else max(strike - fwd, 0.0)
        return df * intrinsic
    sd = vol * math.sqrt(tau)
    d1 = (math.log(fwd / strike) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if cp == "C":
        return df * (fwd * _ncdf(d1) - strike * _ncdf(d2))
    return df * (strike * _ncdf(-d2) - fwd * _ncdf(-d1))


@dataclass
class Curve:
    """Constant annualised carry for one (currency, expiry), from Derive option pricing."""

    basis: float  # ln(F / S) / tau
    rate: float  # -ln(df) / tau


@dataclass
class OptionLeg:
    name: str
    ccy: str
    expiry: float  # unix seconds
    strike: float
    cp: str
    amount: float
    vol: float
    mark: float
    delta: float  # per contract
    bid: float = 0.0
    ask: float = 0.0

    @property
    def expiry_key(self) -> tuple[str, float]:
        return self.ccy, self.expiry

    def tau_years(self, at_s: float) -> float:
        return (self.expiry - at_s) / 86400.0 / YEAR_DAYS

    def value(self, spot: float, at_s: float, curve: Curve) -> float:
        tau = self.tau_years(at_s)
        if tau <= 0:
            return max(spot - self.strike, 0.0) if self.cp == "C" else max(self.strike - spot, 0.0)
        fwd = spot * math.exp(curve.basis * tau)
        df = math.exp(-curve.rate * tau)
        return black76(fwd, self.strike, tau, self.vol, df, self.cp)

    def intrinsic(self, spot: float) -> float:
        return max(spot - self.strike, 0.0) if self.cp == "C" else max(self.strike - spot, 0.0)


def strike_str(k: float) -> str:
    return str(int(k)) if k == int(k) else f"{k:g}"


def parse_option_name(name: str) -> tuple[str, float, float, str]:
    ccy, ymd, strike, cp = name.split("-")
    exp = datetime.strptime(ymd, "%Y%m%d").replace(hour=OPTION_EXPIRY_HOUR_UTC, tzinfo=timezone.utc)
    return ccy, exp.timestamp(), float(strike), cp


def curve_from_pricing(spot: float, expiry: float, now_s: float, pricing: dict[str, Any]) -> Curve:
    tau = max((expiry - now_s) / 86400.0 / YEAR_DAYS, 1e-6)
    fwd = _f(pricing.get("f"), spot) or spot
    df = _f(pricing.get("df"), 1.0) or 1.0
    return Curve(basis=math.log(fwd / spot) / tau, rate=-math.log(df) / tau)


def exec_price(bid: float, ask: float, mark: float, side: int, mode: str) -> float:
    """Price paid (side=+1 buy) or received (side=-1 sell) under an execution assumption."""
    if mode == "mark":
        return mark
    if mode == "mid" and bid > 0 and ask > 0:
        return 0.5 * (bid + ask)
    if mode == "touch":
        px = ask if side > 0 else bid
        if px > 0:
            return px
    return mark


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
@dataclass
class FeeSchedule:
    perp_rate: float
    perp_base: float
    option_rate: float
    option_base: float
    option_cap: float  # fee capped at this fraction of option premium

    def option_fee(self, index: float, price: float, qty: float) -> float:
        fee = self.option_rate * index * abs(qty)
        if self.option_cap > 0:
            fee = min(fee, self.option_cap * price * abs(qty))
        return fee + self.option_base

    def perp_fee(self, index: float, qty: float) -> float:
        return self.perp_rate * index * abs(qty) + self.perp_base


@dataclass
class RollTarget:
    ccy: str
    expiry: float
    expiry_label: str
    strike: float
    curve: Curve
    call: dict[str, Any]
    put: dict[str, Any]
    amount_step: float = 0.01


@dataclass
class Book:
    now_s: float
    sub: dict[str, Any]
    spot: dict[str, float]
    perps: dict[str, float]  # name -> amount
    perp_ccy: dict[str, str]
    perp_spread: dict[str, float]
    options: list[OptionLeg]
    curves: dict[tuple[str, float], Curve]
    cash0: float  # USDC + unsettled perp value
    collateral_units: dict[str, tuple[str, float, bool]]  # asset -> (ccy, underlying units, staked?)
    funding_scen: dict[str, dict[str, float]] = field(default_factory=dict)  # scen -> perp -> APR
    borrow_apy: float = 0.0
    supply_apy: float = 0.0
    borrow_apy_30d: float = 0.0
    staking_apr: float = DEFAULT_STAKING_APR
    staking_src: str = ""
    fees: Optional[FeeSchedule] = None
    roll_targets: dict[str, RollTarget] = field(default_factory=dict)


def fetch_lido_apr() -> tuple[float, str]:
    try:
        r = requests.get(LIDO_APR_URL, timeout=20)
        r.raise_for_status()
        return float(r.json()["data"]["smaApr"]) / 100.0, "Lido 7d SMA"
    except Exception as e:  # noqa: BLE001 - any failure falls back to a default
        return DEFAULT_STAKING_APR, f"default (Lido fetch failed: {type(e).__name__})"


def load_book(client: DeriveClient, sub_id: int, days: float, roll_buffer_days: float,
              staking_apr: Optional[float], extra_funding: list[float]) -> Book:
    now_s = time.time()
    now_ms = int(now_s * 1000)
    sub = client.post("private/get_subaccount", {"subaccount_id": sub_id})
    positions = sub.get("positions") or []

    tickers: dict[str, Any] = {}
    for p in positions:
        name = p["instrument_name"]
        tickers[name] = client.post("public/get_ticker", {"instrument_name": name}, auth=False)

    spot: dict[str, float] = {}
    for p in positions:
        ccy = p["instrument_name"].split("-")[0]
        spot.setdefault(ccy, _f(p.get("index_price")))

    perps: dict[str, float] = {}
    perp_ccy: dict[str, str] = {}
    perp_spread: dict[str, float] = {}
    options: list[OptionLeg] = []
    perp_unsettled = 0.0
    for p in positions:
        name = p["instrument_name"]
        tk = tickers.get(name, {})
        if p.get("instrument_type") == "perp":
            perps[name] = _f(p["amount"])
            perp_ccy[name] = name.split("-")[0]
            perp_spread[name] = max(_f(tk.get("a")) - _f(tk.get("b")), 0.0)
            perp_unsettled += _f(p.get("mark_value"))
            spot[perp_ccy[name]] = _f(tk.get("I")) or spot.get(perp_ccy[name], 0.0)
        elif p.get("instrument_type") == "option":
            ccy, exp, k, cp = parse_option_name(name)
            pr = tk.get("option_pricing") or {}
            options.append(OptionLeg(
                name=name, ccy=ccy, expiry=exp, strike=k, cp=cp, amount=_f(p["amount"]),
                vol=_f(pr.get("i")), mark=_f(p.get("mark_price")), delta=_f(p.get("delta")),
                bid=_f(tk.get("b")), ask=_f(tk.get("a")),
            ))

    curves: dict[tuple[str, float], Curve] = {}
    for leg in options:
        if leg.expiry_key not in curves:
            pr = (tickers[leg.name].get("option_pricing") or {})
            curves[leg.expiry_key] = curve_from_pricing(spot[leg.ccy], leg.expiry, now_s, pr)

    cash0 = perp_unsettled
    collateral_units: dict[str, tuple[str, float, bool]] = {}
    for c in sub.get("collaterals") or []:
        asset = str(c.get("asset_name", "")).upper()
        if asset == "USDC":
            cash0 += _f(c.get("amount"))
            continue
        ccy = COLLATERAL_UNDERLYING.get(asset)
        if ccy and spot.get(ccy):
            collateral_units[asset] = (ccy, _f(c.get("mark_value")) / spot[ccy], asset in STAKED_ASSETS)
        else:
            print(f"[warn] collateral {asset} not modelled (held at constant USD value)", file=sys.stderr)
            collateral_units[asset] = ("USD", _f(c.get("mark_value")), False)
    spot.setdefault("USD", 1.0)

    book = Book(now_s=now_s, sub=sub, spot=spot, perps=perps, perp_ccy=perp_ccy, perp_spread=perp_spread,
                options=options, curves=curves, cash0=cash0, collateral_units=collateral_units)

    # Funding scenarios per perp, from realised hourly history.
    hist: dict[str, list[tuple[int, float]]] = {}
    for name in perps:
        h = client.post("public/get_funding_rate_history", {
            "instrument_name": name, "start_timestamp": now_ms - 30 * DAY_MS + 60_000,
            "end_timestamp": now_ms, "period": 3600}, auth=False)
        hist[name] = [(int(x["timestamp"]), _f(x["funding_rate"])) for x in h.get("funding_rate_history", [])]

    def avg(name: str, d: int) -> float:
        xs = [r for t, r in hist[name] if t >= now_ms - d * DAY_MS]
        return mean(xs) * HOURS_PER_YEAR if xs else 0.0

    book.funding_scen = {
        "30d realised avg": {n: avg(n, 30) for n in perps},
        "7d realised avg": {n: avg(n, 7) for n in perps},
        "current rate": {n: _f(tickers[n].get("f")) * HOURS_PER_YEAR for n in perps},
    }
    for apr in extra_funding:
        book.funding_scen[f"stress {apr * 100:g}% APR"] = {n: apr for n in perps}

    # USDC lending rates for this subaccount's risk universe.
    ru = sub.get("risk_universe_id")
    ir = client.post("public/get_interest_rate_history", {
        "currency": "USDC", "start_timestamp": now_ms - 30 * DAY_MS, "end_timestamp": now_ms,
        "period": 86400}, auth=False).get("interest_rate_history", [])
    ir = [x for x in ir if ru is None or x.get("risk_universe_id") == ru]
    if ir:
        book.borrow_apy = _f(ir[-1]["borrow_apy"]["close"])
        book.supply_apy = _f(ir[-1]["supply_apy"]["close"])
        book.borrow_apy_30d = mean(_f(x["borrow_apy"]["close"]) for x in ir)

    if staking_apr is None:
        book.staking_apr, book.staking_src = fetch_lido_apr()
    else:
        book.staking_apr, book.staking_src = staking_apr, "--staking-apr"

    # Fees: instrument schedule, overridden by account-level fee_info if set.
    perp_name = next(iter(perps), None)
    opt_name = options[0].name if options else None
    pi = client.post("public/get_instrument", {"instrument_name": perp_name}, auth=False) if perp_name else {}
    oi = client.post("public/get_instrument", {"instrument_name": opt_name}, auth=False) if opt_name else {}
    try:
        fi = client.post("private/get_account", {"wallet": client.wallet}).get("fee_info") or {}
    except SystemExit:
        fi = {}
    base_disc = _f(fi.get("base_fee_discount"))
    book.fees = FeeSchedule(
        perp_rate=_f(fi.get("perp_taker_fee")) or _f(pi.get("taker_fee_rate"), 0.0003),
        perp_base=_f(pi.get("base_fee"), 0.1) * (1 - base_disc),
        option_rate=_f(fi.get("option_taker_fee")) or _f(oi.get("taker_fee_rate"), 0.0003),
        option_base=_f(oi.get("base_fee"), 0.5) * (1 - base_disc),
        option_cap=_f(oi.get("mark_price_fee_rate_cap"), 0.125),
    )

    # Roll targets: first live expiry per currency beyond horizon + buffer.
    horizon_s = now_s + days * 86400
    expiring_ccys = {leg.ccy for leg in options if leg.expiry <= horizon_s}
    if expiring_ccys:
        live = client.post("public/get_all_live_instruments", {}, auth=False)
        live = live if isinstance(live, list) else live.get("instruments", [])
        for ccy in sorted(expiring_ccys):
            exps = sorted({n.split("-")[1] for n in live if isinstance(n, str)
                           and n.startswith(f"{ccy}-2") and n.count("-") == 3})
            cands = [e for e in exps if parse_option_name(f"{ccy}-{e}-1-C")[1] > horizon_s + roll_buffer_days * 86400]
            if not cands:
                print(f"[warn] no {ccy} expiry beyond horizon; cannot model a roll", file=sys.stderr)
                continue
            e = cands[0]
            exp_s = parse_option_name(f"{ccy}-{e}-1-C")[1]
            chain = client.post("public/get_tickers", {"instrument_type": "option", "currency": ccy,
                                                       "expiry_date": e}, auth=False).get("tickers", {})
            any_pr = next((t.get("option_pricing") for t in chain.values() if t.get("option_pricing")), {})
            curve = curve_from_pricing(spot[ccy], exp_s, now_s, any_pr)
            fwd = spot[ccy] * math.exp(curve.basis * ((exp_s - now_s) / 86400 / YEAR_DAYS))
            strikes = sorted({float(n.split("-")[2]) for n in chain
                              if f"{ccy}-{e}-{n.split('-')[2]}-C" in chain and f"{ccy}-{e}-{n.split('-')[2]}-P" in chain})
            k = min(strikes, key=lambda s: abs(s - fwd))
            ks = strike_str(k)
            c, pt = chain[f"{ccy}-{e}-{ks}-C"], chain[f"{ccy}-{e}-{ks}-P"]
            inst = client.post("public/get_instrument", {"instrument_name": f"{ccy}-{e}-{ks}-C"}, auth=False)
            book.roll_targets[ccy] = RollTarget(ccy=ccy, expiry=exp_s, expiry_label=e, strike=k, curve=curve,
                                                call=c, put=pt, amount_step=_f(inst.get("amount_step"), 0.01) or 0.01)
    return book


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    funding: float = 0.0
    staking: float = 0.0
    interest: float = 0.0
    fees: float = 0.0
    spread: float = 0.0
    perp_price_pnl: float = 0.0
    collateral_price_pnl: float = 0.0
    options: float = 0.0  # carry/theta/roll-down + delta/gamma of option legs
    total: float = 0.0
    min_cash: float = 0.0
    actions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def round_step(x: float, step: float) -> float:
    return round(round(x / step) * step, 10) if step > 0 else x


def simulate(book: Book, days: float, funding: dict[str, float], shock: float, plan: str,
             exec_mode: str) -> Result:
    """Hourly path from now to horizon with spot moving linearly to spot*(1+shock)."""
    res = Result()
    fees = book.fees
    assert fees is not None
    now_s = book.now_s
    steps = int(round(days * 24))
    dt_h = days * 24 / steps

    def spot_at(ccy: str, frac: float) -> float:
        if ccy == "USD":
            return 1.0
        return book.spot[ccy] * (1 + shock * frac)

    cash = book.cash0
    perps = dict(book.perps)
    legs = [OptionLeg(**leg.__dict__) for leg in book.options]
    leg_curves = {id(leg): book.curves[leg.expiry_key] for leg in legs}
    units = {a: u for a, (_, u, _) in book.collateral_units.items()}

    value0 = sum(leg.amount * leg.value(book.spot[leg.ccy], now_s, leg_curves[id(leg)]) for leg in legs)
    premiums_paid = 0.0  # cash spent opening roll legs (at execution price)
    settled = 0.0  # cash received from option expiries
    res.min_cash = cash

    # Delta to replace per expiring (ccy, expiry) group, at today's deltas.
    groups: dict[tuple[str, float], float] = {}
    for leg in legs:
        if leg.expiry <= now_s + days * 86400:
            groups[leg.expiry_key] = groups.get(leg.expiry_key, 0.0) + leg.amount * leg.delta
    handled: set[tuple[str, float]] = set()

    prev_spot = {c: book.spot[c] for c in book.spot}
    for i in range(1, steps + 1):
        frac = i / steps
        t_s = now_s + i * dt_h * 3600
        s_now = {c: spot_at(c, frac) for c in book.spot}

        # Perp mark-to-market (settles into USDC) and funding.
        for name, amt in perps.items():
            ccy = book.perp_ccy[name]
            pnl = amt * (s_now[ccy] - prev_spot[ccy])
            fund = -amt * s_now[ccy] * funding.get(name, 0.0) * dt_h / HOURS_PER_YEAR
            cash += pnl + fund
            res.perp_price_pnl += pnl
            res.funding += fund

        # Staking accrues as extra underlying units.
        for asset, (ccy, _, staked) in book.collateral_units.items():
            res.collateral_price_pnl += units[asset] * (s_now[ccy] - prev_spot[ccy])
            if staked:
                gain_units = units[asset] * book.staking_apr * dt_h / HOURS_PER_YEAR
                units[asset] += gain_units
                res.staking += gain_units * s_now[ccy]

        # Interest on the USDC balance (APY -> continuous rate).
        rate = math.log1p(book.borrow_apy) if cash < 0 else math.log1p(book.supply_apy)
        intr = cash * (math.exp(rate * dt_h / HOURS_PER_YEAR) - 1.0)
        cash += intr
        res.interest += intr

        # Expiries.
        for leg in [lg for lg in legs if lg.expiry <= t_s]:
            payoff = leg.amount * leg.intrinsic(s_now[leg.ccy])
            cash += payoff
            settled += payoff
            legs.remove(leg)
            key = leg.expiry_key
            if key in groups and key not in handled:
                handled.add(key)
                qty = groups[key]
                cash, premium = _handle_expiry(book, plan, key, qty, s_now, t_s, legs, leg_curves, perps,
                                               fees, exec_mode, res, cash)
                premiums_paid += premium

        res.min_cash = min(res.min_cash, cash)
        prev_spot = s_now

    end_s = now_s + days * 86400
    s_end = {c: spot_at(c, 1.0) for c in book.spot}
    value_end = sum(leg.amount * leg.value(s_end[leg.ccy], end_s, leg_curves[id(leg)]) for leg in legs)
    # Option P&L at model prices; execution slippage is reported separately in `spread`.
    res.options = value_end + settled - value0 - premiums_paid
    res.total = (res.funding + res.staking + res.interest - res.fees - res.spread + res.perp_price_pnl
                 + res.collateral_price_pnl + res.options)
    return res


def _handle_expiry(book: Book, plan: str, key: tuple[str, float], qty: float, s_now: dict[str, float],
                   t_s: float, legs: list[OptionLeg], leg_curves: dict[int, Curve], perps: dict[str, float],
                   fees: FeeSchedule, exec_mode: str, res: Result, cash: float) -> tuple[float, float]:
    """Apply the plan for an expiring synthetic. Returns (cash, roll premium paid at model)."""
    ccy, exp = key
    label = datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%d")
    spot = s_now[ccy]
    if plan == "roll" and ccy in book.roll_targets:
        tgt = book.roll_targets[ccy]
        q = round_step(abs(qty), tgt.amount_step)
        side = 1 if qty > 0 else -1  # long synthetic = buy call, sell put
        ks = strike_str(tgt.strike)
        premium = 0.0
        for cp, tk, leg_side in (("C", tgt.call, side), ("P", tgt.put, -side)):
            pr = tk.get("option_pricing") or {}
            leg = OptionLeg(name=f"{ccy}-{tgt.expiry_label}-{ks}-{cp}", ccy=ccy, expiry=tgt.expiry,
                            strike=tgt.strike, cp=cp, amount=leg_side * q, vol=_f(pr.get("i")),
                            mark=_f(tk.get("M")), delta=_f(pr.get("d")), bid=_f(tk.get("b")), ask=_f(tk.get("a")))
            model_px = leg.value(spot, t_s, tgt.curve)
            px_now = exec_price(leg.bid, leg.ask, leg.mark, leg_side, exec_mode)
            slip = (px_now - leg.mark) * leg_side  # >0 = worse than mark
            fee = fees.option_fee(spot, model_px, q)
            res.spread += slip * q
            res.fees += fee
            premium += leg_side * q * model_px
            cash -= leg_side * q * model_px + slip * q + fee
            legs.append(leg)
            leg_curves[id(leg)] = tgt.curve
        res.actions.append(f"{label}: roll {side * q:+g} {ccy} synthetic into {tgt.expiry_label} K={ks}")
        return cash, premium

    # plan == "close" (or no roll target): buy back perp short of the same size.
    perp = next((n for n, c in book.perp_ccy.items() if c == ccy), None)
    if perp is None:
        res.actions.append(f"{label}: {ccy} synthetic expires, no perp to adjust")
        return cash, 0.0
    amt = perps[perp]
    adj = -math.copysign(min(abs(qty), abs(amt)), amt) if amt * qty < 0 else 0.0
    if adj:
        perps[perp] = amt + adj
        fee = fees.perp_fee(spot, adj)
        slip = 0.0 if exec_mode in ("mid", "mark") else 0.5 * book.perp_spread.get(perp, 0.0) * abs(adj)
        res.fees += fee
        res.spread += slip
        cash -= fee + slip
        res.actions.append(f"{label}: {ccy} synthetic expires; close {adj:+g} {perp}")
    return cash, 0.0


# --------------------------------------------------------------------------- #
# Margin check (read-only simulator)
# --------------------------------------------------------------------------- #
def margin_checks(client: DeriveClient, book: Book, sub_id: int) -> list[dict[str, Any]]:
    if not book.options:
        return []
    next_exp = min(leg.expiry for leg in book.options)
    expiring = [leg for leg in book.options if leg.expiry == next_exp]
    settle_cash = 0.0
    close_legs, roll_legs, perp_adj = [], [], []
    roll_cash = 0.0
    by_ccy: dict[str, float] = {}
    for leg in expiring:
        curve = book.curves[leg.expiry_key]
        fwd = book.spot[leg.ccy] * math.exp(curve.basis * max(leg.tau_years(book.now_s), 0))
        settle_cash += leg.amount * leg.intrinsic(fwd)
        close_legs.append({"instrument_name": leg.name, "amount": str(-leg.amount)})
        by_ccy[leg.ccy] = by_ccy.get(leg.ccy, 0.0) + leg.amount * leg.delta
    for ccy, qty in by_ccy.items():
        tgt = book.roll_targets.get(ccy)
        if tgt:
            q = round_step(abs(qty), tgt.amount_step)
            side = 1 if qty > 0 else -1
            ks = strike_str(tgt.strike)
            roll_legs += [{"instrument_name": f"{ccy}-{tgt.expiry_label}-{ks}-C", "amount": str(side * q)},
                          {"instrument_name": f"{ccy}-{tgt.expiry_label}-{ks}-P", "amount": str(-side * q)}]
            roll_cash -= side * q * (_f(tgt.call.get("M")) - _f(tgt.put.get("M")))
        perp = next((n for n, c in book.perp_ccy.items() if c == ccy), None)
        if perp and book.perps[perp] * qty < 0:
            perp_adj.append({"instrument_name": perp,
                             "amount": str(-math.copysign(min(abs(qty), abs(book.perps[perp])), book.perps[perp]))})

    usdc = lambda x: [{"asset_name": "USDC", "amount": str(round(x, 6))}]  # noqa: E731
    cases = [
        ("after next expiry, no action", close_legs, usdc(settle_cash)),
        ("after next expiry + roll", close_legs + roll_legs, usdc(settle_cash + roll_cash)),
        ("after next expiry + close perp", close_legs + perp_adj, usdc(settle_cash)),
        ("now: open roll before expiry", roll_legs, usdc(roll_cash)),
    ]
    out = []
    for label, pos, coll in cases:
        if not pos:
            continue
        try:
            r = client.post("private/get_margin", {"subaccount_id": sub_id, "simulated_position_changes": pos,
                                                   "simulated_collateral_changes": coll})
            out.append({"case": label, "post_maintenance_margin": _f(r.get("post_maintenance_margin")),
                        "post_initial_margin": _f(r.get("post_initial_margin")),
                        "pre_maintenance_margin": _f(r.get("pre_maintenance_margin")),
                        "is_valid_trade": r.get("is_valid_trade")})
        except SystemExit as e:
            out.append({"case": label, "error": str(e)[:200]})
    return out


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def fmt(x: float) -> str:
    return f"{x:>+10,.0f}"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Project net P&L of a Derive basis/funding-carry book.")
    ap.add_argument("--wallet", required=True, help="Owner wallet address.")
    ap.add_argument("--subaccount", type=int, required=True)
    ap.add_argument("--key-env", help="Env var holding the session private key.")
    ap.add_argument("--scan-env", action="store_true")
    ap.add_argument("--base-url", default=BASE_URL_DEFAULT)
    ap.add_argument("--days", type=float, default=60.0, help="Horizon in days (default 60).")
    ap.add_argument("--exec", dest="exec_mode", choices=["mark", "mid", "touch"], default="mid",
                    help="Execution price for rolls: mark, mid (default) or touch (cross the book).")
    ap.add_argument("--staking-apr", type=float, help="Override stETH APR (decimal, e.g. 0.025).")
    ap.add_argument("--stress-funding", type=float, nargs="*", default=[0.03, 0.0],
                    help="Extra flat funding APR scenarios (decimals). Default: 0.03 0.0")
    ap.add_argument("--roll-buffer-days", type=float, default=3.0,
                    help="Roll into the first expiry at least this many days past the horizon.")
    ap.add_argument("--json-out")
    args = ap.parse_args(argv)

    pk, src_var, signer = discover_key(args.key_env, args.scan_env)
    print(f"[auth] session key from '{src_var}' (signer {signer})", file=sys.stderr)
    client = DeriveClient(pk, args.wallet, args.base_url)
    book = load_book(client, args.subaccount, args.days, args.roll_buffer_days, args.staking_apr,
                     args.stress_funding)
    sub = book.sub
    equity = _f(sub.get("subaccount_value"))
    mm, im = _f(sub.get("maintenance_margin")), _f(sub.get("initial_margin"))
    proj = _f(sub.get("projected_margin_change"))

    line = "=" * 78
    print(line)
    print(f"Derive basis projection | subaccount {args.subaccount} | horizon {args.days:g} days | "
          f"{datetime.fromtimestamp(book.now_s, timezone.utc):%Y-%m-%d %H:%M} UTC")
    print(line)
    print(f"Equity ${equity:,.0f}   maintenance-margin headroom ${mm:,.0f}   initial ${im:,.0f}")
    if book.options:
        nxt = min(leg.expiry for leg in book.options)
        print(f"Projected MM change at next expiry "
              f"({datetime.fromtimestamp(nxt, timezone.utc):%Y-%m-%d %H:%M} UTC): ${proj:,.0f}"
              f" -> MM after expiry ${mm + proj:,.0f}")
        if mm + proj < 0:
            print("!! LIQUIDATION WARNING: maintenance margin turns negative right after that expiry "
                  "unless the expiring hedge is rolled or the perp short is reduced first.")
    print("\nSpot: " + ", ".join(f"{c} {s:,.2f}" for c, s in book.spot.items() if c != "USD"))
    print(f"USDC borrow APY {book.borrow_apy * 100:.2f}% (30d avg {book.borrow_apy_30d * 100:.2f}%), "
          f"supply {book.supply_apy * 100:.2f}%   staking APR {book.staking_apr * 100:.2f}% ({book.staking_src})")
    print("Funding APR scenarios: " + "; ".join(
        f"{k}: " + ", ".join(f"{n} {v * 100:.2f}%" for n, v in d.items()) for k, d in book.funding_scen.items()))
    print("Option forward curves (annualised basis / discount rate):")
    for (ccy, exp), cv in sorted(book.curves.items()):
        print(f"  {ccy} {datetime.fromtimestamp(exp, timezone.utc):%Y-%m-%d}: basis {cv.basis * 100:+.2f}%  "
              f"rate {cv.rate * 100:.2f}%")
    for ccy, tgt in book.roll_targets.items():
        sp = (_f(tgt.call.get("a")) - _f(tgt.call.get("b"))) + (_f(tgt.put.get("a")) - _f(tgt.put.get("b")))
        print(f"  roll target {ccy} {tgt.expiry_label} K={tgt.strike:g}: basis {tgt.curve.basis * 100:+.2f}%  "
              f"rate {tgt.curve.rate * 100:.2f}%  C/P bid-ask total {sp:,.2f}")

    base = "30d realised avg"
    results: dict[str, Any] = {"scenarios": {}, "spot_grid": {}}
    print(f"\nNet P&L over {args.days:g} days at unchanged spot (exec={args.exec_mode}):")
    hdr = f"  {'plan':<6}{'funding scenario':<22}{'funding':>10}{'staking':>10}{'interest':>10}" \
          f"{'options':>10}{'fees+spr':>10}{'NET':>10}{'% equity':>10}"
    print(hdr)
    for plan in ("roll", "close"):
        for scen, rates in book.funding_scen.items():
            r = simulate(book, args.days, rates, 0.0, plan, args.exec_mode)
            results["scenarios"][f"{plan}|{scen}"] = r.as_dict()
            print(f"  {plan:<6}{scen:<22}{fmt(r.funding)}{fmt(r.staking)}{fmt(r.interest)}{fmt(r.options)}"
                  f"{fmt(-(r.fees + r.spread))}{fmt(r.total)}{r.total / equity * 100 if equity else 0:>9.1f}%")
    for plan in ("roll", "close"):
        acts = results["scenarios"][f"{plan}|{base}"]["actions"]
        print(f"  {plan} actions: " + ("; ".join(acts) if acts else "none"))

    shocks = [-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3]
    print(f"\nSpot sensitivity (funding = {base}); spot moves linearly to the shocked level:")
    print("  " + f"{'plan':<6}" + "".join(f"{s * 100:>+9.0f}%" for s in shocks))
    for plan in ("roll", "close"):
        row = []
        for s in shocks:
            r = simulate(book, args.days, book.funding_scen[base], s, plan, args.exec_mode)
            results["spot_grid"][f"{plan}|{s:+.2f}"] = r.as_dict()
            row.append(r.total)
        print("  " + f"{plan:<6}" + "".join(f"{v:>+10,.0f}" for v in row))

    checks = margin_checks(client, book, args.subaccount)
    if checks:
        print("\nMargin simulator (private/get_margin, read-only) around the next expiry:")
        for c in checks:
            if "error" in c:
                print(f"  {c['case']:<34} error: {c['error']}")
            else:
                flag = "LIQUIDATABLE" if c["post_maintenance_margin"] < 0 else "ok"
                print(f"  {c['case']:<34} MM {c['post_maintenance_margin']:>+10,.0f}  "
                      f"IM {c['post_initial_margin']:>+10,.0f}  {flag}")
    print(line)
    print("Model notes: options repriced with Black-76 at fixed IV, each expiry's option-implied carry held\n"
          "constant; perp mark ~ index; perp P&L settles into USDC; rolls at today's spread; funding paid on\n"
          "spot notional hourly. 'options' = forward roll-down + theta (+ delta/gamma when spot moves).")

    if args.json_out:
        results.update({
            "subaccount_id": args.subaccount, "horizon_days": args.days, "equity": equity,
            "maintenance_margin": mm, "initial_margin": im, "projected_margin_change": proj,
            "spot": book.spot, "funding_scenarios": book.funding_scen, "borrow_apy": book.borrow_apy,
            "staking_apr": book.staking_apr, "margin_checks": checks,
            "curves": {f"{c}|{e:.0f}": cv.__dict__ for (c, e), cv in book.curves.items()},
        })
        with open(args.json_out, "w") as fh:
            json.dump(results, fh, indent=2, default=str)
        print(f"[info] wrote {args.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
