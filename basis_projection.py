#!/usr/bin/env python3
"""Project the net P&L of Derive basis (perp-funding carry) books over N days.

A funding-carry book on Derive typically looks like:

  long  X : spot / LST collateral (wstETH, jitoSOL, XAUT, ...) and synthetic long
            forwards (long call + short put)
  short X : X-PERP, which receives hourly funding while funding is positive
  financing: a negative USDC balance, borrowed at the lending pool's borrow APY

Over a horizon the book earns or pays:

  + perp funding on the short perps          rate x |size| x spot x time
  + staking yield on LST collateral          Lido APR for wstETH, --asset-apr for others
  -/+ USDC interest                          on the path-dependent cash balance
  - forward roll-down of synthetic longs     option-implied forward decays to spot
  +/- option time value                      short OTM puts decay, long ITM extrinsic decays
  - roll costs for legs expiring in-horizon  spread vs mark + exchange fees
  +/- residual delta / gamma if spot moves   scenario grid

Two ways of handling legs that expire inside the horizon are compared:

  roll   re-open the expiring synthetic delta at the first expiry after the horizon
  close  buy back the same amount of perp short instead (no more carry on that slice)

Everything is read from PUBLIC endpoints (public/margin_watch for positions,
public/get_margin to simulate margin after the next expiry), so no session key
is needed and any subaccount can be projected. Nothing is traded or changed.

Usage
-----
  # every funded subaccount of a wallet, 60-day horizon
  python3 basis_projection.py --wallet 0x... --days 60

  # specific subaccounts
  python3 basis_projection.py --subaccount 65812 65813 --exec mid --json-out out.json
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
LIDO_ASSETS = {"WSTETH", "STETH"}  # earn the Lido stETH APR
DEFAULT_LIDO_APR = 0.025
# Assumed yields for other LST collateral; override with --asset-apr NAME=APR.
DEFAULT_ASSET_APR = {"JITOSOL": 0.07, "MSOL": 0.07, "JUPSOL": 0.07}


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
            return self.intrinsic(spot)
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


def round_step(x: float, step: float) -> float:
    return round(round(x / step) * step, 10) if step > 0 else x


def ts_label(ts: float, fmt: str = "%Y-%m-%d") -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime(fmt)


# --------------------------------------------------------------------------- #
# Shared market data (cached across subaccounts)
# --------------------------------------------------------------------------- #
@dataclass
class FeeSchedule:
    perp_rate: float
    perp_base: float
    option_rate: float
    option_base: float
    option_cap: float  # option fee capped at this fraction of premium

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

    def leg_name(self, cp: str) -> str:
        return f"{self.ccy}-{self.expiry_label}-{strike_str(self.strike)}-{cp}"


class Market:
    def __init__(self, client: DeriveClient, days: float, roll_buffer_days: float,
                 asset_apr: dict[str, float], lido_apr: Optional[float], extra_funding: list[float]):
        self.c = client
        self.now_s = time.time()
        self.now_ms = int(self.now_s * 1000)
        self.days = days
        self.roll_buffer_days = roll_buffer_days
        self.extra_funding = extra_funding
        self._tickers: dict[str, Any] = {}
        self._funding: dict[str, dict[str, float]] = {}
        self._rates: dict[Any, tuple[float, float, float]] = {}
        self._instruments: dict[str, Any] = {}
        self._spot: dict[str, float] = {}
        self._live: Optional[list[str]] = None
        self._rolls: dict[str, Optional[RollTarget]] = {}
        self._fees: Optional[FeeSchedule] = None
        if lido_apr is None:
            lido_apr, self.lido_src = self._fetch_lido()
        else:
            self.lido_src = "--lido-apr"
        self.asset_apr = {**DEFAULT_ASSET_APR, **{a: lido_apr for a in LIDO_ASSETS}, **asset_apr}

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self.c.post(path, body, auth=False)

    @staticmethod
    def _fetch_lido() -> tuple[float, str]:
        try:
            r = requests.get(LIDO_APR_URL, timeout=20)
            r.raise_for_status()
            return float(r.json()["data"]["smaApr"]) / 100.0, "Lido 7d SMA"
        except Exception as e:  # noqa: BLE001 - any failure falls back to a default
            return DEFAULT_LIDO_APR, f"default (Lido fetch failed: {type(e).__name__})"

    def ticker(self, name: str) -> dict[str, Any]:
        if name not in self._tickers:
            self._tickers[name] = self.post("public/get_ticker", {"instrument_name": name})
        return self._tickers[name]

    def instrument(self, name: str) -> dict[str, Any]:
        if name not in self._instruments:
            self._instruments[name] = self.post("public/get_instrument", {"instrument_name": name})
        return self._instruments[name]

    def spot(self, ccy: str) -> float:
        if ccy == "USD":
            return 1.0
        if ccy not in self._spot:
            px = 0.0
            try:
                px = _f(self.ticker(f"{ccy}-PERP").get("I"))
            except SystemExit:
                pass
            if not px:
                px = _f(self.post("public/get_currency", {"currency": ccy}).get("spot_price"))
            self._spot[ccy] = px
        return self._spot[ccy]

    def funding_scenarios(self, perp: str) -> dict[str, float]:
        if perp not in self._funding:
            h = self.post("public/get_funding_rate_history", {
                "instrument_name": perp, "start_timestamp": self.now_ms - 30 * DAY_MS + 60_000,
                "end_timestamp": self.now_ms, "period": 3600}).get("funding_rate_history", [])
            hist = [(int(x["timestamp"]), _f(x["funding_rate"])) for x in h]

            def avg(d: int) -> float:
                xs = [r for t, r in hist if t >= self.now_ms - d * DAY_MS]
                return mean(xs) * HOURS_PER_YEAR if xs else 0.0

            sc = {"30d realised avg": avg(30), "7d realised avg": avg(7),
                  "current rate": _f(self.ticker(perp).get("f")) * HOURS_PER_YEAR}
            for apr in self.extra_funding:
                sc[f"stress {apr * 100:g}% APR"] = apr
            self._funding[perp] = sc
        return self._funding[perp]

    def usdc_rates(self, risk_universe: Any) -> tuple[float, float, float]:
        """(borrow APY now, supply APY now, borrow APY 30d avg) for a risk universe."""
        if risk_universe not in self._rates:
            ir = self.post("public/get_interest_rate_history", {
                "currency": "USDC", "start_timestamp": self.now_ms - 30 * DAY_MS,
                "end_timestamp": self.now_ms, "period": 86400}).get("interest_rate_history", [])
            ir = [x for x in ir if risk_universe is None or x.get("risk_universe_id") == risk_universe]
            if ir:
                self._rates[risk_universe] = (_f(ir[-1]["borrow_apy"]["close"]), _f(ir[-1]["supply_apy"]["close"]),
                                              mean(_f(x["borrow_apy"]["close"]) for x in ir))
            else:
                self._rates[risk_universe] = (0.0, 0.0, 0.0)
        return self._rates[risk_universe]

    def fees(self, perp_name: Optional[str], option_name: Optional[str]) -> FeeSchedule:
        """Instrument fee schedule, overridden by the account's fee_info if a key is available."""
        if self._fees is None:
            pi = self.instrument(perp_name) if perp_name else {}
            oi = self.instrument(option_name) if option_name else {}
            fi: dict[str, Any] = {}
            if self.c.acct is not None:
                try:
                    fi = self.c.post("private/get_account", {"wallet": self.c.wallet}).get("fee_info") or {}
                except SystemExit:
                    fi = {}
            disc = _f(fi.get("base_fee_discount"))
            self._fees = FeeSchedule(
                perp_rate=_f(fi.get("perp_taker_fee")) or _f(pi.get("taker_fee_rate"), 0.0003),
                perp_base=_f(pi.get("base_fee"), 0.1) * (1 - disc),
                option_rate=_f(fi.get("option_taker_fee")) or _f(oi.get("taker_fee_rate"), 0.0003),
                option_base=_f(oi.get("base_fee"), 0.5) * (1 - disc),
                option_cap=_f(oi.get("mark_price_fee_rate_cap"), 0.125),
            )
        return self._fees

    def roll_target(self, ccy: str) -> Optional[RollTarget]:
        """First live expiry beyond horizon + buffer; synthetic strike nearest its forward."""
        if ccy in self._rolls:
            return self._rolls[ccy]
        if self._live is None:
            live = self.post("public/get_all_live_instruments", {})
            self._live = live if isinstance(live, list) else live.get("instruments", [])
        cutoff = self.now_s + (self.days + self.roll_buffer_days) * 86400
        exps = sorted({n.split("-")[1] for n in self._live if isinstance(n, str)
                       and n.startswith(f"{ccy}-2") and n.count("-") == 3})
        cands = [e for e in exps if parse_option_name(f"{ccy}-{e}-1-C")[1] > cutoff]
        tgt = None
        if cands:
            e = cands[0]
            exp_s = parse_option_name(f"{ccy}-{e}-1-C")[1]
            chain = self.post("public/get_tickers", {"instrument_type": "option", "currency": ccy,
                                                     "expiry_date": e}).get("tickers", {})
            any_pr = next((t.get("option_pricing") for t in chain.values() if t.get("option_pricing")), {})
            spot = self.spot(ccy)
            curve = curve_from_pricing(spot, exp_s, self.now_s, any_pr)
            fwd = spot * math.exp(curve.basis * ((exp_s - self.now_s) / 86400 / YEAR_DAYS))
            pairs = sorted({float(n.split("-")[2]) for n in chain
                            if n.endswith("-C") and n[:-1] + "P" in chain})
            if pairs:
                k = min(pairs, key=lambda s: abs(s - fwd))
                c, p = chain[f"{ccy}-{e}-{strike_str(k)}-C"], chain[f"{ccy}-{e}-{strike_str(k)}-P"]
                step = _f(self.instrument(f"{ccy}-{e}-{strike_str(k)}-C").get("amount_step"), 0.01) or 0.01
                tgt = RollTarget(ccy=ccy, expiry=exp_s, expiry_label=e, strike=k, curve=curve,
                                 call=c, put=p, amount_step=step)
        if tgt is None:
            print(f"[warn] no {ccy} option expiry/strike pair beyond horizon; cannot model a roll", file=sys.stderr)
        self._rolls[ccy] = tgt
        return tgt


# --------------------------------------------------------------------------- #
# One subaccount's book
# --------------------------------------------------------------------------- #
@dataclass
class Book:
    sub_id: int
    now_s: float
    watch: dict[str, Any]  # raw public/margin_watch result
    spot: dict[str, float]
    perps: dict[str, float]  # name -> amount
    perp_ccy: dict[str, str]
    perp_spread: dict[str, float]
    options: list[OptionLeg]
    curves: dict[tuple[str, float], Curve]
    cash0: float  # USDC + unsettled perp value
    collateral_units: dict[str, tuple[str, float]]  # asset -> (ccy, underlying units)
    asset_apr: dict[str, float]
    funding_scen: dict[str, dict[str, float]]
    borrow_apy: float
    supply_apy: float
    borrow_apy_30d: float
    fees: FeeSchedule
    roll_targets: dict[str, RollTarget]

    @property
    def equity(self) -> float:
        return _f(self.watch.get("subaccount_value"))

    def net_delta(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for name, amt in self.perps.items():
            out[self.perp_ccy[name]] = out.get(self.perp_ccy[name], 0.0) + amt
        for leg in self.options:
            out[leg.ccy] = out.get(leg.ccy, 0.0) + leg.amount * leg.delta
        for ccy, units in self.collateral_units.values():
            if ccy != "USD":
                out[ccy] = out.get(ccy, 0.0) + units
        return out

    def expiring_groups(self, days: float) -> dict[tuple[str, float], float]:
        """Delta per (ccy, expiry) group expiring inside the horizon, at today's deltas."""
        groups: dict[tuple[str, float], float] = {}
        for leg in self.options:
            if leg.expiry <= self.now_s + days * 86400:
                groups[leg.expiry_key] = groups.get(leg.expiry_key, 0.0) + leg.amount * leg.delta
        return groups

    def roll_sizes(self, days: float, mode: str) -> dict[tuple[str, float], float]:
        """Delta to re-hedge per expiring group.

        full     replace the whole expiring delta (keeps today's residual net delta)
        neutral  replace only what is needed to end delta-neutral in that currency;
                 the residual is netted against the earliest expiring groups first
        """
        groups = self.expiring_groups(days)
        if mode == "full":
            return groups
        residual = self.net_delta()
        out: dict[tuple[str, float], float] = {}
        for key in sorted(groups, key=lambda k: k[1]):
            g = groups[key]
            r = residual.get(key[0], 0.0)
            cut = math.copysign(min(abs(g), abs(r)), g) if g * r > 0 else 0.0
            out[key] = g - cut
            residual[key[0]] = r - cut
        return out


def load_book(mkt: Market, sub_id: int) -> Book:
    w = mkt.post("public/margin_watch", {"subaccount_id": sub_id})
    positions = w.get("positions") or []
    perps: dict[str, float] = {}
    perp_ccy: dict[str, str] = {}
    perp_spread: dict[str, float] = {}
    options: list[OptionLeg] = []
    cash0 = 0.0
    for p in positions:
        name = p["instrument_name"]
        tk = mkt.ticker(name)
        if p.get("instrument_type") == "perp":
            perps[name] = _f(p["amount"])
            perp_ccy[name] = name.split("-")[0]
            perp_spread[name] = max(_f(tk.get("a")) - _f(tk.get("b")), 0.0)
            cash0 += _f(p.get("mark_value"))  # unsettled perp P&L settles into USDC
        elif p.get("instrument_type") == "option":
            ccy, exp, k, cp = parse_option_name(name)
            pr = tk.get("option_pricing") or {}
            options.append(OptionLeg(
                name=name, ccy=ccy, expiry=exp, strike=k, cp=cp, amount=_f(p["amount"]),
                vol=_f(pr.get("i")), mark=_f(p.get("mark_price")), delta=_f(p.get("delta")),
                bid=_f(tk.get("b")), ask=_f(tk.get("a")),
            ))

    ccys = {perp_ccy[n] for n in perps} | {leg.ccy for leg in options}
    collateral_units: dict[str, tuple[str, float]] = {}
    for c in w.get("collaterals") or []:
        asset = str(c.get("asset_name", "")).upper()
        if asset == "USDC":
            cash0 += _f(c.get("amount"))
            continue
        ccy = COLLATERAL_UNDERLYING.get(asset)
        if ccy and mkt.spot(ccy):
            collateral_units[asset] = (ccy, _f(c.get("mark_value")) / mkt.spot(ccy))
            ccys.add(ccy)
        else:
            collateral_units[asset] = ("USD", _f(c.get("mark_value")))  # held at constant USD value
    spot = {c: mkt.spot(c) for c in ccys}
    spot["USD"] = 1.0

    curves: dict[tuple[str, float], Curve] = {}
    for leg in options:
        if leg.expiry_key not in curves:
            pr = mkt.ticker(leg.name).get("option_pricing") or {}
            curves[leg.expiry_key] = curve_from_pricing(spot[leg.ccy], leg.expiry, mkt.now_s, pr)

    scen_names = list(mkt.funding_scenarios(next(iter(perps)))) if perps else []
    funding_scen = {s: {n: mkt.funding_scenarios(n)[s] for n in perps} for s in scen_names}
    borrow, supply, borrow30 = mkt.usdc_rates(w.get("risk_universe_id"))
    fees = mkt.fees(next(iter(perps), None), options[0].name if options else None)

    book = Book(sub_id=sub_id, now_s=mkt.now_s, watch=w, spot=spot, perps=perps, perp_ccy=perp_ccy,
                perp_spread=perp_spread, options=options, curves=curves, cash0=cash0,
                collateral_units=collateral_units,
                asset_apr={a: mkt.asset_apr.get(a, 0.0) for a in collateral_units},
                funding_scen=funding_scen, borrow_apy=borrow, supply_apy=supply, borrow_apy_30d=borrow30,
                fees=fees, roll_targets={})
    for ccy, _ in book.expiring_groups(mkt.days):
        tgt = mkt.roll_target(ccy)
        if tgt:
            book.roll_targets[ccy] = tgt
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
    options: float = 0.0  # roll-down/theta (+ delta/gamma when spot moves), at model prices
    total: float = 0.0
    min_cash: float = 0.0
    actions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.__dict__.items()}

    def __iadd__(self, other: "Result") -> "Result":
        for k, v in other.__dict__.items():
            if isinstance(v, float) and k != "min_cash":
                setattr(self, k, getattr(self, k) + v)
        return self


def simulate(book: Book, days: float, funding: dict[str, float], shock: float, plan: str,
             exec_mode: str, roll_size: str = "neutral") -> Result:
    """Hourly path from now to horizon with every underlying moving linearly to spot*(1+shock)."""
    res = Result()
    now_s = book.now_s
    steps = max(int(round(days * 24)), 1)
    dt_h = days * 24 / steps

    def spot_at(ccy: str, frac: float) -> float:
        return 1.0 if ccy == "USD" else book.spot[ccy] * (1 + shock * frac)

    cash = book.cash0
    perps = dict(book.perps)
    legs = [OptionLeg(**leg.__dict__) for leg in book.options]
    leg_curves = {id(leg): book.curves[leg.expiry_key] for leg in legs}
    units = {a: u for a, (_, u) in book.collateral_units.items()}

    value0 = sum(leg.amount * leg.value(book.spot[leg.ccy], now_s, leg_curves[id(leg)]) for leg in legs)
    premiums_paid = 0.0  # roll legs opened, at model price
    settled = 0.0  # cash received from option expiries
    res.min_cash = cash
    groups = book.roll_sizes(days, roll_size)
    handled: set[tuple[str, float]] = set()

    prev = {c: book.spot[c] for c in book.spot}
    for i in range(1, steps + 1):
        frac = i / steps
        t_s = now_s + i * dt_h * 3600
        s_now = {c: spot_at(c, frac) for c in book.spot}

        for name, amt in perps.items():  # perp MtM settles into USDC; funding paid on spot notional
            ccy = book.perp_ccy[name]
            pnl = amt * (s_now[ccy] - prev[ccy])
            fund = -amt * s_now[ccy] * funding.get(name, 0.0) * dt_h / HOURS_PER_YEAR
            cash += pnl + fund
            res.perp_price_pnl += pnl
            res.funding += fund

        for asset, (ccy, _) in book.collateral_units.items():
            res.collateral_price_pnl += units[asset] * (s_now[ccy] - prev[ccy])
            apr = book.asset_apr.get(asset, 0.0)
            if apr:
                gain = units[asset] * apr * dt_h / HOURS_PER_YEAR
                units[asset] += gain
                res.staking += gain * s_now[ccy]

        rate = math.log1p(book.borrow_apy) if cash < 0 else math.log1p(book.supply_apy)
        intr = cash * (math.exp(rate * dt_h / HOURS_PER_YEAR) - 1.0)
        cash += intr
        res.interest += intr

        for leg in [lg for lg in legs if lg.expiry <= t_s]:
            payoff = leg.amount * leg.intrinsic(s_now[leg.ccy])
            cash += payoff
            settled += payoff
            legs.remove(leg)
            key = leg.expiry_key
            if key in groups and key not in handled:
                handled.add(key)
                cash, premium = _handle_expiry(book, plan, key, groups[key], s_now, t_s, legs, leg_curves,
                                               perps, exec_mode, res, cash)
                premiums_paid += premium

        res.min_cash = min(res.min_cash, cash)
        prev = s_now

    end_s = now_s + days * 86400
    s_end = {c: spot_at(c, 1.0) for c in book.spot}
    value_end = sum(leg.amount * leg.value(s_end[leg.ccy], end_s, leg_curves[id(leg)]) for leg in legs)
    res.options = value_end + settled - value0 - premiums_paid
    res.total = (res.funding + res.staking + res.interest - res.fees - res.spread + res.perp_price_pnl
                 + res.collateral_price_pnl + res.options)
    return res


def _handle_expiry(book: Book, plan: str, key: tuple[str, float], qty: float, s_now: dict[str, float],
                   t_s: float, legs: list[OptionLeg], leg_curves: dict[int, Curve], perps: dict[str, float],
                   exec_mode: str, res: Result, cash: float) -> tuple[float, float]:
    """Apply the plan for an expiring group. Returns (cash, roll premium paid at model)."""
    ccy, exp = key
    label = ts_label(exp)
    spot = s_now[ccy]
    fees = book.fees
    tgt = book.roll_targets.get(ccy)
    if abs(qty) < 1e-9:
        res.actions.append(f"{label}: {ccy} legs expire; not re-hedged (offsets residual delta)")
        return cash, 0.0
    if plan == "roll" and tgt:
        q = round_step(abs(qty), tgt.amount_step)
        if q <= 0:
            return cash, 0.0
        side = 1 if qty > 0 else -1  # long synthetic = buy call, sell put
        premium = 0.0
        for cp, tk, leg_side in (("C", tgt.call, side), ("P", tgt.put, -side)):
            pr = tk.get("option_pricing") or {}
            leg = OptionLeg(name=tgt.leg_name(cp), ccy=ccy, expiry=tgt.expiry, strike=tgt.strike, cp=cp,
                            amount=leg_side * q, vol=_f(pr.get("i")), mark=_f(tk.get("M")), delta=_f(pr.get("d")),
                            bid=_f(tk.get("b")), ask=_f(tk.get("a")))
            model_px = leg.value(spot, t_s, tgt.curve)
            slip = (exec_price(leg.bid, leg.ask, leg.mark, leg_side, exec_mode) - leg.mark) * leg_side
            fee = fees.option_fee(spot, model_px, q)
            res.spread += slip * q
            res.fees += fee
            premium += leg_side * q * model_px
            cash -= leg_side * q * model_px + slip * q + fee
            legs.append(leg)
            leg_curves[id(leg)] = tgt.curve
        res.actions.append(f"{label}: roll {side * q:+g} {ccy} synthetic -> {tgt.expiry_label} K={strike_str(tgt.strike)}")
        return cash, premium

    perp = next((n for n, c in book.perp_ccy.items() if c == ccy), None)
    if perp is None:
        res.actions.append(f"{label}: {ccy} legs expire, no perp to adjust")
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
        res.actions.append(f"{label}: {ccy} legs expire; close {adj:+g} {perp}")
    return cash, 0.0


# --------------------------------------------------------------------------- #
# Margin around the next expiry (public/get_margin full-portfolio simulator)
# --------------------------------------------------------------------------- #
class Portfolio:
    """Explicit portfolio state for public/get_margin. Perp entry prices preserve unsettled P&L."""

    def __init__(self, watch: dict[str, Any]):
        self.pos: dict[str, list[Any]] = {}
        self.col: dict[str, float] = {}
        self.perp_mark: dict[str, float] = {}
        for p in watch.get("positions") or []:
            amt = _f(p["amount"])
            if p.get("instrument_type") == "perp":
                mark = _f(p["mark_price"])
                self.perp_mark[p["instrument_name"]] = mark
                self.pos[p["instrument_name"]] = [amt, mark - _f(p["mark_value"]) / amt if amt else mark]
            else:
                self.pos[p["instrument_name"]] = [amt, None]
        for c in watch.get("collaterals") or []:
            self.col[c["asset_name"]] = _f(c["amount"])

    def copy(self) -> "Portfolio":
        p = Portfolio({})
        p.pos = {k: list(v) for k, v in self.pos.items()}
        p.col = dict(self.col)
        p.perp_mark = dict(self.perp_mark)
        return p

    def usdc(self, x: float) -> None:
        self.col["USDC"] = self.col.get("USDC", 0.0) + x

    def trade_option(self, name: str, dq: float) -> None:
        amt = self.pos.get(name, [0.0, None])[0] + dq
        if abs(amt) < 1e-9:
            self.pos.pop(name, None)
        else:
            self.pos[name] = [amt, None]

    def trade_perp(self, name: str, dq: float) -> None:
        """Trade at mark. Reducing realises the slice's unsettled P&L into USDC."""
        mark = self.perp_mark[name]
        amt, entry = self.pos.get(name, [0.0, mark])
        if amt * dq >= 0:  # increase
            new = amt + dq
            entry = (amt * entry + dq * mark) / new if new else mark
        else:
            closed = min(abs(dq), abs(amt))
            self.usdc(math.copysign(closed, amt) * (mark - entry))
            new = amt + dq
            if abs(new) > abs(amt):  # flipped through zero
                entry = mark
        if abs(new) < 1e-9:
            self.pos.pop(name, None)
        else:
            self.pos[name] = [new, entry]

    def request(self, book: Book) -> dict[str, Any]:
        positions = []
        for name, (amt, entry) in self.pos.items():
            e: dict[str, Any] = {"instrument_name": name, "amount": str(round(amt, 10))}
            if entry is not None:
                e["entry_price"] = str(round(entry, 8))
            positions.append(e)
        cur = book.watch.get("currency")
        market = cur[0] if isinstance(cur, list) and cur else (cur or "ETH")
        return {"margin_type": book.watch.get("margin_type") or "PM2", "market": market,
                "simulated_positions": positions,
                "simulated_collaterals": [{"asset_name": a, "amount": str(round(v, 10))} for a, v in self.col.items()]}


def margin_checks(mkt: Market, book: Book, roll_size: str = "neutral") -> dict[str, Any]:
    """MM/IM for: now, after the next expiry with no action, and with each way of handling it."""
    horizon_s = book.now_s + mkt.days * 86400
    if not book.options or min(leg.expiry for leg in book.options) > horizon_s:
        return {}
    nxt = min(leg.expiry for leg in book.options)
    expiring = [leg for leg in book.options if leg.expiry == nxt]
    sizes = book.roll_sizes(mkt.days, roll_size)
    by_ccy = {ccy: q for (ccy, exp), q in sizes.items() if exp == nxt}
    settle_cash = 0.0
    for leg in expiring:
        curve = book.curves[leg.expiry_key]
        fwd = book.spot[leg.ccy] * math.exp(curve.basis * max(leg.tau_years(book.now_s), 0.0))
        settle_cash += leg.amount * leg.intrinsic(fwd)

    now = Portfolio(book.watch)
    expired = now.copy()
    for leg in expiring:
        expired.trade_option(leg.name, -leg.amount)
    expired.usdc(settle_cash)

    def add_roll(p: Portfolio, temp_hedge: bool) -> None:
        for ccy, qty in by_ccy.items():
            tgt = book.roll_targets.get(ccy)
            if not tgt or abs(qty) < 1e-9:
                continue
            q = round_step(abs(qty), tgt.amount_step)
            side = 1 if qty > 0 else -1
            p.trade_option(tgt.leg_name("C"), side * q)
            p.trade_option(tgt.leg_name("P"), -side * q)
            p.usdc(-side * q * (_f(tgt.call.get("M")) - _f(tgt.put.get("M"))))
            perp = next((n for n, c in book.perp_ccy.items() if c == ccy), None)
            if temp_hedge and perp:
                p.trade_perp(perp, -side * q)

    def close_perp(p: Portfolio) -> None:
        for ccy, qty in by_ccy.items():
            perp = next((n for n, c in book.perp_ccy.items() if c == ccy), None)
            if perp and book.perps[perp] * qty < 0:
                p.trade_perp(perp, -math.copysign(min(abs(qty), abs(book.perps[perp])), book.perps[perp]))

    cases: dict[str, Portfolio] = {"now": now, "after expiry, no action": expired}
    p = expired.copy(); add_roll(p, False); cases["after expiry + roll"] = p
    p = expired.copy(); close_perp(p); cases["after expiry + close perp"] = p
    p = now.copy()
    for leg in expiring:  # one package: close expiring legs at mark + open the roll
        p.trade_option(leg.name, -leg.amount)
        p.usdc(leg.amount * leg.mark)
    add_roll(p, False); cases["before expiry: calendar roll package"] = p
    p = now.copy(); add_roll(p, False); cases["before expiry: open roll only"] = p
    p = now.copy(); add_roll(p, True); cases["before expiry: roll + temp perp hedge"] = p
    p = expired.copy(); add_roll(p, True); cases["after expiry, temp hedge still on"] = p
    p = now.copy(); close_perp(p); cases["before expiry: close perp"] = p

    out: dict[str, Any] = {"next_expiry": ts_label(nxt, "%Y-%m-%d %H:%M UTC"), "roll_size": roll_size,
                           "rehedge_delta": {k: round(v, 4) for k, v in by_ccy.items()},
                           "settlement_cash": round(settle_cash, 2), "cases": {}}
    for label, pf in cases.items():
        try:
            r = mkt.post("public/get_margin", pf.request(book))
            out["cases"][label] = {"mm": _f(r.get("pre_maintenance_margin")), "im": _f(r.get("pre_initial_margin"))}
        except SystemExit as e:
            out["cases"][label] = {"error": str(e)[:200]}
    return out


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def fmt(x: float) -> str:
    return f"{x:>+10,.0f}"


ROW_HDR = (f"  {'plan':<6}{'funding scenario':<20}{'funding':>10}{'staking':>10}{'interest':>10}"
           f"{'options':>10}{'fees+spr':>10}{'NET':>10}{'per 30d':>10}")


def row(plan: str, scen: str, r: Result, days: float) -> str:
    return (f"  {plan:<6}{scen:<20}{fmt(r.funding)}{fmt(r.staking)}{fmt(r.interest)}{fmt(r.options)}"
            f"{fmt(-(r.fees + r.spread))}{fmt(r.total)}{fmt(r.total * 30 / days)}")


def report_book(mkt: Market, book: Book, args: argparse.Namespace, base: str, shocks: list[float]) -> dict[str, Any]:
    days = args.days
    w = book.watch
    print("\n" + "-" * 90)
    print(f"Subaccount {book.sub_id} | equity ${book.equity:,.0f} | MM ${_f(w.get('maintenance_margin')):,.0f} "
          f"IM ${_f(w.get('initial_margin')):,.0f} | {w.get('margin_type')}")
    print("  perps: " + (", ".join(f"{n} {a:+g} (${abs(a) * book.spot[book.perp_ccy[n]]:,.0f})"
                                   for n, a in book.perps.items()) or "none"))
    print("  net delta incl. collateral: " + ", ".join(f"{c} {d:+.2f} (${d * book.spot[c]:+,.0f})"
                                                      for c, d in book.net_delta().items()))
    groups = book.expiring_groups(days)
    if groups:
        sizes = book.roll_sizes(days, args.roll_size)
        print("  expiring in horizon (delta -> re-hedge): " + "; ".join(
            f"{ts_label(e)} {c} {q:+.2f} -> {sizes[(c, e)]:+.2f}"
            for (c, e), q in sorted(groups.items(), key=lambda x: x[0][1])))
        for ccy, tgt in book.roll_targets.items():
            sp = (_f(tgt.call.get("a")) - _f(tgt.call.get("b"))) + (_f(tgt.put.get("a")) - _f(tgt.put.get("b")))
            print(f"  roll target {ccy}: {tgt.expiry_label} K={strike_str(tgt.strike)} basis "
                  f"{tgt.curve.basis * 100:+.2f}%/yr, C+P bid-ask ${sp:,.2f}")
    out: dict[str, Any] = {"equity": book.equity, "scenarios": {}, "spot_grid": {}}
    print(ROW_HDR)
    plans = ("roll", "close") if groups else ("hold",)
    for plan in plans:
        for scen, rates in book.funding_scen.items():
            r = simulate(book, days, rates, 0.0, "roll" if plan == "hold" else plan, args.exec_mode,
                         args.roll_size)
            out["scenarios"][f"{plan}|{scen}"] = r
            if scen in (base, "current rate") or scen.startswith("stress"):
                print(row(plan, scen, r, days))
    for plan in plans:
        acts = out["scenarios"][f"{plan}|{base}"].actions
        if acts:
            print(f"  {plan}: " + "; ".join(acts))
    for plan in plans:
        for s in shocks:
            out["spot_grid"][f"{plan}|{s:+.2f}"] = simulate(book, days, book.funding_scen[base], s,
                                                            "roll" if plan == "hold" else plan, args.exec_mode,
                                                            args.roll_size)
    mc = margin_checks(mkt, book, args.roll_size)
    out["margin"] = mc
    if mc:
        print(f"  margin around next expiry {mc['next_expiry']} (re-hedge {mc['roll_size']} "
              + ", ".join(f"{k} {v:+.2f}" for k, v in mc["rehedge_delta"].items())
              + f", settles ${mc['settlement_cash']:,.0f} cash):")
        for label, v in mc["cases"].items():
            if "error" in v:
                print(f"    {label:<40} error {v['error']}")
            else:
                flag = "LIQUIDATABLE" if v["mm"] < 0 else ("no new risk (IM<0)" if v["im"] < 0 else "ok")
                print(f"    {label:<40} MM {v['mm']:>+11,.0f}  IM {v['im']:>+11,.0f}  {flag}")
        noact = mc["cases"].get("after expiry, no action", {})
        if noact.get("mm", 0) < 0:
            print(f"  !! LIQUIDATION WARNING: maintenance margin turns negative when {mc['next_expiry']} settles.")
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Project net P&L of Derive basis/funding-carry books.")
    ap.add_argument("--wallet", help="Owner wallet: projects every funded subaccount unless --subaccount is given.")
    ap.add_argument("--subaccount", type=int, nargs="+", help="Subaccount id(s).")
    ap.add_argument("--key-env", help="Optional session key env var (only used for account fee overrides).")
    ap.add_argument("--base-url", default=BASE_URL_DEFAULT)
    ap.add_argument("--days", type=float, default=60.0, help="Horizon in days (default 60).")
    ap.add_argument("--exec", dest="exec_mode", choices=["mark", "mid", "touch"], default="mid",
                    help="Execution price for rolls: mark, mid (default) or touch (cross the book).")
    ap.add_argument("--lido-apr", type=float, help="Override the stETH APR (decimal).")
    ap.add_argument("--asset-apr", nargs="*", default=[], metavar="ASSET=APR",
                    help="Yield on other collateral, e.g. JITOSOL=0.075 (decimal).")
    ap.add_argument("--stress-funding", type=float, nargs="*", default=[0.03, 0.0],
                    help="Extra flat funding APR scenarios (decimals). Default: 0.03 0.0")
    ap.add_argument("--roll-size", choices=["neutral", "full"], default="neutral",
                    help="Re-hedge expiring legs to end delta-neutral (default) or replace their full delta.")
    ap.add_argument("--roll-buffer-days", type=float, default=3.0,
                    help="Roll into the first expiry at least this many days past the horizon.")
    ap.add_argument("--json-out")
    args = ap.parse_args(argv)
    if not args.wallet and not args.subaccount:
        ap.error("give --wallet and/or --subaccount")

    pk = None
    if args.key_env:
        pk, src_var, signer = discover_key(args.key_env, False)
        print(f"[auth] session key from '{src_var}' (signer {signer}), used for fee_info only", file=sys.stderr)
    client = DeriveClient(pk, args.wallet, args.base_url)
    asset_apr = {}
    for kv in args.asset_apr:
        k, v = kv.split("=", 1)
        asset_apr[k.upper()] = float(v)
    mkt = Market(client, args.days, args.roll_buffer_days, asset_apr, args.lido_apr, args.stress_funding)

    sub_ids = list(args.subaccount or [])
    if not sub_ids:
        ids = mkt.post("public/get_subaccounts", {"wallet": args.wallet}).get("subaccount_ids", [])
        for sid in ids:
            w = mkt.post("public/margin_watch", {"subaccount_id": sid})
            if w.get("positions") or abs(_f(w.get("subaccount_value"))) > 1:
                sub_ids.append(sid)

    line = "=" * 90
    print(line)
    print(f"Derive basis projection | horizon {args.days:g} days | exec={args.exec_mode} roll={args.roll_size} | "
          f"{ts_label(mkt.now_s, '%Y-%m-%d %H:%M')} UTC | subaccounts {sub_ids}")
    print(line)
    books = [load_book(mkt, sid) for sid in sub_ids]
    perps_seen = sorted({n for b in books for n in b.perps})
    print("Funding APR (30d / 7d / current): " + "; ".join(
        f"{n} {mkt.funding_scenarios(n)['30d realised avg'] * 100:.2f}/{mkt.funding_scenarios(n)['7d realised avg'] * 100:.2f}"
        f"/{mkt.funding_scenarios(n)['current rate'] * 100:.2f}%" for n in perps_seen))
    if books:
        b0 = books[0]
        print(f"USDC borrow APY {b0.borrow_apy * 100:.2f}% (30d avg {b0.borrow_apy_30d * 100:.2f}%), "
              f"supply {b0.supply_apy * 100:.2f}%; stETH APR {mkt.asset_apr['WSTETH'] * 100:.2f}% ({mkt.lido_src})")

    base = "30d realised avg"
    shocks = [-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3]
    per_book = {b.sub_id: report_book(mkt, b, args, base, shocks) for b in books}

    # Combined view: each book's preferred handling of expiries ("roll" where it has expiries).
    print("\n" + line)
    total_eq = sum(b.equity for b in books)
    print(f"ALL SUBACCOUNTS {sub_ids} | equity ${total_eq:,.0f} | expiring legs rolled where present")
    print(ROW_HDR)
    scen_names = list(next((b.funding_scen for b in books if b.funding_scen), {}))
    combined: dict[str, Any] = {}
    for plan in ("roll", "close"):
        for scen in scen_names:
            tot = Result()
            for b in books:
                d = per_book[b.sub_id]["scenarios"]
                tot += d.get(f"{plan}|{scen}") or d.get(f"hold|{scen}") or Result()
            tot.total = sum((per_book[b.sub_id]["scenarios"].get(f"{plan}|{scen}")
                             or per_book[b.sub_id]["scenarios"].get(f"hold|{scen}") or Result()).total for b in books)
            combined[f"{plan}|{scen}"] = tot
            print(row(plan, scen, tot, args.days))
    print(f"\nSpot sensitivity, all underlyings moving together (funding = {base}):")
    print("  " + f"{'plan':<6}" + "".join(f"{s * 100:>+9.0f}%" for s in shocks))
    for plan in ("roll", "close"):
        vals = []
        for s in shocks:
            vals.append(sum((per_book[b.sub_id]["spot_grid"].get(f"{plan}|{s:+.2f}")
                             or per_book[b.sub_id]["spot_grid"][f"hold|{s:+.2f}"]).total for b in books))
        print("  " + f"{plan:<6}" + "".join(f"{v:>+10,.0f}" for v in vals))
    print(line)
    print("Model: Black-76 at fixed IV with each expiry's option-implied carry held constant; perp mark ~ index;\n"
          "perp P&L settles into USDC; rolls at today's spread; 'options' = forward roll-down + theta (+ delta/gamma\n"
          "when spot moves). Margin cases are Derive's public/get_margin on an explicit portfolio (read-only).")

    if args.json_out:
        def ser(x: Any) -> Any:
            return x.as_dict() if isinstance(x, Result) else x
        dump = {"generated": ts_label(mkt.now_s, "%Y-%m-%dT%H:%M:%SZ"), "horizon_days": args.days,
                "exec": args.exec_mode, "combined": {k: ser(v) for k, v in combined.items()},
                "subaccounts": {sid: {k: ({kk: ser(vv) for kk, vv in v.items()} if isinstance(v, dict) else v)
                                      for k, v in d.items()} for sid, d in per_book.items()}}
        with open(args.json_out, "w") as fh:
            json.dump(dump, fh, indent=2, default=str)
        print(f"[info] wrote {args.json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
