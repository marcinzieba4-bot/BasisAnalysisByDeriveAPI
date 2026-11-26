"""Breakevens, hybrid and today's pricing snapshot (uses calls_vs_unhedged_backtest)."""
import json, math, os
import numpy as np, pandas as pd
from scipy.optimize import brentq
import calls_vs_unhedged_backtest as bt

out = {}
for cur in ("ETH", "BTC"):
    spot_h, funds, dvol, opt = bt.load(cur)
    q = bt.roll_quotes(opt, dvol, spot_h)
    yrs = (spot_h.index[-1] - bt.FIRST_ROLL).days / 365
    comp = lambda r: float(np.prod(1 + r) - 1)
    res = {}
    # breakeven flat basis APR (compounded 3y A == B)
    def diff_apr(a):
        t = bt.monthly_table(spot_h, None, q, flat_apr=a)
        return comp(t.r_B) - comp(t.r_A)
    try:
        res["breakeven_basis_apr"] = brentq(diff_apr, -0.10, 0.60)
    except ValueError:
        res["breakeven_basis_apr"] = None
    for src, fund in funds.items():
        def diff_vol(ev):
            t = bt.monthly_table(spot_h, fund, q, extra_vol=ev)
            return comp(t.r_B) - comp(t.r_A)
        try:
            res[f"breakeven_extra_vol_{src}"] = brentq(diff_vol, -0.10, 0.30)
        except ValueError:
            res[f"breakeven_extra_vol_{src}"] = None
        t = bt.monthly_table(spot_h, fund, q)
        rH = 0.5 * (t.r_A + t.r_B)  # 20 unhedged + 50 calls
        res[f"hybrid_{src}"] = bt.stats(rH, yrs)
        # path: worst run of consecutive losing B months and cumulative bleed
        lose = (t.r_B < 0).astype(int)
        runs = lose.groupby((lose != lose.shift()).cumsum()).sum()
        res[f"B_longest_losing_streak_{src}"] = int(runs.max())
        eqB = (1 + t.r_B).cumprod(); eqA = (1 + t.r_A).cumprod()
        for nm, eq in (("A", eqA), ("B", eqB)):
            tr = (eq / eq.cummax() - 1).idxmin()
            res[f"{nm}_maxDD_window_{src}"] = [str(eq[:tr].idxmax().date()), str(tr.date()),
                                               float(eq[tr] / eq[:tr].max() - 1)]
        res[f"months_up_gt15_{src}"] = int((t.move > 0.15).sum())
        res[f"months_dn_lt10_{src}"] = int((t.move < -0.10).sum())
    # today's snapshot
    now = spot_h.index[-1]
    S = float(spot_h.iloc[-1])
    dv = float(dvol.iloc[-1])
    skew = float(q["skew_vs_dvol"].median())
    iv = dv + skew + 0.01
    exp = pd.Timestamp("2026-12-25 08:00")  # next monthly after 27-Nov-2026
    T = (exp - now).total_seconds() / (365 * 86400)
    carry = float(q["carry_apr"].iloc[-6:].median())
    F = S * math.exp(carry * T)
    K = S * 1.05
    prem = bt.bs_call(F, K, T, iv) / S + 0.0003
    delta = bt.bs_delta(F, K, T, iv)
    snap = {"time": str(now), "spot": S, "dvol": dv, "iv_assumed": iv, "T_days": T * 365,
            "prem_pct_spot": prem, "delta_per_call": delta, "carry": carry,
            "premium_100calls_vs_basis_forgone_on_40": None}
    for src, fund in funds.items():
        f30 = float(fund.loc[now - pd.Timedelta(days=30):].sum() * 365 / 30)
        f90 = float(fund.loc[now - pd.Timedelta(days=90):].sum() * 365 / 90)
        snap[f"funding_apr_30d_{src}"] = f30
        snap[f"funding_apr_90d_{src}"] = f90
    snap["avg_iv_paid_3y"] = float(q.loc[q.index >= bt.FIRST_ROLL, "iv_paid"].mean() + 0.01)
    tH = bt.monthly_table(spot_h, funds["hyperliquid"], q)
    snap["avg_realised_vol_3y"] = float(tH["realised_vol"].mean())
    # monthly move needed for B to beat A this month (using 30d HL funding)
    fm = snap["funding_apr_30d_hyperliquid"] * T
    h = 0.4
    up = brentq(lambda x: (fm - prem + max(x - 0.05, 0)) - (h * x + (1 - h) * fm), 0.05, 1.0)
    dn = brentq(lambda x: (fm - prem) - (h * x + (1 - h) * fm), -1.0, 0.05)
    snap["B_beats_A_if_move_above"] = up
    snap["B_beats_A_if_move_below"] = dn
    res["today"] = snap
    out[cur] = res
os.makedirs(bt.OUT, exist_ok=True)
json.dump(out, open(os.path.join(bt.OUT, "extras.json"), "w"), indent=2, default=float)
print(json.dumps(out, indent=1, default=lambda x: round(float(x), 4)))
