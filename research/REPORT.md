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

## Strike choice: ATM vs 5% OTM vs 10% OTM (each priced with its own real Deribit IV)

On average the IV paid was almost the same at all three strikes: ETH ≈ 60.7%, BTC ≈ 46.3%. So any difference comes from the shape of the monthly moves, not from skew.

**Delta-matched** (same 40 starting delta as A), compounded 3y:

| | ATM (~73 calls) | 5% OTM (~95–103) | 10% OTM (~123–158) | A 40 unhedged |
|---|---|---|---|---|
| ETH, HL funding | +92% | +133% | **+183%** | +41% |
| ETH, Deribit funding | +46% | +77% | **+115%** | +19% |
| BTC, HL funding | **+89%** | +84% | +76% | +73% |
| BTC, Deribit funding | +48% | +45% | +38% | **+50%** |
| ETH payoff / premium | 1.22× | 1.38× | 1.58× | |
| BTC payoff / premium | 1.18× | 1.21× | 1.24× | |
| P(ATM beats 5% OTM), bootstrap 36m | ETH 0% · BTC 60% | | | |

**Same count** (100 calls), HL funding:

| | delta | premium / month | 3y reinvest | maxDD |
|---|---|---|---|---|
| ETH ATM | 0.55 | 7.4% | +89% | −44% |
| ETH 5% OTM | 0.43 | 5.3% | +125% | −34% |
| ETH 10% OTM | 0.33 | 3.7% | **+139%** | **−26%** |
| BTC ATM | 0.54 | 5.8% | +93% | −31% |
| BTC 5% OTM | 0.39 | 3.7% | +83% | −19% |
| BTC 10% OTM | 0.27 | 2.3% | +69% | −17% |

**Budget-matched BTC ATM** (≈62 ATM calls for the same monthly spend as 100 × 5% OTM): +89% (HL) / +48% (Deribit). It had the lowest drawdown of any BTC call variant (−16% / −18%).

**Reading:**
- **ETH:** further OTM was better. ETH's big months (+33%, +43%, +47%, +49%) reward convexity per premium dollar.
- **BTC:** ATM is marginally better than 5% OTM. That comes from a smoother path, because BTC had many +6% to +13% months where only ATM pays. The edge per premium dollar is about the same at every strike, so BTC's strike choice is mostly noise.
- **ATM at 100 calls** is about 55 delta, i.e. more exposure than A's 40, plus a much deeper drawdown.

Reproduce: `python3 research/strike_compare.py`. Per-month detail is in `output/strike_monthly_<COIN>_<funding>.csv`.

## ATM calls (70–100) vs the perp long (unhedged part)

One ATM call is about 0.545 delta. So 40 delta ≈ 73 calls, 80 calls ≈ 44 delta, and 90 calls ≈ 49 delta.
Results are 3y with reinvesting. "P boot" is the chance the calls beat A over 36 months in the block bootstrap. "P phase" is the share of the 30 roll-start days on which the calls beat A.

| | ATM calls | B (calls) | A40 | P boot vs A40 / same-delta A | P phase | B maxDD / A maxDD | B worst month / A worst month |
|---|---|---|---|---|---|---|---|
| ETH, basis ~15% | 73 / 80 / 90 | +90% / +90% / +90% | +41% | 0.96 / 0.92 / 0.85 · 0.95–0.96 | 1.00 | −33 to −40% / −30% | −6 to −7% / −14% |
| ETH, basis ~5% | 73 / 80 / 90 | +44% / +45% / +45% | +19% | 0.86 / 0.81 / 0.74 · 0.85–0.86 | 0.90 | −36 to −43% / −31% | −7 to −8% / −14% |
| BTC, basis ~15% | 73 / 80 / 90 | +89% / +90% / +92% | +73% | 0.71 / 0.70 / 0.67 · 0.70–0.71 | 0.40–0.43 | −22 to −28% / −19% | −5 to −6% / −9% |
| BTC, basis ~7% | 73 / 80 / 90 | +48% / +50% / +51% | +50% | 0.47 / 0.49 / 0.50 · 0.46–0.48 | 0.03–0.13 | −24 to −30% / −20% | −7 to −8% / −10% |

**Breakeven flat basis (monthly-expiry path):**
- ETH: none. The calls win even at −10% APR.
- BTC: ≈ 7.5–8% APR. The phase sweep shows the BTC edge depends on roll timing.

**Sizing:** going above about 75–80 ATM calls adds almost nothing to compounded return, and each +10 calls deepens the drawdown by about 2–3 pts.

Reproduce: `python3 research/atm_vs_perp.py`.

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
