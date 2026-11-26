#!/usr/bin/env python3
"""Tenor x strike grid: fully hedged basis book + long calls (ATM / 5 / 10 / 15% OTM,
1 / 2 / 3 / 6 / 9 / 12-month expiries, held to expiry and re-bought), versus the
"perp long" alternative of leaving 40% of the book unhedged.

Pricing: at every month-end (last Friday 08:00 UTC) an implied-vol surface is fitted
to ALL real Deribit option trades of the following 24h (OTM calls and puts,
5-460 days):  IV = f(log forward-moneyness, log tenor), quadratic, weighted by size.
Forwards come from the trades themselves (implied carry per expiry). Options are
bought at surface IV + extra spread (default +1 vol pt) + 0.03% fee and marked at
surface mid every month, so drawdowns include mark-to-market.

Sizing: delta-matched, i.e. n = 0.40 / delta at each purchase (same starting
exposure as 40 unhedged), so strikes/tenors are compared at equal risk.

Robustness: every start month (phase) is run; a 12M strategy has 12 phases.
Only ~3 independent 12M outcomes exist in a 3y window: read phase spreads, not
single numbers.
"""
from __future__ import annotations

import json
import math
import os

import numpy as np
import pandas as pd
from scipy.optimize import brentq

import calls_vs_unhedged_backtest as bt

DATA, OUT = bt.DATA, bt.OUT
TENORS = (1, 2, 3, 6, 9, 12)
STRIKES = (0.0, 0.05, 0.10, 0.15)
H = 0.40
FEE = 0.0003
SETTLE_FEE = 0.00015


# --------------------------------------------------------------------------- #
# IV surface per date
# --------------------------------------------------------------------------- #
class Surface:
    def __init__(self, tr: pd.DataFrame, spot: float, when: pd.Timestamp):
        self.spot = spot
        tr = tr.copy()
        tr["tau"] = tr["dte"] / 365.0
        # implied carry per expiry from near-ATM trades
        carries = {}
        for exp, d in tr[np.abs(np.log(tr["moneyness"])) < 0.12].groupby("expiry"):
            d = d.sample(min(len(d), 60), random_state=1)
            vals = []
            for r in d.itertuples():
                prem = r.price_coin * r.index_price
                vol = r.iv / 100
                if r.type == "P":  # put-call parity on forward: C = P + F - K
                    f = lambda F: bt.bs_call(F, r.strike, r.tau, vol) - (prem + F - r.strike)  # noqa: E731
                else:
                    f = lambda F: bt.bs_call(F, r.strike, r.tau, vol) - prem  # noqa: E731
                try:
                    F = brentq(f, r.index_price * 0.8, r.index_price * 1.4)
                    vals.append(math.log(F / r.index_price) / r.tau)
                except ValueError:
                    pass
            if vals:
                carries[(exp - when).total_seconds() / (365 * 86400)] = float(np.median(vals))
        if carries:
            ks = sorted(carries)
            self.c_tau = np.array(ks)
            self.c_val = np.clip(np.array([carries[k] for k in ks]), -0.2, 0.4)
        else:
            self.c_tau, self.c_val = np.array([0.1]), np.array([0.05])
        tr["carry"] = np.interp(tr["tau"], self.c_tau, self.c_val)
        tr["x"] = np.log(tr["strike"] / (tr["index_price"] * np.exp(tr["carry"] * tr["tau"])))
        otm = ((tr["type"] == "C") & (tr["moneyness"] >= 0.97)) | ((tr["type"] == "P") & (tr["moneyness"] <= 1.03))
        d = tr[otm & tr["x"].between(-0.35, 0.45)]
        self.n = len(d)
        X = self._design(d["x"].to_numpy(), np.log(d["tau"].to_numpy()))
        y = d["iv"].to_numpy() / 100
        w = np.sqrt(np.clip(d["amount"].to_numpy(), 0.1, 200))
        mask = np.ones(len(y), bool)
        for _ in range(3):
            beta, *_ = np.linalg.lstsq(X[mask] * w[mask, None], y[mask] * w[mask], rcond=None)
            res = y - X @ beta
            mad = np.median(np.abs(res[mask])) * 1.4826 + 1e-4
            mask = np.abs(res) < 4 * mad
        self.beta = beta
        self.tau_lo = float(d["tau"].quantile(0.01))
        self.tau_hi = float(d["tau"].max())
        self.rmse = float(np.sqrt(np.mean(res[mask] ** 2)))

    @staticmethod
    def _design(x, lt):
        return np.column_stack([np.ones_like(x), x, x * x, lt, x * lt, lt * lt])

    def carry(self, tau):
        return float(np.interp(tau, self.c_tau, self.c_val))

    def iv(self, K, tau, S=None):
        S = S or self.spot
        tau_c = min(max(tau, self.tau_lo), self.tau_hi)
        F = S * math.exp(self.carry(tau) * tau)
        x = math.log(K / F)
        v = float(self._design(np.array([x]), np.array([math.log(tau_c)])) @ self.beta)
        return min(max(v, 0.15), 2.5), F

    def price(self, K, tau, S=None, extra=0.0):
        if tau <= 1e-6:
            return max((S or self.spot) - K, 0.0)
        v, F = self.iv(K, tau, S)
        return bt.bs_call(F, K, tau, v + extra)

    def delta(self, K, tau, S=None):
        v, F = self.iv(K, tau, S)
        return bt.bs_delta(F, K, tau, v) * F / (S or self.spot)


def build(cur: str):
    spot_h, funds, dvol, _ = bt.load(cur)
    tr = pd.read_csv(os.path.join(DATA, f"surface_{cur}.csv"), parse_dates=["date", "time", "expiry"])
    dates = sorted(d for d in tr["date"].unique() if pd.Timestamp(d) >= bt.FIRST_ROLL)
    now = spot_h.index[-1]
    grid = [pd.Timestamp(d) for d in dates if pd.Timestamp(d) < now - pd.Timedelta(days=5)] + [now]
    surf = {}
    for d in dates:
        d = pd.Timestamp(d)
        key = now if d >= now - pd.Timedelta(days=5) else d
        sub = tr[tr["date"] == d]
        surf[key] = Surface(sub, float(spot_h[key]), key)
    S = np.array([float(spot_h[g]) for g in grid])
    f = {}
    for src, fund in funds.items():
        vals = []
        for a, b in zip(grid[:-1], grid[1:]):
            hrs = spot_h.loc[a + pd.Timedelta(hours=1): b]
            vals.append(float((fund.reindex(hrs.index).fillna(0) * hrs).sum() / spot_h[a]))
        f[src] = np.array(vals)
    return grid, S, f, surf


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #
def yfrac(a, b):
    return (b - a).total_seconds() / (365 * 86400)


def sim_A(S, f, i0, reinvest):
    x = S[i0 + 1:] / S[i0:-1] - 1
    r = H * x + (1 - H) * f[i0:]
    eq = np.cumprod(1 + r) if reinvest else 1 + np.cumsum(r)
    return np.concatenate([[1.0], eq])


def sim_B(grid, S, f, surf, i0, tenor, otm, reinvest, extra=0.01, n_fixed=None):
    N = len(grid)
    book = 1.0
    units = 0.0
    K = expiry_i = None
    eq = [1.0]
    prem_paid = payoff_got = 0.0
    n_list = []
    i = i0
    next_roll = i0
    while i < N - 1:
        if i == next_roll:
            if units:
                pay = units * max(S[i] - K, 0.0)
                pay -= SETTLE_FEE * units * S[i] if pay > 0 else 0.0
                book += pay
                payoff_got += pay
                units = 0.0
            exp_i = min(i + tenor, N - 1)
            # expiry date of a 'tenor'-month option bought at grid[i]
            exp_date = grid[i + tenor] if i + tenor < N - 1 else grid[i] + pd.DateOffset(months=tenor)
            if i + tenor >= N - 1:
                # last option runs past the window: real last-Friday expiry ~tenor months out
                exp_date = pd.Timestamp(grid[i]) + pd.Timedelta(days=round(tenor * 30.44))
            tau = yfrac(grid[i], exp_date)
            sf = surf[grid[i]]
            K = S[i] * (1 + otm)
            n = n_fixed if n_fixed is not None else H / sf.delta(K, tau, S[i])
            notional = book if reinvest else 1.0
            units = n * notional / S[i]
            cost = units * (sf.price(K, tau, S[i], extra) + FEE * S[i])
            book -= cost
            prem_paid += cost
            n_list.append(n)
            K_exp = exp_date
            expiry_i = i + tenor
            next_roll = i + tenor
        # month i -> i+1: basis funding on the fully hedged book
        book += f[i] * (book if reinvest else 1.0)
        i += 1
        if units:
            tau_rem = yfrac(grid[i], K_exp)
            V = units * (surf[grid[i]].price(K, tau_rem, S[i]) if tau_rem > 1e-6 else max(S[i] - K, 0))
        else:
            V = 0.0
        eq.append(book + V)
    return np.array(eq), prem_paid, payoff_got, float(np.mean(n_list)) if n_list else float("nan")


def metrics(eq, years):
    r = eq[1:] / eq[:-1] - 1
    dd = (eq / np.maximum.accumulate(eq) - 1).min()
    tot = eq[-1] - 1
    return dict(total=float(tot), cagr=float((eq[-1]) ** (1 / years) - 1) if eq[-1] > 0 else -1.0,
                maxdd=float(dd), worst_m=float(r.min()))


def main():
    os.makedirs(OUT, exist_ok=True)
    report = {}
    for cur in ("ETH", "BTC"):
        grid, S, f, surf = build(cur)
        # surface diagnostics: ATM IV term structure through time
        diag = []
        for g in grid:
            sf = surf[g]
            row = {"date": str(g.date()), "n_trades": sf.n, "rmse_volpts": round(sf.rmse * 100, 2),
                   "carry_3m": round(sf.carry(0.25), 3), "carry_12m": round(sf.carry(1.0), 3)}
            for t in TENORS:
                row[f"atm_{t}m"] = round(sf.iv(S[grid.index(g)], t / 12)[0] * 100, 1)
                row[f"c15_{t}m"] = round(sf.iv(S[grid.index(g)] * 1.15, t / 12)[0] * 100, 1)
            diag.append(row)
        pd.DataFrame(diag).to_csv(os.path.join(OUT, f"surface_diag_{cur}.csv"), index=False)
        rep = {"surface_avg": pd.DataFrame(diag).drop(columns=["date"]).mean().round(2).to_dict()}
        for src in ("hyperliquid", "deribit"):
            rows = []
            for reinvest in (True, False):
                for tenor in TENORS:
                    for otm in STRIKES:
                        ph = []
                        for p in range(tenor):
                            if p >= len(grid) - 13:
                                break
                            years = yfrac(grid[p], grid[-1])
                            eB, prem, pay, n = sim_B(grid, S, f[src], surf, p, tenor, otm, reinvest)
                            eA = sim_A(S, f[src], p, reinvest)
                            mB, mA = metrics(eB, years), metrics(eA, years)
                            ph.append(dict(phase=p, years=years, B=mB, A=mA, prem_py=prem / years,
                                           payoff_over_prem=pay / prem if prem else float("nan"), n=n,
                                           excess_cagr=mB["cagr"] - mA["cagr"]))
                        p0 = ph[0]
                        ex = np.array([x["excess_cagr"] for x in ph])
                        rows.append(dict(
                            mode="reinvest" if reinvest else "no-reinvest", tenor_m=tenor, otm=otm,
                            avg_calls=p0["n"], B_total=p0["B"]["total"], A_total=p0["A"]["total"],
                            B_cagr=p0["B"]["cagr"], A_cagr=p0["A"]["cagr"],
                            B_maxdd=p0["B"]["maxdd"], A_maxdd=p0["A"]["maxdd"], B_worst_m=p0["B"]["worst_m"],
                            prem_per_year=p0["prem_py"], payoff_over_prem=p0["payoff_over_prem"],
                            phases=len(ph), excess_cagr_median=float(np.median(ex)),
                            excess_cagr_min=float(ex.min()), excess_cagr_max=float(ex.max()),
                            share_phases_B_beats_A=float((ex > 0).mean()),
                            B_maxdd_worst_phase=float(min(x["B"]["maxdd"] for x in ph)),
                        ))
            df = pd.DataFrame(rows)
            df.to_csv(os.path.join(OUT, f"tenor_strike_{cur}_{src}.csv"), index=False)
            rep[src] = rows
        report[cur] = rep
    json.dump(report, open(os.path.join(OUT, "tenor_strike.json"), "w"), indent=2, default=float)

    for cur, rep in report.items():
        print(f"\n######## {cur}  surface avg: {rep['surface_avg']}")
        for src in ("hyperliquid", "deribit"):
            df = pd.DataFrame(rep[src])
            df = df[df["mode"] == "reinvest"]
            print(f"--- {src} funding, reinvest. A40: total {df['A_total'].iloc[0]*100:+.0f}%  "
                  f"CAGR {df['A_cagr'].iloc[0]*100:+.1f}%  maxDD {df['A_maxdd'].iloc[0]*100:.0f}%")
            piv = df.pivot(index="tenor_m", columns="otm", values="B_total").mul(100).round(0)
            print("B total (start Nov-23):\n", piv.to_string())
            piv = df.pivot(index="tenor_m", columns="otm", values="excess_cagr_median").mul(100).round(1)
            print("median excess CAGR vs A over all start months (pp):\n", piv.to_string())
            piv = df.pivot(index="tenor_m", columns="otm", values="share_phases_B_beats_A").round(2)
            print("share of start months B beats A:\n", piv.to_string())
            piv = df.pivot(index="tenor_m", columns="otm", values="B_maxdd").mul(100).round(0)
            print("B maxDD (start Nov-23):\n", piv.to_string())
            piv = df.pivot(index="tenor_m", columns="otm", values="prem_per_year").mul(100).round(1)
            print("premium paid per year, % of notional:\n", piv.to_string())
            piv = df.pivot(index="tenor_m", columns="otm", values="payoff_over_prem").round(2)
            print("payoff / premium:\n", piv.to_string())


if __name__ == "__main__":
    main()
