#!/usr/bin/env python3
"""Download the public market data used by calls_vs_unhedged_backtest.py.

Sources (all public, no key needed; Derive's own API is geo-blocked from many
regions, so we use liquid proxies that track it closely):
  * Coinbase       - daily spot candles (BTC-USD, ETH-USD)
  * Deribit        - DVOL 30d implied-vol index (daily)
  * Deribit        - BTC/ETH-PERPETUAL hourly funding history
  * Hyperliquid    - BTC/ETH hourly funding history (Binance-style +11% APR baseline)
  * Deribit        - option trades sampled around each monthly roll, to measure the
                     real IV of ~30d, ~5% OTM calls vs. DVOL (skew calibration)

Writes CSVs into ./data next to this script.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)

START = datetime(2023, 9, 1, tzinfo=timezone.utc)
END = datetime.now(timezone.utc)
S = requests.Session()


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def get(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 + 2 * i)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def coinbase_daily(product: str) -> pd.DataFrame:
    rows = []
    t = START
    while t < END:
        t2 = min(t + timedelta(days=290), END)
        d = get(
            f"https://api.exchange.coinbase.com/products/{product}/candles",
            {"granularity": 86400, "start": t.isoformat(), "end": t2.isoformat()},
        )
        rows += d
        t = t2
        time.sleep(0.3)
    df = pd.DataFrame(rows, columns=["time", "low", "high", "open", "close", "volume"])
    df["date"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_localize(None)
    return df.drop_duplicates("date").sort_values("date")[["date", "open", "high", "low", "close"]]


def deribit_dvol(cur: str) -> pd.DataFrame:
    rows = []
    end = ms(END)
    while True:
        d = get(
            "https://www.deribit.com/api/v2/public/get_volatility_index_data",
            {"currency": cur, "start_timestamp": ms(START), "end_timestamp": end, "resolution": "1D"},
        )["result"]
        data = d["data"]
        rows += data
        if not d.get("continuation") or not data:
            break
        end = d["continuation"]
        if end <= ms(START):
            break
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_localize(None)
    return df.drop_duplicates("date").sort_values("date")[["date", "close"]].rename(columns={"close": "dvol"})


def deribit_funding(inst: str) -> pd.DataFrame:
    rows = []
    t = START
    while t < END:
        t2 = min(t + timedelta(days=30), END)
        d = get(
            "https://www.deribit.com/api/v2/public/get_funding_rate_history",
            {"instrument_name": inst, "start_timestamp": ms(t), "end_timestamp": ms(t2)},
        )["result"]
        rows += d
        t = t2
        time.sleep(0.2)
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_localize(None)
    # interest_1h is the realised hourly funding paid long->short
    return df.drop_duplicates("time").sort_values("time")[["time", "interest_1h", "index_price"]]


def hl_funding(coin: str) -> pd.DataFrame:
    rows = []
    t = ms(START)
    endms = ms(END)
    while t < endms:
        for i in range(8):
            r = S.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "fundingHistory", "coin": coin, "startTime": t},
                timeout=30,
            )
            if r.status_code != 429:
                break
            time.sleep(3 * (i + 1))
        r.raise_for_status()
        d = r.json()
        if not d:
            break
        rows += d
        nt = d[-1]["time"] + 1
        if nt <= t:
            break
        t = nt
        time.sleep(1.0)
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True).dt.tz_localize(None)
    df["fundingRate"] = df["fundingRate"].astype(float)
    return df.drop_duplicates("time").sort_values("time")[["time", "fundingRate"]]


def last_friday(year: int, month: int) -> datetime:
    d = datetime(year + (month == 12), month % 12 + 1, 1, 8, tzinfo=timezone.utc) - timedelta(days=1)
    while d.weekday() != 4:
        d -= timedelta(days=1)
    return d


def monthly_expiries(start: datetime, end: datetime) -> list[datetime]:
    out, y, m = [], start.year, start.month
    while True:
        e = last_friday(y, m)
        if e > end + timedelta(days=40):
            break
        if e >= start:
            out.append(e)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def deribit_inst_trades(name: str, t0: datetime, t1: datetime) -> list[dict]:
    try:
        d = get(
            "https://history.deribit.com/api/v2/public/get_last_trades_by_instrument_and_time",
            {"instrument_name": name, "start_timestamp": ms(t0), "end_timestamp": ms(t1),
             "count": 1000, "include_old": "true"},
            tries=2,
        )
    except Exception:  # noqa: BLE001  (unlisted strike -> HTTP 400)
        return []
    return (d.get("result") or {}).get("trades", []) if isinstance(d, dict) else []


def deribit_option_iv_samples(cur: str, lo: float = 1.02, hi: float = 1.09) -> pd.DataFrame:
    """At every monthly roll (last Friday 08:00 UTC) pull real Deribit trades, in the
    24h after the roll, of next-monthly calls struck at strike/spot in [lo, hi]
    (default ~2-9% OTM). Gives the actual IV a buyer paid (taker-buy trades ~= ask)."""
    fund = pd.read_csv(os.path.join(DATA, f"funding_deribit_{cur}.csv"), parse_dates=["time"])
    fund = fund.set_index(fund["time"].dt.floor("h"))["index_price"]
    exps = monthly_expiries(datetime(2023, 10, 1, tzinfo=timezone.utc), END)
    grids = [50, 100] if cur == "ETH" else [500, 1000]
    out = []
    for roll, nxt in zip(exps[:-1], exps[1:]):
        if roll > END:
            break
        key = pd.Timestamp(roll.replace(tzinfo=None))
        if key not in fund.index:
            continue
        spot = float(fund.loc[key])
        code = f"{nxt.day}{nxt.strftime('%b').upper()}{nxt.strftime('%y')}"
        strikes = sorted({k for g in grids for k in range(int(spot * lo // g * g), int(spot * (hi + 0.01)), g)
                          if spot * lo <= k <= spot * hi})
        for k in strikes:
            name = f"{cur}-{code}-{k}-C"
            for tr in deribit_inst_trades(name, roll, roll + timedelta(hours=24)):
                if not tr.get("iv"):
                    continue
                out.append({
                    "roll": roll.replace(tzinfo=None), "expiry": nxt.replace(tzinfo=None),
                    "instrument": name, "strike": k, "spot_at_roll": spot,
                    "time": datetime.fromtimestamp(tr["timestamp"] / 1000, tz=timezone.utc).replace(tzinfo=None),
                    "index_price": tr["index_price"], "moneyness": k / tr["index_price"],
                    "iv": float(tr["iv"]), "direction": tr["direction"], "amount": float(tr["amount"]),
                    "price_coin": float(tr["price"]),
                })
            time.sleep(0.1)
        print(f"  {cur} roll {roll.date()} -> {code}: {sum(1 for o in out if o['roll'] == roll.replace(tzinfo=None))} trades", file=sys.stderr)
    return pd.DataFrame(out)


def deribit_surface_trades(cur: str) -> pd.DataFrame:
    """All option trades (every expiry/strike) in the 24h after 08:00 UTC on each
    monthly date (last Friday) plus the last 24h, filtered to OTM-ish strikes
    (strike/spot 0.75-1.5) and 5-460 days to expiry. Used to fit a monthly IV
    surface (moneyness x tenor) for pricing and marking 1-12M options."""
    dates = [d for d in monthly_expiries(datetime(2023, 11, 1, tzinfo=timezone.utc), END) if d < END]
    windows = [(d, d + timedelta(hours=24)) for d in dates] + [(END - timedelta(hours=24), END)]
    out = []
    for t0, t1 in windows:
        t = ms(t0)
        n = 0
        while True:
            d = get(
                "https://history.deribit.com/api/v2/public/get_last_trades_by_currency_and_time",
                {"currency": cur, "kind": "option", "start_timestamp": t, "end_timestamp": ms(t1),
                 "count": 1000, "include_old": "true", "sorting": "asc"},
            )["result"]
            trs = d["trades"]
            for tr in trs:
                parts = tr["instrument_name"].split("-")
                if len(parts) != 4 or not tr.get("iv"):
                    continue
                try:
                    exp = datetime.strptime(parts[1], "%d%b%y").replace(hour=8, tzinfo=timezone.utc)
                except ValueError:
                    continue
                k = float(parts[2])
                ts = datetime.fromtimestamp(tr["timestamp"] / 1000, tz=timezone.utc)
                dte = (exp - ts).total_seconds() / 86400
                m = k / tr["index_price"]
                if not (5 <= dte <= 460 and 0.75 <= m <= 1.5):
                    continue
                out.append({"date": t0.replace(tzinfo=None), "time": ts.replace(tzinfo=None),
                            "instrument": tr["instrument_name"], "type": parts[3], "strike": k,
                            "expiry": exp.replace(tzinfo=None), "dte": dte, "index_price": tr["index_price"],
                            "moneyness": m, "iv": float(tr["iv"]), "price_coin": float(tr["price"]),
                            "amount": float(tr["amount"]), "direction": tr["direction"]})
                n += 1
            if not trs or not d.get("has_more"):
                break
            t = trs[-1]["timestamp"] + 1
            time.sleep(0.05)
        print(f"  {cur} surface {t0:%Y-%m-%d}: {n} trades kept", file=sys.stderr)
    return pd.DataFrame(out)


def main():
    force = "--force" in sys.argv
    for prod, cur in (("BTC-USD", "BTC"), ("ETH-USD", "ETH")):
        jobs = [
            (f"spot_{cur}.csv", lambda: coinbase_daily(prod)),
            (f"dvol_{cur}.csv", lambda: deribit_dvol(cur)),
            (f"funding_deribit_{cur}.csv", lambda: deribit_funding(f"{cur}-PERPETUAL")),
            (f"funding_hl_{cur}.csv", lambda: hl_funding(cur)),
            (f"optiv_{cur}.csv", lambda: deribit_option_iv_samples(cur)),
            # ATM band (strike 95-101.9% of spot) and 10%-OTM band (109.1-113%)
            (f"optiv_atm_{cur}.csv", lambda: deribit_option_iv_samples(cur, 0.95, 1.019)),
            (f"optiv_otm10_{cur}.csv", lambda: deribit_option_iv_samples(cur, 1.091, 1.13)),
            (f"surface_{cur}.csv", lambda: deribit_surface_trades(cur)),
        ]
        for fname, fn in jobs:
            path = os.path.join(DATA, fname)
            if os.path.exists(path) and not force:
                continue
            print(f"fetch {fname}", file=sys.stderr)
            fn().to_csv(path, index=False)
    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
