#!/usr/bin/env python3
"""ATM vs 5%-OTM vs 10%-OTM monthly calls on a fully hedged basis book, vs leaving
40% unhedged. Every strike is priced with the real Deribit IV traded at that
strike at each roll (no shared-vol shortcut), + Derive spread and fees.

Sizing variants:
  same count   100 calls per 100 of basis (what you'd literally trade)
  delta-match  n = 0.40 / delta each month (same starting exposure as A)
  budget-match n = premium(100 x 5%OTM) / premium(strike) (same monthly spend)
"""
import json, math, os
import numpy as np, pandas as pd
import calls_vs_unhedged_backtest as bt

STRIKES = {"ATM": (0.0, 0.98, 1.02), "5% OTM": (0.05, 1.03, 1.07), "10% OTM": (0.10, 1.08, 1.12)}
comp = lambda r: float(np.prod(1 + r) - 1)  # noqa: E731


def run(cur):
    spot_h, funds, dvol, opt = bt.load(cur)
    qs = {k: bt.roll_quotes(opt, dvol, spot_h, lo, hi) for k, (_, lo, hi) in STRIKES.items()}
    res = {"iv": {k: float(q.loc[q.index >= bt.FIRST_ROLL, "iv_paid"].mean()) for k, q in qs.items()},
           "iv_coverage": {k: int(q.loc[q.index >= bt.FIRST_ROLL, "iv_src"].ne("none").sum()) for k, q in qs.items()}}
    for src, fund in funds.items():
        yrs = (spot_h.index[-1] - bt.FIRST_ROLL).days / 365
        base = bt.monthly_table(spot_h, fund, qs["5% OTM"], otm=0.05)
        rA = base["r_A"]
        rows, rets = [], {"A 40 unhedged": rA}
        for k, (otm, _, _) in STRIKES.items():
            same = bt.monthly_table(spot_h, fund, qs[k], otm=otm, n_calls=1.0)
            dm = bt.monthly_table(spot_h, fund, qs[k], otm=otm, delta_match=True)
            n_bud = (base["premium"] / same["premium"]).to_numpy()
            rB_bud = same["funding_m"] + n_bud * (same["call_payoff"] - same["premium"])
            for lab, t, r, n in (("100 calls", same, same["r_B"], same["n_calls"]),
                                 ("delta-matched", dm, dm["r_B"], dm["n_calls"]),
                                 ("budget-matched", same, rB_bud, pd.Series(n_bud, index=same.index))):
                st = bt.stats(r, yrs)
                rets[f"{k} {lab}"] = r
                rows.append({"strike": k, "sizing": lab, "avg_n": float(n.mean()),
                             "avg_delta": float((n * same["delta_calls"]).mean()),
                             "avg_prem_month": float((n * same["premium"]).mean()),
                             "payoff_over_prem": float((n * t["call_payoff"]).sum() / (n * t["premium"]).sum()),
                             "reinvest": st["reinvest_total"], "no_reinvest": st["no_reinvest_total"],
                             "maxDD": st["max_drawdown"], "worst_m": st["worst_month"], "sharpe": st["sharpe0"],
                             "months_beat_A": int((r > rA).sum())})
            ph = bt.phase_sweep(spot_h, fund, dvol, qs[k], otm=otm)
            rows[-3]["phase_beat_A"] = float((ph["B_comp"] > ph["A_comp"]).mean())
            rows[-3]["phase_median"] = float(ph["B_comp"].median())
        # bootstrap: joint resampling of months -> P(each beats A, ATM beats 5%OTM)
        rng = np.random.default_rng(11)
        M = pd.DataFrame(rets).to_numpy()
        N = len(M)
        wins = np.zeros(M.shape[1]); atm_vs_5 = {"100 calls": 0, "delta-matched": 0, "budget-matched": 0}
        cols = list(rets)
        sims = 20000
        for _ in range(sims):
            idx = []
            while len(idx) < 36:
                st0 = rng.integers(N); idx += [(st0 + j) % N for j in range(3)]
            tot = np.prod(1 + M[np.array(idx[:36])], axis=0) - 1
            wins += tot > tot[0]
            for lab in atm_vs_5:
                atm_vs_5[lab] += tot[cols.index(f"ATM {lab}")] > tot[cols.index(f"5% OTM {lab}")]
        for r in rows:
            r["P_beat_A_boot36"] = float(wins[cols.index(f"{r['strike']} {r['sizing']}")] / sims)
        res[src] = {"A": bt.stats(rA, yrs), "rows": rows,
                    "P_ATM_beats_5OTM_boot36": {k: v / sims for k, v in atm_vs_5.items()},
                    "by_year_100calls": pd.DataFrame({k: rets[f"{k} 100 calls"] for k in STRIKES} | {"A": rA})
                    .groupby(rA.index.year).apply(lambda d: (1 + d).prod() - 1).round(4).to_dict(orient="index")}
        # monthly detail for the 100-call variants
        det = pd.DataFrame({"move": base["move"]} | {k: rets[f"{k} 100 calls"] for k in STRIKES} | {"A": rA})
        det.to_csv(os.path.join(bt.OUT, f"strike_monthly_{cur}_{src}.csv"))
    return res


if __name__ == "__main__":
    out = {c: run(c) for c in ("ETH", "BTC")}
    json.dump(out, open(os.path.join(bt.OUT, "strike_compare.json"), "w"), indent=2, default=float)
    for c, r in out.items():
        print(f"\n===== {c}  avg IV paid: { {k: round(v*100,1) for k, v in r['iv'].items()} }  coverage {r['iv_coverage']}")
        for src in ("hyperliquid", "deribit"):
            print(f"-- funding {src}: A40 reinvest {r[src]['A']['reinvest_total']*100:+.0f}%  noreinv {r[src]['A']['no_reinvest_total']*100:+.0f}%  maxDD {r[src]['A']['max_drawdown']*100:.0f}%")
            df = pd.DataFrame(r[src]["rows"])
            for k in ("reinvest", "no_reinvest", "maxDD", "worst_m", "avg_prem_month", "phase_median"):
                if k in df: df[k] = (df[k] * 100).round(1)
            for k in ("avg_n", "avg_delta", "payoff_over_prem", "sharpe", "P_beat_A_boot36", "phase_beat_A"):
                if k in df: df[k] = df[k].round(2)
            print(df.to_string(index=False))
            print("   P(ATM beats 5%OTM, bootstrap 36m):", {k: round(v, 2) for k, v in r[src]["P_ATM_beats_5OTM_boot36"].items()})
            print("   by year (100 calls, compounded):", r[src]["by_year_100calls"])
