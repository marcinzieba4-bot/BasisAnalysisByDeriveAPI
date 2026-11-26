"""Compare fitted short-dated surface IV with actual Deribit trade IVs near the
1W/2W/4W x ATM/5/10/15% OTM points (calls, taker buys and all)."""
import numpy as np, pandas as pd
import weekly_backtest as wb
from tenor_strike_backtest import yfrac
for cur in ("ETH", "BTC"):
    tr = pd.read_csv(f"data/wsurface_{cur}.csv", parse_dates=["time", "expiry"])
    tr["date"] = pd.to_datetime(tr["date"], format="mixed")
    grid, S, f, surf, rv = wb.build(cur)
    rows = []
    for k, g in enumerate(grid[:-1]):
        d = tr[(tr["date"] == g) & (tr["type"] == "C")]
        for t in (1, 2, 4):
            for otm in (0.0, 0.05, 0.10, 0.15):
                m = d[(d["dte"].between(7 * t - 2, 7 * t + 1)) & (d["moneyness"].between(1 + otm - 0.015, 1 + otm + 0.015))]
                if len(m) < 3:
                    continue
                buy = m[m["direction"] == "buy"]
                ivs = []
                for r in m.itertuples():
                    ivs.append(surf[g].iv(r.strike, r.dte / 365, r.index_price)[0] * 100)
                m = m.assign(fit=ivs)
                rows.append(dict(t=t, otm=otm, trade=np.average(m.iv, weights=m.amount),
                                 trade_buy=np.average(buy.iv, weights=buy.amount) if len(buy) else np.nan,
                                 fit=np.average(m.fit, weights=m.amount), n=len(m)))
    df = pd.DataFrame(rows)
    g = df.groupby(["t", "otm"]).agg(obs=("n", "size"), trade_iv=("trade", "mean"), buy_iv=("trade_buy", "mean"), fit_iv=("fit", "mean"))
    g["fit_minus_buy"] = g["fit_iv"] - g["buy_iv"]
    print(cur); print(g.round(1).to_string())
    g[["fit_minus_buy"]].reset_index().to_csv(f"output/wing_bias_{cur}.csv", index=False)
