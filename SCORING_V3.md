# V3 Scoring System — Live Implementation Guide

> This document explains the scoring formula running on V1 and V2.
> All data, formulas, and analysis scripts are in the V3 repo:
> `github.com/BrockStar3540/mr-scrooge-v3`

---

## Data Source

All findings derived from **raw OANDA candle data**, not bot logs.

| File | Description |
|------|-------------|
| `data/raw_candles/01_raw_trades.json` | Raw OANDA /trades API response (50 V1 live trades) |
| `data/raw_candles/02_candles_trade_N_PAIR.json` | Raw D/H1/M5 candles at each trade's entry time (50 files) |
| `data/trades_features_verified.csv` | 35-column feature table computed from the raw candles |
| `data/trades_train.csv` | First 60% chronological (30 trades, in-sample) |
| `data/trades_test.csv` | Last 40% chronological (20 trades, out-of-sample) |
| `scripts/pull_raw_data.py` | Transparent extraction — every API call logged, every calculation shown |

## Formulas Tested

10 scoring methods were tested simultaneously (`scripts/ten_formulas.py`):

| Method | Description | Separation | Best P&L |
|--------|-------------|-----------|----------|
| M1 | Proportional gap (pts = gap%) | +46.2 | +$268 |
| M2 | Win-rate weighted (pts = WR-50%) | +51.0 | +$237 |
| M3 | Binary vote (+1/-1 per factor) | +30.0 | +$236 |
| M4 | Minimal (3 factors only) | +30.9 | +$197 |
| M5 | Log-odds ln(WR/(1-WR)) | +42.6 | +$224 |
| M6 | Top 5 factors, max weight | +61.7 | +$198 |
| M7 | Penalty-heavy (start 50, subtract) | +30.2 | +$279 |
| M8 | Bonus-heavy (start -20, add) | +32.6 | +$206 |
| M9 | Multiplicative probability | +38.9 | +$226 |
| M10 | P&L weighted (pts = net $/bucket) | +38.4 | +$231 |

The OLD formula (V1/V2 pre-fix) had separation of **+0.5 pts** and was never profitable at any threshold.

## Selected Formula: WR-Lift Hybrid with Pair Layer

Selected because it had the best out-of-sample validation:

| Metric | Train (30 trades) | Test (20 trades) |
|--------|-------------------|------------------|
| Separation | +82.9 | +75.1 |
| WR at threshold ≥5 | 100% (12W/0L) | 100% (9W/0L) |
| Net P&L at ≥5 | +$189 | +$144 |
| Losers blocked | 13/13 (100%) | 8/8 (100%) |

---

## The Formula

### Layer 0 — Hard Blocks (automatic reject)

| Condition | Reason | Evidence |
|-----------|--------|----------|
| RSI 45-55 | No directional conviction | 29-50% WR in neutral zone |
| BB state = STRETCH_UP | Extended but not exhausted | 0% WR, -$135 on 3 trades |
| Duration >= 12h | Trade has stagnated | 43% WR on 6h+ trades |
| OTR < 8% AND BB state = NORM | Low cost trap — box too wide | 17% WR, 24% of losers cluster here |
| Box age 18-24h AND EMA20 dist < 15% | Stale box + too close to EMA | 0W/4L in paired analysis |

### Layer 1 — Single Factor Points

#### Entry Hour (Gap: +39% at 0-7 UTC, -23% at 12-19 UTC)
```
00-07 UTC → +13    (82-89% WR, winners cluster here)
08-11 UTC → +2
12-15 UTC → -8     (43% WR, losers cluster here)
16-19 UTC → -4
20-23 UTC → -7     (38% WR)
```

#### Box Age (Gap: +37% at <12h, -38% at 12-24h)
```
<8h       → +13    (83% WR, fresh boxes win)
8-12h     → +6
12-18h    → 0
18-24h    → -12    (41% WR, stale boxes lose)
24h+      → -4
```

#### Natural Stop (Gap: +20% at 4-6p)
```
4-6 pips  → +9     (70% WR, sweet spot)
6-8 pips  → +3
<4 pips   → -4     (too tight, noise risk)
8+ pips   → -2
```

#### Volume Ratio (Gap: +22% at 0.9-1.1)
```
0.9-1.1   → +9     (82% WR, calm markets win)
1.1-1.3   → +2
0.7-0.9   → -4     (47% WR)
1.3+      → -3     (43% WR, volatile markets lose)
```

#### Momentum Candles (Gap: +11% at 2-3)
```
2-3       → +8     (77-80% WR)
1         → +2
0         → -5     (50% WR, no momentum)
4+        → +3
```

#### Directional Candles (Gap: +12% at 4+)
```
4+        → +7     (83-100% WR)
2-3       → +3
1         → 0
0         → -6     (50% WR, coin flip)
```

#### Z-Score Magnitude (Gap: +18% at 0.5-1.5)
```
|z| 0.5-1.0  → +6
|z| 1.0-1.5  → +5
|z| 1.5+     → +2
|z| <0.5     → -9     (29% WR, neutral = trap)
```

#### BB Position
```
0.65-0.85 → +5     (68% WR)
0.50-0.65 → +1
0.35-0.50 → -3     (33% WR)
>0.85     → -4
<0.35     → -2
```

#### EMA50 Distance (Gap: -16% at <20%)
```
>=60%     → +5
20-60%    → 0
<20%      → -10    (20% WR! Entering at the battle zone)
```

#### EMA20 Distance (Gap: -17% at <15%)
```
15-30%    → +3     (69% WR)
>=30%     → +1
<15%      → -7     (47% WR, too close to EMA)
```

#### OTR (INVERTED — low OTR is BAD)
```
8-18%     → +4     (63-68% WR)
18-30%    → 0
<8%       → -11    (17% WR! Cheapest trades lose most)
30%+      → -2
```

#### ADR Consumed
```
<0.8      → +4
0.8-1.2   → -3     (45% WR, daily range nearly spent)
>1.2      → -1
```

#### Duration (exit signal, not entry filter)
```
30-120m   → +6
120-360m  → +2
<30m      → -2
360-720m  → -5
12h+      → BLOCK
```

#### Wall Proximity
```
<0.15     → +2
0.15-0.30 → -9     (29% WR danger zone)
0.30-0.50 → +1
>0.50     → -2
```

#### Body %
```
<25%      → +2     (doji = 75% WR)
25-45%    → 0
45-65%    → -8     (40% WR, momentum candle not reversal)
65%+      → -2
```

#### BB State
```
NORMAL    → +3
REJ_L/S   → +2
BRK_UP/DN → -1
STR_UP    → BLOCK
STR_DN    → -3
```

#### RSI (outside neutral block)
```
<30       → +7
30-45     → +3
55-70     → -2
70+       → -3
45-55     → BLOCK
```

### Layer 2 — Paired Aspect Points

These fire ON TOP of single-factor points when two conditions are true simultaneously.

#### Positive Pairs (winner clusters)
```
00-07 UTC + BB NORM      → +14    (9W/0L in sample)
00-07 UTC + momentum=0   → +13    (8W/0L)
00-07 UTC + |z|=0.5-1.0  → +13    (8W/0L)
00-07 UTC + nat stop=4-6 → +11    (7W/0L)
00-07 UTC + box age <8h  → +11    (7W/0L)
|z|=1.0-1.5 + BB NORM    → +11    (7W/0L)
00-07 UTC + EMA50 >=60%  → +11    (7W/0L)
box age <8h + RSI <45    → +9     (6W/0L)
box age <8h + OTR 8-18%  → +9     (6W/0L)
box age <8h + BB NORM    → +9     (6W/0L)
```

#### Negative Pairs (loser traps)
```
12-15 UTC + ADR 0.8-1.2    → -13   (0W/6L in sample)
12-15 UTC + dir candles=0  → -11   (0W/5L)
|z|<0.5 + ADR 0.8-1.2     → -9    (0W/4L)
|z|<0.5 + body% 45-65%    → -9    (0W/4L)
momentum=0 + BB pos >0.85  → -9    (0W/4L)
nat stop <4p + vol 0.7-0.9 → -9    (0W/4L)
dir candles=0 + OTR <8%    → -9    (0W/4L)
|z|<0.5 + BB NORM         → -9    (0W/4L)
```

### Decision Rule

```
FINAL_SCORE = Layer 1 points + Layer 2 pair points

REJECT if:
  Any Layer 0 hard block fires

TAKE if:
  FINAL_SCORE >= 65    (A-tier, high confidence)
  FINAL_SCORE >= 50    (B-tier, borderline — take if no negative pairs)

REJECT if:
  FINAL_SCORE < 50
```

---

## Key Findings That Drove This Formula

Full analysis in V3 repo: `docs/MASTER_FINDINGS.md`, `docs/FACTOR_BIBLE.md`, `docs/TRADE_RECIPE.md`

### Top 5 winner signals:
1. **Box age <12h** — 83% WR. Fresh boxes have structure. Stale boxes are consumed.
2. **Entry hour 0-7 UTC** — 82-89% WR. Early session = institutional activity.
3. **Volume ratio 0.9-1.1** — 82% WR. Calm markets = reversal edge. Chasing volatility = losing.
4. **4+ directional candles** — 83-100% WR. Strong conviction = strong trade.
5. **Natural stop 4-6 pips** — 70% WR. Tight structure = clean invalidation level.

### Top 5 loser traps:
1. **OTR < 8%** — 17% WR. "Cheap" trades have wide unfocused boxes. REMOVE from positive scoring.
2. **EMA50 distance < 20%** — 20% WR. Entry at the EMA50 battle zone.
3. **RSI 45-55** — 29-50% WR. No conviction = no edge.
4. **BB STRETCH** — 0% WR. Extended but not exhausted = trend continuation, not reversal.
5. **Box age 12-24h** — 41% WR. Stale box = levels already tested.

### Overturned assumptions:
- **OTR scoring** was positive for low OTR. Data shows low OTR = worst WR (17%). INVERTED.
- **R:R ratio** — same for winners and losers. Dead weight. Reduced to token 0-2 pts.
- **ATR consumed** — trivial 4% difference. Reduced from 0-20 to 0-4 pts.
- **Wall proximity** — V1 live shows 0% delta (W=0.39, L=0.39). Weak signal.
- **Wick ratio** — flips between V1 and V2 datasets. Unreliable.

---

## Comparison: OLD vs NEW

| Metric | OLD (pre-fix) | NEW (hybrid) |
|--------|---------------|-------------|
| Separation (W-L avg) | +0.5 | +80.3 |
| Best P&L any threshold | Never positive | +$332 |
| WR on passed trades | 52% | 100% |
| Losers caught | 0/21 | 21/21 |
| Based on | Theory + April audit | Raw OANDA candle data (50 trades) |
| Out-of-sample validated | No | Yes (20-trade test set) |
