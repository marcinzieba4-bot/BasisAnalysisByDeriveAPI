#!/usr/bin/env python3
"""Short tenors: 1W / 2W / 4W calls (ATM / 5 / 10 / 15% OTM) on a fully hedged basis
book, rolled at the Friday 08:00 UTC weekly expiries, vs leaving 40% unhedged.

Same machinery as tenor_strike_backtest.py but on a weekly grid:
  * IV surface fitted every Friday to all Deribit option trades 08:00-11:00 UTC
    (3-45 days to expiry), forwards implied from the trades
  * bought at surface IV + spread (default +1 vol pt; sensitivity +3), marked weekly
  * Derive-style fees: 0.03% of spot per contract capped at 12.5% of premium; same
    cap on the 0.015% settlement fee
  * delta-matched sizing (n = 0.40 / delta at each purchase)
  * 48 different start weeks (Nov-23 .. Oct-24), each compared with A40 (rebalanced
    weekly) over the same window
"""
from __future__ import annotations

import json
import math
import os

import numpy as np
import pandas as pd

import calls_vs_unhedged_backtest as bt
from tenor_strike_backtest import Surface, metrics, yfrac

DATA, OUT = bt.DATA, bt.OUT
TENORS_W = (1, 2, 4)
STRIKES = (0.0, 0.05, 0.10, 0.15)
H = 0.40


def fee_open(S, prem):
    return min(0.0003 * S, 0.125 * prem)


def fee_settle(S, pay):
    return min(0.00015 * S, 0.125 * pay)


def build(cur):
    spot_h, funds, dvol, _ = bt.load(cur)
    tr = pd.read_csv(os.path.join(DATA, f"wsurface_{cur}.csv"), parse_dates=["time", "expiry"])
    tr["date"] = pd.to_datetime(tr["date"], format="mixed")
    now = spot_h.index[-1]
    dates = sorted(pd.Timestamp(d) for d in tr["date"].unique())
    grid, surf = [], {}
    for d in dates:
        key = now if d >= now - pd.Timedelta(days=2) else d
        if key in surf:
            continue
        sub = tr[tr["date"] == d]
        if len(sub) < 50:
            continue
        surf[key] = Surface(sub, float(spot_h[key]), key)
        grid.append(key)
    grid = sorted(grid)
    S = np.array([float(spot_h[g]) for g in grid])
    f = {}
    for src, fund in funds.items():
        vals = []
        for a, b in zip(grid[:-1], grid[1:]):
            hrs = spot_h.loc[a + pd.Timedelta(hours=1): b]
            vals.append(float((fund.reindex(hrs.index).fillna(0) * hrs).sum() / spot_h[a]))
        f[src] = np.array(vals)
    # realised vol over each week (hourly log returns)
    rv = []
    for a, b in zip(grid[:-1], grid[1:]):
        lr = np.log(spot_h.loc[a:b]).diff().dropna()
        rv.append(float(lr.std() * math.sqrt(8760)))
    return grid, S, f, surf, np.array(rv)


def sim_A(S, f, i0, reinvest):
    x = S[i0 + 1:] / S[i0:-1] - 1
    r = H * x + (1 - H) * f[i0:]
    eq = np.cumprod(1 + r) if reinvest else 1 + np.cumsum(r)
    return np.concatenate([[1.0], eq])


def sim_B(grid, S, f, surf, i0, tenor, otm, reinvest, extra=0.01):
    N = len(grid)
    book, units, K, exp_date = 1.0, 0.0, None, None
    eq = [1.0]
    prem_paid = payoff_got = prem_rel = 0.0
    n_list = []
    i, next_roll = i0, i0
    while i < N - 1:
        if i == next_roll:
            if units:
                pay = units * max(S[i] - K, 0.0)
                if pay > 0:
                    pay -= units * fee_settle(S[i], pay / units)
                book += pay
                payoff_got += pay
                units = 0.0
            if i + tenor <= N - 2:
                exp_date = grid[i + tenor]
            else:  # runs past the window: true weekly expiry tenor*7 days out
                exp_date = grid[i] + pd.Timedelta(days=7 * tenor)
            tau = yfrac(grid[i], exp_date)
            sf = surf[grid[i]]
            K = S[i] * (1 + otm)
            n = H / sf.delta(K, tau, S[i])
            units = n * (book if reinvest else 1.0) / S[i]
            px = sf.price(K, tau, S[i], extra)
            cost = units * (px + fee_open(S[i], px))
            prem_rel += cost / book
            book -= cost
            prem_paid += cost
            n_list.append(n)
            next_roll = i + tenor
        book += f[i] * (book if reinvest else 1.0)
        i += 1
        if units:
            tau_rem = yfrac(grid[i], exp_date)
            V = units * (surf[grid[i]].price(K, tau_rem, S[i]) if tau_rem > 1e-6 else max(S[i] - K, 0.0))
        else:
            V = 0.0
        eq.append(book + V)
    return np.array(eq), prem_paid, payoff_got, float(np.mean(n_list)), prem_rel


def main():
    report = {}
    for cur in ("ETH", "BTC"):
        grid, S, f, surf, rv = build(cur)
        diag = {}
        for t in TENORS_W:
            ivs = [surf[g].iv(S[k], 7 * t / 365)[0] for k, g in enumerate(grid[:-t])]
            rvs = [float(np.sqrt(np.mean(rv[k:k + t] ** 2))) for k in range(len(grid) - t)]
            diag[f"{t}W"] = {"atm_iv": float(np.mean(ivs)), "realised": float(np.mean(rvs))}
        rep = {"weeks": len(grid) - 1, "iv_vs_rv": diag}
        for src in ("hyperliquid", "deribit"):
            rows = []
            for extra in (0.01, 0.03):
                for reinvest in (True, False):
                    for tenor in TENORS_W:
                        for otm in STRIKES:
                            ph = []
                            for p in range(48):
                                years = yfrac(grid[p], grid[-1])
                                eB, prem, pay, n, prel = sim_B(grid, S, f[src], surf, p, tenor, otm, reinvest, extra)
                                eA = sim_A(S, f[src], p, reinvest)
                                mB, mA = metrics(eB, years), metrics(eA, years)
                                ph.append(dict(B=mB, A=mA, n=n, prem_py=prel / years,
                                               pop=pay / prem if prem else float("nan"),
                                               ex=mB["cagr"] - mA["cagr"]))
                            ex = np.array([x["ex"] for x in ph])
                            p0 = ph[0]
                            rows.append(dict(extra_vol=extra, mode="reinvest" if reinvest else "no-reinvest",
                                             tenor_w=tenor, otm=otm, avg_calls=p0["n"],
                                             B_total=p0["B"]["total"], A_total=p0["A"]["total"],
                                             B_maxdd=p0["B"]["maxdd"], A_maxdd=p0["A"]["maxdd"],
                                             prem_per_year=p0["prem_py"], payoff_over_prem=p0["pop"],
                                             excess_cagr_median=float(np.median(ex)),
                                             excess_cagr_min=float(ex.min()),
                                             share_starts_B_beats_A=float((ex > 0).mean()),
                                             B_maxdd_worst=float(min(x["B"]["maxdd"] for x in ph)),
                                             A_maxdd_worst=float(min(x["A"]["maxdd"] for x in ph))))
            df = pd.DataFrame(rows)
            df.to_csv(os.path.join(OUT, f"weekly_{cur}_{src}.csv"), index=False)
            rep[src] = rows
        report[cur] = rep
    json.dump(report, open(os.path.join(OUT, "weekly.json"), "w"), indent=2, default=float)

    for cur, rep in report.items():
        print(f"\n######## {cur}  weeks={rep['weeks']}  IV vs RV: "
              f"{ {k: {kk: round(vv * 100, 1) for kk, vv in v.items()} for k, v in rep['iv_vs_rv'].items()} }")
        for src in ("hyperliquid", "deribit"):
            df = pd.DataFrame(rep[src])
            for extra in (0.01, 0.03):
                d = df[(df["mode"] == "reinvest") & (df["extra_vol"] == extra)]
                print(f"--- {src} funding, reinvest, spread +{int(extra * 100)} vol. A40 (start Nov-23): "
                      f"{d['A_total'].iloc[0] * 100:+.0f}%  maxDD {d['A_maxdd'].iloc[0] * 100:.0f}% "
                      f"(worst start {d['A_maxdd_worst'].iloc[0] * 100:.0f}%)")
                cols = [("B_total", 100, 0, "B total, start Nov-23 (%)"),
                        ("excess_cagr_median", 100, 1, "median excess CAGR vs A40, 48 start weeks (pp)"),
                        ("excess_cagr_min", 100, 1, "worst start excess CAGR (pp)"),
                        ("share_starts_B_beats_A", 1, 2, "share of starts B beats A40")]
                if extra == 0.01:
                    cols += [("B_maxdd_worst", 100, 0, "B maxDD worst start (%)"),
                             ("avg_calls", 1, 2, "calls per unit (delta-matched)"),
                             ("prem_per_year", 100, 1, "premium per year, % equity"),
                             ("payoff_over_prem", 1, 2, "payoff / premium")]
                for col, mul, nd, title in cols:
                    piv = d.pivot(index="tenor_w", columns="otm", values=col).mul(mul).round(nd)
                    piv.columns = [f"{int(c * 100)}%" for c in piv.columns]
                    print(f"{title}:\n{piv.to_string()}")


if __name__ == "__main__":
    main()
