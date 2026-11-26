# Upside in a basis book: 40 unhedged vs 100 monthly 5%-OTM calls

Backtest window: **24-Nov-2023 → 26-Nov-2026**. It covers 36 monthly expiries; the
Oct-30 → Nov-27-2026 one is still open and is marked to market.
Spot moves over the window: ETH 2,081 → 1,691 (−18.8%), BTC 37.5k → 60.5k (+61.3%).

| | A: "perp long" via unhedged part | B: full hedge + calls |
|---|---|---|
| spot | 100 | 100 |
| short perp | 60 | 100 |
| options | – | 100 calls, K = 1.05 × spot, monthly expiry (last Fri 08:00 UTC), rolled |
| delta at roll | 40 (linear) | ≈ 43 ETH / 39 BTC (convex) |
| basis earned on | 60 | 100 |

**Inputs (all from real data):**
- **Spot and settlement:** Deribit hourly index.
- **Basis (funding):** Hyperliquid hourly funding, which uses a Binance-style formula with a +11% APR baseline (≈15% realised). Deribit perp funding (≈5–7%) brackets the low end. Derive's own API is geo-blocked from here.
- **Option cost:** the IV of real Deribit taker trades of next-monthly calls struck 3–7% OTM, in the 24h after each roll. Each call is priced off the forward implied by those same trades, plus **+1 vol pt** for a Derive spread and 0.03% fee.

**Reinvesting:** "no reinvest" keeps a fixed notional and withdraws P&L. "Reinvest" resizes to equity every month, so basis income, unhedged gains and realised call payoffs are all redeployed into the basis book.

## Results (3y, % of starting capital)

**ETH**

| basis proxy | mode | pure basis | A 40 unhedged | **B 100 calls** | hybrid 20 + 50 calls |
|---|---|---|---|---|---|
| Hyperliquid (14.8% APR) | reinvest | +55% | +41% | **+126%** | +81% |
| | no reinvest | +45% | +48% | **+110%** | +79% |
| Deribit (5.3% APR) | reinvest | +17% | +19% | **+72%** | +45% |
| | no reinvest | +16% | +30% | **+81%** | +56% |

**BTC**

| basis proxy | mode | pure basis | A 40 unhedged | B 100 calls | hybrid 20 + 50 calls |
|---|---|---|---|---|---|
| Hyperliquid (15.7% APR) | reinvest | +59% | +73% | +83% | +80% |
| | no reinvest | +47% | +63% | +75% | +69% |
| Deribit (7.3% APR) | reinvest | +25% | +50% | +44% | +48% |
| | no reinvest | +22% | +47% | +50% | +49% |

### Risk (Hyperliquid funding)

| | ETH A | ETH B | BTC A | BTC B |
|---|---|---|---|---|
| max drawdown | −29.5% | −34.0% (Aug-25 → Jun-26 premium bleed) | −18.7% | −18.7% |
| worst month | −13.8% | −6.2% | −9.2% | −5.0% |
| best month | +20.7% | +40.6% | +19.6% | +35.0% |
| months B beat A | | 17 / 36 | | 11 / 36 |
| longest B losing streak | | 7 months | | 8 months |

### Option economics

| | ETH | BTC |
|---|---|---|
| avg premium / month (100 calls on 100) | 5.3% of notional | 3.7% of notional |
| avg IV paid vs realised vol | 61.5% vs 63.4% | 47.1% vs 45.9% |
| total premium paid vs payoff received (3y) | 190% vs 255% (1.34×) | 133% vs 161% (1.21×) |

### Robustness

| | ETH | BTC |
|---|---|---|
| B > A across 30 different roll-start days (30d calls priced off DVOL) | 97% (HL) / 83% (Deribit) | 33% / 3% |
| block bootstrap of realised months: P(B > A), 36m | 97% / 93% | 59% / 42% |
| bootstrap P(B > A), 12m | 86% / 79% | 54% / 44% |
| breakeven flat basis APR | none: B wins from −10% to +60% | **≈ 9.9% APR** |
| breakeven extra spread over Deribit IV | +12.5 / +9.9 vol pts | +2.4 / 0 vol pts |

Flat-basis sensitivity (reinvest, A / B):

| basis APR | ETH A | ETH B | BTC A | BTC B |
|---|---|---|---|---|
| 5% | 19% | 72% | 45% | 37% |
| 10% | 31% | 100% | 59% | 59% |
| 15% | 43% | 132% | 74% | 85% |
| 20% | 57% | 169% | 90% | 115% |

Strike sensitivity (ATM / 5% / 10% OTM) is approximate, because it uses the 5%-OTM IV for every strike:
- ETH: B +90% / +126% / +141%
- BTC: B +95% / +83% / +71%

## Today (26-Nov-2026 16:00 UTC)

| | ETH | BTC |
|---|---|---|
| spot / DVOL | 1,691 / 65.4 | 60,548 / 51.8 |
| 5%-OTM 25-Dec call (100 calls) | ≈ 5.2% of notional, delta 0.43 | ≈ 3.8% of notional, delta 0.40 |
| funding, last 30d (HL / Deribit) | 3.9% / 0.9% APR | 7.8% / 2.6% APR |
| B beats A this month if spot moves | > +17% or < −13% | > +14% or < −9% |

## Reproduce

```bash
pip install pandas numpy scipy requests
python3 research/fetch_market_data.py      # public data -> research/data/
python3 research/calls_vs_unhedged_backtest.py --unhedged 0.40 --calls 1.0 --otm 0.05 --extra-vol 0.01
python3 research/extras.py                 # breakevens, hybrid, today's pricing
```

Per-month detail is in `research/output/monthly_<COIN>_<funding>.csv`. The roll quotes (IV paid, forward carry) are in `research/output/roll_quotes_<COIN>.csv`.
