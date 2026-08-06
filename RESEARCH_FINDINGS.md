# PredictX Research Findings — Win/Loss Pattern Analysis
# Generated from predictx_memory.sqlite3 | prediction_history table
# Total graded: 1,192 | Wins: 857 (71.9%) | Losses: 335 (28.1%) | Void: 14

---

## 1. OVERALL PERFORMANCE

| Metric | Value |
|---|---|
| Total graded (excl void) | 1,192 |
| Wins | 857 (71.9%) |
| Losses | 335 (28.1%) |
| Prematch win rate | 71.1% |
| Live win rate | 74.5% |
| Live loss rate | 25.2% |
| Prematch loss rate | 29.1% |


---

## 2. PICK TYPE

| Pick Type | Wins | Total | Win Rate | Loss Rate |
|---|---|---|---|---|
| double_chance | 815 | 1,126 | 72.4% | 27.6% |
| goals | 16 | 27 | 59.3% | 40.7% |
| live_total_goals | 10 | 11 | 90.9% | 9.1% |
| live_match_winner | 11 | 15 | 73.3% | 26.7% |
| match_result | 5 | 13 | 38.5% | **61.5%** |

**Key insight:** match_result (1X2) is a trap — 61.5% loss rate. Avoid it.
goals market also underperforms at 40.7% loss rate.
live_total_goals is the hidden gem — 90.9% win rate (small sample but strong).

---

## 3. SELECTION

| Selection | Win Rate | Loss Rate | Total |
|---|---|---|---|
| Home or Away | **83.8%** | 16.2% | 265 |
| Home or Draw | 73.4% | 26.4% | 409 |
| Away or Draw | 65.2% | **35.4%** | 452 |
| Under 3.5 goals | 81.8% | 18.2% | 11 |

**Key insight:** "Away or Draw" is the weakest selection — 35.4% loss rate, nearly 10 points worse than "Home or Draw".
"Home or Away" is the strongest — only 16.2% loss rate.
Rule: Require higher confidence (70+) for "Away or Draw" picks.

---

## 4. CONFIDENCE

| Confidence | Win Rate | Loss Rate | Total |
|---|---|---|---|
| 50-59% | 56.0% | **44.0%** | 25 |
| 60-69% | 67.1% | 32.9% | 362 |
| 70-79% | 74.6% | 25.4% | 786 |
| 80-89% | 73.7% | 26.3% | 19 |

**Fine-grained confidence findings:**
- conf 59: 0% win rate (4 picks) — complete failure
- conf 63: 47% win rate — below coin flip
- conf 68: 67% win rate (238 picks) — large volume, below average
- conf 74: 75% win rate (684 picks) — bread and butter
- conf 79: 100% win rate (4 picks)
- conf 82: 79% win rate (14 picks)

**Key insight:** The 60-66 confidence band is noisy and unreliable — treat it like a coin flip.
Minimum viable confidence threshold: 67. Ideal zone: 70-79.
The 50-59 band has a 44% loss rate — should be blocked entirely.

---

## 5. COUNTRY

### Best Countries (lowest loss rate, min 10 graded)
| Country | Win Rate | Loss Rate | Total |
|---|---|---|---|
| Austria | 100% | 0% | 12 |
| India | 94.1% | 5.9% | 17 |
| Uzbekistan | 93.3% | 6.7% | 15 |
| Switzerland | 90.0% | 10.0% | 10 |
| Norway | 86.5% | 13.5% | 37 |
| Australia | 86.1% | 13.9% | 79 |
| Germany Amateur | 83.3% | 16.7% | 12 |
| Sweden | 81.8% | 18.2% | 22 |
| Ireland | 80.0% | 20.0% | 10 |
| Kazakhstan | 80.0% | 20.0% | 10 |
| Bulgaria | 80.0% | 20.0% | 10 |

### Worst Countries (highest loss rate, min 10 graded)
| Country | Win Rate | Loss Rate | Total |
|---|---|---|---|
| Bolivia | 45.5% | **54.5%** | 11 |
| Uruguay | 45.5% | **54.5%** | 11 |
| Romania | 50.0% | **50.0%** | 12 |
| China | 53.6% | 46.4% | 28 |
| Russia | 56.8% | 43.2% | 44 |
| Peru | 57.1% | 42.9% | 14 |
| Argentina | 60.0% | 40.0% | 70 |
| Ecuador | 60.0% | 40.0% | 10 |
| World (mixed) | 65.9% | 34.1% (excl void) | 41 |
| Republic of Korea | 65.4% | 34.6% | 26 |

**Key insight:** Bolivia, Uruguay, Romania, China, Russia are money-losers.
These 5 countries alone account for a disproportionate share of losses.
Rule: Block or require conf 75+ for Bolivia, Uruguay, Romania, China, Russia.

---

## 6. LEAGUE

### Perfect Win Rate Leagues (100%, min 5 graded)
- Finland Kakkonen (8/8)
- Norway 2nd Division Group 2 (8/8)
- Uzbekistan Pro Liga (11/11)
- Scotland Premier League Cup, Women (11/11)
- Brazil Copa do Brasil (7/7)
- Armenia Premier League (7/7)
- Norway Eliteserien (6/6)
- Austria Bundesliga (6/6)
- India Calcutta Premier Div. (6/6)
- Lebanon Premier League (6/6)
- India Durand Cup (6/6)
- Russia Premier League (5/5)
- China League 1 (5/5)
- Slovakia Superliga (5/5)
- Switzerland Super League (5/5)
- Australia Victoria Premier League 1 (5/5)

### Worst Leagues (highest loss rate, min 5 graded)
| League | Loss Rate | Losses/Total |
|---|---|---|
| Scotland League Cup | **100%** | 5/5 |
| Argentina Primera Division, Women | 83% | 5/6 |
| Russia Russian Cup | 80% | 8/10 |
| Finland Kolmonen | 71% | 5/7 |
| China Chinese Super League | 67% | 4/6 |
| Argentina Primera LPF | 62% | 10/16 |
| Uruguay Primera Division | 60% | 3/5 |
| USA National Womens Soccer League | 60% | 3/5 |
| International Africa Cup of Nations, Women | 57% | 4/7 |
| China League 2 | 55% | 6/11 |
| Bolivia Division Profesional | 50% | 5/10 |
| Romania Superliga | 50% | 3/6 |
| USA MLS | 47% | 8/17 |
| Argentina Primera Nacional | 46% | 6/13 |
| World UEFA Europa League Qualification | 44% | 4/9 |

**Key insight:** Scotland League Cup, Russia Russian Cup, Argentina women's football,
China Super League, and Argentina Primera LPF are consistent loss generators.
Rule: Block Scotland League Cup, Russia Russian Cup entirely.
Require conf 75+ for Argentina Primera LPF, China CSL, USA MLS.

---

## 7. SOURCE

| Source | Win Rate | Loss Rate | Total |
|---|---|---|---|
| enriched_ensemble | 71.9% | 28.1% | 1,146 |
| sportybet_market_signal | 90.9% | 9.1% | 11 |
| competition_special:brasileirao | 100% | 0% | 3 |
| competition_special:copa-sudamericana | 100% | 0% | 2 |
| competition_special:europa-league | 55.6% | **44.4%** | 9 |
| competition_special:champions-league | 66.7% | 33.3% | 6 |
| competition_special:liga-profesional | 60.0% | 40.0% | 5 |

**Key insight:** sportybet_market_signal has a 90.9% win rate — trust it more.
competition_special:europa-league is underperforming at 44.4% loss rate.
enriched_ensemble is the backbone but has room to improve with better filters.

---

## 8. ODDS RANGES

### Favorite Odds
| Favorite Odds | Win Rate | Loss Rate |
|---|---|---|
| 1.01-1.29 | 76.4% | 23.6% |
| 1.30-1.49 | 75.0% | 25.0% |
| 1.50-1.69 | **80.0%** | **20.0%** ← best |
| 1.70-1.99 | 74.1% | 25.9% |
| 2.00-2.49 | 69.8% | 30.2% |
| 2.50-2.99 | 65.4% | **34.6%** ← worst |

**Key insight:** Favorite odds 1.50-1.69 is the sweet spot (80% win rate).
Avoid picks where the favorite is priced 2.50+ — loss rate climbs to 34.6%.

### Home Odds
| Home Odds | Win Rate | Loss Rate |
|---|---|---|
| 1.30-1.49 | **94.1%** | 5.9% ← best |
| 1.50-1.69 | 84.2% | 15.8% |
| 1.70-1.99 | 79.4% | 20.6% |
| 2.00-2.49 | 75.3% | 24.7% |
| 2.50-2.99 | 62.1% | **37.9%** ← worst |
| 3.00-3.99 | 69.1% | 30.9% |
| 4.00-5.99 | 74.2% | 25.8% |
| 6.00+ | 76.3% | 23.7% |

**Key insight:** Home odds 2.50-2.99 is the single worst home odds range — 37.9% loss rate.
Interestingly, very high home odds (6.00+) still win 76.3% — double chance covers the underdog.

### Draw Odds
| Draw Odds | Win Rate | Loss Rate |
|---|---|---|
| 1.30-1.49 | 0% | **100%** ← avoid |
| 1.70-1.99 | 64.5% | **35.5%** ← danger zone |
| 2.00-2.49 | 70.5% | 29.5% |
| 2.50-2.99 | 75.5% | 24.5% |
| 3.00-3.99 | **77.7%** | 22.3% |
| 4.00-5.99 | 77.4% | 22.6% |
| 6.00+ | 78.1% | 21.9% |

**Key insight:** Higher draw odds = better win rate. When draw is priced low (1.30-1.99),
the game is too tight and unpredictable. Rule: Avoid matches where draw odds < 2.00.

### Away Odds
| Away Odds | Win Rate | Loss Rate |
|---|---|---|
| 1.70-1.99 | **81.8%** | 18.2% ← best |
| 6.00+ | 81.1% | 18.9% |
| 1.01-1.29 | 78.1% | 21.9% |
| 2.00-2.49 | 72.1% | 27.9% |
| 2.50-2.99 | 66.7% | **33.3%** ← worst |
| 3.00-3.99 | 68.1% | 31.9% |
| 1.30-1.49 | 64.7% | 35.3% |

**Key insight:** Away odds 2.50-2.99 and 1.30-1.49 are danger zones (33-35% loss rate).
Strong away favorites (1.70-1.99) and big underdogs (6.00+) both perform well.

### Favorite Side
| Favorite Side | Win Rate | Loss Rate |
|---|---|---|
| Home favorite | **78.6%** | 21.4% |
| Away favorite | 75.1% | 24.9% |
| Draw favorite | 67.5% | **32.5%** |

**Key insight:** When the draw is the market favorite (draw odds shorter than home and away),
loss rate jumps to 32.5%. These are the most unpredictable matches in the dataset.
Rule: When draw is favorite, require conf 72+ or skip entirely.

---

## 9. SCORE PATTERNS

### In Wins (top scores)
1-1 (8.7%), 1-0 (7.9%), 0-0 (7.4%), 0-1 (7.4%), 1-2 (7.3%), 2-1 (6.0%), 2-0 (5.8%)
- Draws (0-0, 1-1, 2-2) = ~22% of winning scores
- Low-scoring games dominate wins

### In Losses (top scores)
1-0 (13.4%), 1-2 (9.9%), 2-1 (9.0%), 1-1 (7.8%), 3-1 (6.6%), 0-1 (6.3%)
- 1-0 is the #1 loss score — a single goal deciding the game against the pick
- High-scoring losses (3+ goals) = 23% of losses

### Goals Distribution
| Goals | Win % | Loss % |
|---|---|---|
| 0 | 7.3% | 3.9% |
| 1 | 15.3% | **19.7%** |
| 2 | 18.1% | 16.4% |
| 3 | **20.2%** | **23.0%** |
| 4 | 15.9% | 17.3% |
| 5 | 9.6% | 10.1% |
| 6+ | 12.6% | 9.6% |

**Key insight:** 1-goal and 3-goal games are disproportionately lossy.
1-goal games (19.7% of losses vs 15.3% of wins) — a single goal swing kills double chance.
3-goal games (23% of losses) — high-scoring games are volatile.

---

## 10. CONSOLIDATED RULES (derived from research)

### BLOCK (high loss rate, consistent pattern)
1. match_result (1X2) pick type — 61.5% loss rate
2. Confidence below 60 — 44% loss rate
3. Draw odds below 2.00 — 35.5%+ loss rate
4. Favorite odds 2.50+ — 34.6% loss rate
5. Home odds 2.50-2.99 — 37.9% loss rate
6. Scotland League Cup — 100% loss rate
7. Russia Russian Cup — 80% loss rate
8. Bolivia, Uruguay — 54.5% loss rate each

### CAUTION (require conf 72+)
1. "Away or Draw" selection — 35.4% loss rate
2. Draw as market favorite — 32.5% loss rate
3. China (any league) — 46.4% loss rate
4. Russia (any league) — 43.2% loss rate
5. Argentina Primera LPF — 62% loss rate
6. USA MLS — 47% loss rate
7. Confidence 60-66 band — noisy, treat as 50/50
8. competition_special:europa-league — 44.4% loss rate

### TRUST MORE (low loss rate, strong pattern)
1. "Home or Away" selection — 16.2% loss rate
2. live_total_goals — 9.1% loss rate
3. sportybet_market_signal source — 9.1% loss rate
4. Home odds 1.30-1.69 — 5.9-15.8% loss rate
5. Draw odds 3.00+ — 21-22% loss rate
6. Favorite odds 1.50-1.69 — 20% loss rate
7. Away odds 1.70-1.99 — 18.2% loss rate
8. Norway, Australia, India, Uzbekistan, Austria — all under 14% loss rate
9. Confidence 74 — 75% win rate, highest volume sweet spot
10. competition_special:brasileirao, copa-sudamericana — 100% win rate

---

## 11. OPTIMAL PICK PROFILE (highest probability of win)

A pick matching ALL of these criteria has the highest expected win rate:
- Selection: "Home or Away" or "Home or Draw"
- Confidence: 70-79 (ideal: 74)
- Prediction mode: live (preferred) or prematch
- Favorite side: home or away (NOT draw)
- Favorite odds: 1.50-1.99
- Draw odds: 2.50+
- Home odds: NOT in 2.50-2.99 range
- Country: Norway, Australia, India, Uzbekistan, Austria, Switzerland, Sweden
- Source: sportybet_market_signal > enriched_ensemble
- Avoid: Bolivia, Uruguay, Romania, China, Russia, Argentina LPF, Scotland Cup, Russia Cup

---

## 12. DATA NOTES

- Database: predictx/data/predictx_memory.sqlite3
- Table: prediction_history
- Odds extracted from: signals_json -> odds_profile (home_odds, draw_odds, away_odds, favorite_odds, favorite_side)
- 54 records had no odds profile (4.5% of graded)
- Analysis scripts: analyze_wins.py, pattern_deep.py, odds_analysis.py, loss_analysis.py
- Last updated: based on 1,192 graded predictions
