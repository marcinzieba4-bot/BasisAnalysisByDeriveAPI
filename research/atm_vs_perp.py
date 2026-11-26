#!/usr/bin/env python3
"""ATM monthly calls (n per 100 of basis, n = 70..100) on a fully hedged basis book
vs the 'perp long' alternative of leaving part of the book unhedged.

Each n is compared with (a) A40, i.e. 40 unhedged, the alternative actually on the
table, and (b) A at the same starting delta as the calls (n x avg ATM delta).
ATM IV = real Deribit taker trades struck 98-102% of spot at each roll, plus 1 vol
pt for Derive, plus fees."""
import json, os
import numpy as np, pandas as pd
from scipy.optimize import brentq
import calls_vs_unhedged_backtest as bt

comp = lambda r: float(np.prod(1 + r) - 1)  # noqa: E731
NS = (0.70, 0.73, 0.80, 0.85, 0.90, 1.00)
out = {}
for cur in ("ETH", "BTC"):
    spot_h, funds, dvol, opt = bt.load(cur)
    q = bt.roll_quotes(opt, dvol, spot_h, 0.98, 1.02)
    yrs = (spot_h.index[-1] - bt.FIRST_ROLL).days / 365
    dlt = float(bt.monthly_table(spot_h, funds["deribit"], q, otm=0.0)["delta_calls"].mean())
    out[cur] = {"atm_delta": dlt}
    for src, fund in funds.items():
        rows = []
        for n in NS:
            hm = n * dlt
            for lab, h in (("vs A40", 0.40), ("vs A same-delta", hm)):
                t = bt.monthly_table(spot_h, fund, q, otm=0.0, n_calls=n, h=h)
                sB, sA = bt.stats(t["r_B"], yrs), bt.stats(t["r_A"], yrs)
                bs = bt.bootstrap(t, sims=10000)
                ph = bt.phase_sweep(spot_h, fund, dvol, q, h=h, n_calls=n, otm=0.0)
                def d_apr(a):
                    tt = bt.monthly_table(spot_h, None, q, otm=0.0, n_calls=n, h=h, flat_apr=a)
                    return comp(tt["r_B"]) - comp(tt["r_A"])
                try:
                    be = brentq(d_apr, -0.3, 1.0)
                except ValueError:
                    be = None
                rows.append(dict(n_calls=round(n * 100), delta=round(hm * 100), cmp=lab, A_unhedged=round(h * 100),
                                 B_reinv=sB["reinvest_total"], A_reinv=sA["reinvest_total"],
                                 B_noreinv=sB["no_reinvest_total"], A_noreinv=sA["no_reinvest_total"],
                                 B_dd=sB["max_drawdown"], A_dd=sA["max_drawdown"],
                                 B_worst=sB["worst_month"], A_worst=sA["worst_month"],
                                 prem_m=float((n * t["premium"]).mean()),
                                 P_boot36=float((bs["B_comp"] > bs["A_comp"]).mean()),
                                 P_phase=float((ph["B_comp"] > ph["A_comp"]).mean()),
                                 breakeven_basis=be))
        out[cur][src] = rows
json.dump(out, open(os.path.join(bt.OUT, "atm_vs_perp.json"), "w"), indent=2, default=float)
for cur, r in out.items():
    print(f"\n===== {cur}  avg ATM delta/call {r['atm_delta']:.3f}")
    for src in ("hyperliquid", "deribit"):
        df = pd.DataFrame(r[src])
        for k in ("B_reinv", "A_reinv", "B_noreinv", "A_noreinv", "B_dd", "A_dd", "B_worst", "A_worst", "prem_m"):
            df[k] = (df[k] * 100).round(1)
        df["breakeven_basis"] = df["breakeven_basis"].apply(lambda x: "none" if x is None or x != x else f"{x*100:.1f}%")
        for k in ("P_boot36", "P_phase"):
            df[k] = df[k].round(2)
        print(f"-- funding {src}"); print(df.to_string(index=False))
