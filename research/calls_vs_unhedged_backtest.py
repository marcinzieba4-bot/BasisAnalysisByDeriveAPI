#!/usr/bin/env python3
"""Basis book: get upside via (A) leaving part of the spot unhedged, or
(B) staying fully hedged and buying monthly 5%-OTM calls?

Setup per 1 unit of spot held in the basis book (scale to 100 coins / $ at will):
  A  "unhedged / synthetic perp long":  long 1 spot, short (1-h) perp
        -> linear delta h (default 0.40), earns funding only on (1-h)
  B  "full hedge + calls":              long 1 spot, short 1 perp, long n calls
        -> n (default 1.0) monthly calls struck at 1.05 x spot, rolled at every
           monthly expiry (last Friday 08:00 UTC, as on Derive/Deribit);
           earns funding on the full 1, pays the option premium
  0  "pure basis":                      long 1 spot, short 1 perp (reference)

Everything is driven by real history (last 3 years, Nov-2023 .. Nov-2026):
  * spot / settlement: Deribit hourly index (08:00 UTC fixings)
  * basis (funding):   Hyperliquid hourly funding (Binance-style formula with
                       +11% APR baseline) and Deribit perp funding (lower) as a
                       second source, plus flat-APR sensitivities
  * option cost:       actual Deribit trades of next-monthly calls struck ~3-7%
                       OTM in the 24h after each roll (taker-buy IV ~= ask),
                       priced off the forward implied by those same trades,
                       plus an extra Derive spread (default +1 vol pt) and fees.

"Reinvesting" is modelled two ways:
  * no reinvest   - fixed notional every month, P&L withdrawn (simple sum)
  * full reinvest - notional resized to equity every month (compounding), so
                    funding income, unhedged gains and realised call payoffs
                    are all redeployed into the basis book.
Robustness: (1) every daily roll phase with 30-day options priced off DVOL +
calibrated skew offset, (2) block bootstrap of the realised months (no vol
dampening: months are resampled with their own vol, IV and funding).
"""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "output")

FIRST_ROLL = pd.Timestamp("2023-11-24 08:00")  # 3 years back from Nov-2026


# --------------------------------------------------------------------------- #
# Black-76 helpers (Deribit/Derive price options off the expiry forward)
# --------------------------------------------------------------------------- #
def bs_call(F, K, T, vol):
    if T <= 0 or vol <= 0:
        return max(F - K, 0.0)
    sd = vol * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sd * sd) / sd
    return F * norm.cdf(d1) - K * norm.cdf(d1 - sd)


def bs_delta(F, K, T, vol):
    sd = vol * math.sqrt(T)
    return norm.cdf((math.log(F / K) + 0.5 * sd * sd) / sd)


def implied_forward(prem_usd, K, T, vol, S):
    f = lambda F: bs_call(F, K, T, vol) - prem_usd  # noqa: E731
    try:
        return brentq(f, S * 0.8, S * 1.3)
    except ValueError:
        return float("nan")


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def load(cur: str):
    idx = pd.read_csv(os.path.join(DATA, f"funding_deribit_{cur}.csv"), parse_dates=["time"])
    idx["time"] = idx["time"].dt.floor("h")
    idx = idx.drop_duplicates("time").set_index("time")
    spot_h = idx["index_price"].astype(float)
    fund_der = idx["interest_1h"].astype(float)

    hl = pd.read_csv(os.path.join(DATA, f"funding_hl_{cur}.csv"))
    hl["time"] = pd.to_datetime(hl["time"], format="mixed").dt.floor("h")
    fund_hl = hl.drop_duplicates("time").set_index("time")["fundingRate"].astype(float)
    fund_hl = fund_hl.reindex(spot_h.index).fillna(0.0)

    dvol = pd.read_csv(os.path.join(DATA, f"dvol_{cur}.csv"), parse_dates=["date"]).set_index("date")["dvol"] / 100
    import glob
    files = sorted(glob.glob(os.path.join(DATA, f"optiv*_{cur}.csv")))
    opt = pd.concat([pd.read_csv(f, parse_dates=["roll", "expiry", "time"]) for f in files if os.path.getsize(f) > 5])
    opt = opt.drop_duplicates(["instrument", "time", "amount", "iv"])
    return spot_h, {"hyperliquid": fund_hl, "deribit": fund_der}, dvol, opt


def roll_quotes(opt: pd.DataFrame, dvol: pd.Series, spot_h: pd.Series, lo: float = 1.03,
                hi: float = 1.07) -> pd.DataFrame:
    """Per roll: IV a buyer paid for a next-monthly call struck at strike/spot in [lo, hi]
    (default ~5% OTM), and the forward carry."""
    rows = []
    for roll, d in opt.groupby("roll"):
        exp = d["expiry"].iloc[0]
        T = (exp - roll).total_seconds() / (365 * 86400)
        S = float(spot_h.get(roll, np.nan))
        near = d[d["moneyness"].between(lo, hi)]
        buys = near[near["direction"] == "buy"]
        if len(buys) >= 3:
            iv = np.average(buys["iv"], weights=buys["amount"]) / 100
            src = "taker-buy"
        elif len(near) >= 3:
            iv = np.average(near["iv"], weights=near["amount"]) / 100 + 0.005
            src = "all+0.5"
        else:
            iv = np.nan
            src = "none"
        mid_iv = np.average(near["iv"], weights=near["amount"]) / 100 if len(near) else np.nan
        # forward implied by each trade, median -> annualised carry
        Ts = (d["expiry"] - d["time"]).dt.total_seconds() / (365 * 86400)
        fwd = [
            implied_forward(r.price_coin * r.index_price, r.strike, t, r.iv / 100, r.index_price) / r.index_price
            for r, t in zip(d.itertuples(), Ts)
        ]
        carry = np.nanmedian([math.log(f) / t for f, t in zip(fwd, Ts) if f == f and t > 0])
        rows.append(
            dict(roll=roll, expiry=exp, T=T, spot=S, iv_paid=iv, iv_mid=mid_iv, iv_src=src,
                 carry_apr=carry, dvol=float(dvol.asof(roll.normalize())), n_trades=len(near))
        )
    q = pd.DataFrame(rows).set_index("roll")
    q["skew_vs_dvol"] = q["iv_paid"] - q["dvol"]
    return q


# --------------------------------------------------------------------------- #
# Monthly-expiry backtest
# --------------------------------------------------------------------------- #
def monthly_table(spot_h, fund, q, h=0.40, n_calls=1.0, otm=0.05, extra_vol=0.01,
                  fee_open=0.0003, fee_settle=0.00015, flat_apr=None, delta_match=False):
    rolls = q[q.index >= FIRST_ROLL]
    now = spot_h.index[-1]
    rows = []
    for roll, r in rolls.iterrows():
        exp = r["expiry"]
        end = min(exp, now)
        S0 = spot_h[roll]
        S1 = spot_h[end]
        K = S0 * (1 + otm)
        iv = r["iv_paid"] if r["iv_paid"] == r["iv_paid"] else r["dvol"] + q["skew_vs_dvol"].median()
        iv_buy = iv + extra_vol
        carry = r["carry_apr"] if r["carry_apr"] == r["carry_apr"] else q["carry_apr"].median()
        F0 = S0 * math.exp(carry * r["T"])
        prem = bs_call(F0, K, r["T"], iv_buy) / S0 + fee_open
        delta1 = bs_delta(F0, K, r["T"], iv)
        n = (h / delta1) if delta_match else n_calls

        if end < exp:  # still open -> mark to market with remaining time
            Trem = (exp - end).total_seconds() / (365 * 86400)
            payoff = bs_call(S1 * math.exp(carry * Trem), K, Trem, r["dvol"]) / S0
            status = f"MTM {end:%Y-%m-%d %H:%M}"
        else:
            payoff = max(S1 - K, 0.0) / S0
            payoff -= fee_settle * (S1 / S0) if payoff > 0 else 0.0
            status = "expired"

        hrs = spot_h.loc[roll + pd.Timedelta(hours=1): end]
        if flat_apr is None:
            f = fund.reindex(hrs.index).fillna(0.0)
            fm = float((f * hrs).sum() / S0)  # funding received per 1 unit short, as frac of S0
        else:
            fm = float(flat_apr / 8760 * (hrs / S0).sum())
        x = S1 / S0 - 1
        lr = np.log(spot_h.loc[roll:end]).diff().dropna()
        rv = float(lr.std() * math.sqrt(8760)) if len(lr) > 10 else np.nan

        rA = h * x + (1 - h) * fm
        rB = fm - n * prem + n * payoff
        rows.append(dict(
            roll=roll, expiry=exp, status=status, S0=S0, S1=S1, move=x, K=K, iv_paid=iv_buy,
            realised_vol=rv, premium=prem, n_calls=n, delta_calls=n * delta1, call_payoff=payoff,
            funding_m=fm, funding_apr=fm / r["T"] if r["T"] else np.nan,
            r_basis=fm, r_A=rA, r_B=rB, B_minus_A=rB - rA,
        ))
    return pd.DataFrame(rows).set_index("roll")


def stats(r: pd.Series, years: float) -> dict:
    eq = (1 + r).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    simple = r.sum()
    comp = eq.iloc[-1] - 1
    vol = r.std() * math.sqrt(12)
    return dict(
        no_reinvest_total=simple, no_reinvest_apr=simple / years,
        reinvest_total=comp, reinvest_cagr=(1 + comp) ** (1 / years) - 1,
        max_drawdown=dd, worst_month=r.min(), best_month=r.max(),
        ann_vol=vol, sharpe0=(r.mean() * 12) / vol if vol else np.nan,
    )


# --------------------------------------------------------------------------- #
# Robustness 1: every daily roll phase, 30d options off DVOL + skew offset
# --------------------------------------------------------------------------- #
def phase_sweep(spot_h, fund, dvol, q, h=0.40, n_calls=1.0, otm=0.05, extra_vol=0.01, fee=0.0003):
    daily = spot_h[spot_h.index.hour == 8]
    daily.index = daily.index.normalize()
    fd = (fund.reindex(spot_h.index).fillna(0) * spot_h).groupby(spot_h.index.normalize()).sum()
    skew = q["skew_vs_dvol"].median()
    carry = q["carry_apr"].median()
    start = FIRST_ROLL.normalize()
    days = daily[daily.index >= start]
    T = 30 / 365
    res = []
    for phase in range(30):
        rA, rB = [], []
        i = phase
        idx = days.index
        while i + 30 < len(idx):
            d0, d1 = idx[i], idx[i + 30]
            S0, S1 = days[d0], days[d1]
            iv = float(dvol.asof(d0)) + skew + extra_vol
            K = S0 * (1 + otm)
            prem = bs_call(S0 * math.exp(carry * T), K, T, iv) / S0 + fee
            fm = float(fd.loc[d0 + pd.Timedelta(days=1): d1].sum() / S0)
            x = S1 / S0 - 1
            rA.append(h * x + (1 - h) * fm)
            rB.append(fm - n_calls * prem + n_calls * max(S1 - K, 0) / S0)
            i += 30
        rA, rB = np.array(rA), np.array(rB)
        res.append(dict(phase=phase, months=len(rA),
                        A_simple=rA.sum(), B_simple=rB.sum(),
                        A_comp=np.prod(1 + rA) - 1, B_comp=np.prod(1 + rB) - 1))
    return pd.DataFrame(res)


# --------------------------------------------------------------------------- #
# Robustness 2: stationary block bootstrap of the realised months
# --------------------------------------------------------------------------- #
def bootstrap(tab: pd.DataFrame, months=36, sims=20000, block=3, seed=7):
    rng = np.random.default_rng(seed)
    A = tab["r_A"].to_numpy()
    B = tab["r_B"].to_numpy()
    N = len(A)
    out = np.empty((sims, 4))
    for s in range(sims):
        idx = []
        while len(idx) < months:
            st = rng.integers(N)
            idx += [(st + k) % N for k in range(block)]
        idx = np.array(idx[:months])
        a, b = A[idx], B[idx]
        out[s] = [a.sum(), b.sum(), np.prod(1 + a) - 1, np.prod(1 + b) - 1]
    df = pd.DataFrame(out, columns=["A_simple", "B_simple", "A_comp", "B_comp"])
    return df


def pct(x, d=1):
    return f"{x * 100:+.{d}f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--unhedged", type=float, default=0.40, help="A: unhedged fraction (40 of 100)")
    ap.add_argument("--calls", type=float, default=1.00, help="B: calls per unit of basis (100 of 100)")
    ap.add_argument("--otm", type=float, default=0.05)
    ap.add_argument("--extra-vol", type=float, default=0.01, help="Derive spread over Deribit taker IV")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    report: dict = {}
    for cur in ("ETH", "BTC"):
        spot_h, funds, dvol, opt = load(cur)
        q = roll_quotes(opt, dvol, spot_h)
        q.to_csv(os.path.join(OUT, f"roll_quotes_{cur}.csv"))
        cur_rep: dict = {"quotes": {
            "median_iv_paid": float(q.loc[q.index >= FIRST_ROLL, "iv_paid"].median()),
            "median_skew_vs_dvol": float(q["skew_vs_dvol"].median()),
            "median_forward_carry_apr": float(q["carry_apr"].median()),
        }}
        for fsrc, fund in funds.items():
            tab = monthly_table(spot_h, fund, q, h=args.unhedged, n_calls=args.calls,
                                otm=args.otm, extra_vol=args.extra_vol)
            tab.to_csv(os.path.join(OUT, f"monthly_{cur}_{fsrc}.csv"))
            years = (tab.index[-1] + (tab["expiry"].iloc[-1] - tab.index[-1]) - tab.index[0]).days / 365
            years = (min(tab["expiry"].iloc[-1], spot_h.index[-1]) - tab.index[0]).days / 365
            rep = {k: stats(tab[c], years) for k, c in (("basis_only", "r_basis"), ("A_unhedged", "r_A"), ("B_calls", "r_B"))}
            rep["months"] = len(tab)
            rep["years"] = years
            rep["B_beats_A_months"] = int((tab["B_minus_A"] > 0).sum())
            rep["avg_funding_apr"] = float(tab["funding_m"].sum() / years)
            rep["avg_call_delta_at_roll"] = float(tab["delta_calls"].mean())
            rep["avg_premium_month"] = float(tab["premium"].mean())
            rep["avg_iv_paid"] = float(tab["iv_paid"].mean())
            rep["avg_realised_vol"] = float(tab["realised_vol"].mean())
            rep["sum_premium"] = float(tab["premium"].sum() * args.calls)
            rep["sum_payoff"] = float(tab["call_payoff"].sum() * args.calls)
            rep["spot_total_move"] = float(tab["S1"].iloc[-1] / tab["S0"].iloc[0] - 1)
            by_year = tab.groupby(tab.index.year)[["r_A", "r_B", "r_basis"]].sum()
            rep["by_year_simple"] = by_year.to_dict(orient="index")
            # delta-matched variant (n = h / delta each month)
            tdm = monthly_table(spot_h, fund, q, h=args.unhedged, otm=args.otm,
                                extra_vol=args.extra_vol, delta_match=True)
            rep["B_delta_matched"] = stats(tdm["r_B"], years) | {"avg_n_calls": float(tdm["n_calls"].mean())}
            # sensitivities
            sens = {}
            for ev in (0.0, 0.01, 0.03):
                t = monthly_table(spot_h, fund, q, h=args.unhedged, n_calls=args.calls, otm=args.otm, extra_vol=ev)
                sens[f"extra_vol_{int(ev * 100)}"] = {"A": stats(t["r_A"], years)["reinvest_total"],
                                                      "B": stats(t["r_B"], years)["reinvest_total"]}
            for apr in (0.05, 0.10, 0.15, 0.20):
                t = monthly_table(spot_h, fund, q, h=args.unhedged, n_calls=args.calls, otm=args.otm,
                                  extra_vol=args.extra_vol, flat_apr=apr)
                sens[f"flat_basis_{int(apr * 100)}apr"] = {"A": stats(t["r_A"], years)["reinvest_total"],
                                                           "B": stats(t["r_B"], years)["reinvest_total"],
                                                           "basis": stats(t["r_basis"], years)["reinvest_total"]}
            for hh, nn in ((0.2, 0.5), (0.3, 0.75), (0.4, 1.0), (0.5, 1.25)):
                t = monthly_table(spot_h, fund, q, h=hh, n_calls=nn, otm=args.otm, extra_vol=args.extra_vol)
                sens[f"A{int(hh * 100)}_vs_B{int(nn * 100)}calls"] = {"A": stats(t["r_A"], years)["reinvest_total"],
                                                                     "B": stats(t["r_B"], years)["reinvest_total"]}
            for o in (0.0, 0.10):
                t = monthly_table(spot_h, fund, q, h=args.unhedged, n_calls=args.calls, otm=o, extra_vol=args.extra_vol + 0.0)
                sens[f"strike_{int(o * 100)}pct_otm_approxIV"] = {"A": stats(t["r_A"], years)["reinvest_total"],
                                                                  "B": stats(t["r_B"], years)["reinvest_total"],
                                                                  "avg_delta": float(t["delta_calls"].mean())}
            rep["sensitivity_reinvest_total"] = sens
            # robustness
            ph = phase_sweep(spot_h, fund, dvol, q, h=args.unhedged, n_calls=args.calls, otm=args.otm,
                             extra_vol=args.extra_vol)
            rep["phase_sweep"] = {
                "B_beats_A_simple_share": float((ph["B_simple"] > ph["A_simple"]).mean()),
                "B_beats_A_comp_share": float((ph["B_comp"] > ph["A_comp"]).mean()),
                "A_comp_median": float(ph["A_comp"].median()), "B_comp_median": float(ph["B_comp"].median()),
                "A_comp_range": [float(ph["A_comp"].min()), float(ph["A_comp"].max())],
                "B_comp_range": [float(ph["B_comp"].min()), float(ph["B_comp"].max())],
            }
            bs = bootstrap(tab)
            rep["bootstrap_36m"] = {
                "P_B_beats_A_comp": float((bs["B_comp"] > bs["A_comp"]).mean()),
                "P_B_beats_A_simple": float((bs["B_simple"] > bs["A_simple"]).mean()),
                "A_comp_p5_p50_p95": [float(v) for v in bs["A_comp"].quantile([.05, .5, .95])],
                "B_comp_p5_p50_p95": [float(v) for v in bs["B_comp"].quantile([.05, .5, .95])],
                "P_A_loss": float((bs["A_comp"] < 0).mean()), "P_B_loss": float((bs["B_comp"] < 0).mean()),
            }
            bs12 = bootstrap(tab, months=12)
            rep["bootstrap_12m"] = {
                "P_B_beats_A_comp": float((bs12["B_comp"] > bs12["A_comp"]).mean()),
                "A_comp_p5_p50_p95": [float(v) for v in bs12["A_comp"].quantile([.05, .5, .95])],
                "B_comp_p5_p50_p95": [float(v) for v in bs12["B_comp"].quantile([.05, .5, .95])],
            }
            cur_rep[fsrc] = rep
        report[cur] = cur_rep

    with open(os.path.join(OUT, "summary.json"), "w") as fh:
        json.dump(report, fh, indent=2, default=float)

    for cur, cr in report.items():
        print(f"\n=== {cur} ===  quotes: {json.dumps({k: round(v, 4) for k, v in cr['quotes'].items()})}")
        for fsrc in ("hyperliquid", "deribit"):
            r = cr[fsrc]
            print(f"-- funding source: {fsrc}  ({r['months']} months, {r['years']:.2f}y, "
                  f"avg basis {pct(r['avg_funding_apr'])} APR, spot {pct(r['spot_total_move'])})")
            for k in ("basis_only", "A_unhedged", "B_calls"):
                s = r[k]
                print(f"   {k:<11} no-reinv {pct(s['no_reinvest_total']):>8}  reinv {pct(s['reinvest_total']):>8} "
                      f"(CAGR {pct(s['reinvest_cagr'])})  maxDD {pct(s['max_drawdown'])}  worst {pct(s['worst_month'])}"
                      f"  best {pct(s['best_month'])}  Sharpe {s['sharpe0']:.2f}")
            s = r["B_delta_matched"]
            print(f"   B_dmatched  no-reinv {pct(s['no_reinvest_total']):>8}  reinv {pct(s['reinvest_total']):>8}  "
                  f"avg n={s['avg_n_calls']:.2f}")
            print(f"   B beats A in {r['B_beats_A_months']}/{r['months']} months; avg call delta {r['avg_call_delta_at_roll']:.2f};"
                  f" avg prem/month {pct(r['avg_premium_month'], 2)}; IV paid {pct(r['avg_iv_paid'])} vs RV {pct(r['avg_realised_vol'])};"
                  f" sum prem {pct(r['sum_premium'])} vs payoff {pct(r['sum_payoff'])}")
            print(f"   phase sweep: {json.dumps(r['phase_sweep'])}")
            print(f"   bootstrap36: {json.dumps(r['bootstrap_36m'])}")
            print(f"   bootstrap12: {json.dumps(r['bootstrap_12m'])}")
            print(f"   by year (simple): {json.dumps({y: {k: round(v, 4) for k, v in d.items()} for y, d in r['by_year_simple'].items()})}")
            print(f"   sens: {json.dumps({k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in r['sensitivity_reinvest_total'].items()})}")


if __name__ == "__main__":
    main()
