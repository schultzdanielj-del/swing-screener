# TC2000 PCF (Personal Criteria Formulas) — Complete Reference

Source: https://help.tc2000.com/m/69445

---

## 1. FORMULA TYPES

PCF produces two types of formulas:

- **Indicator Formula** — returns a numeric value (e.g. `C - AVGC50`)
- **Condition Formula** — returns Boolean true/false (e.g. `C > AVGC50`)

TC2000 auto-detects which type based on what the formula returns.

There are also special indicator subtypes:
- **% True Indicator** — returns 0-100 for how many times a boolean was true over N bars
- **Channel Indicator** — returns two values (upper/lower) plotted as a channel
- **Cumulative Indicator** — running cumulative sum of a formula
- **Child Indicator** — references data from a parent indicator (allows access to PSAR, Relative Strength, Bid/Ask, VSTOP which are not in standard PCF language)

---

## 2. PRICE & VOLUME PRIMITIVES

| Symbol | Description | Offset syntax |
|--------|-------------|---------------|
| `C` | Close (current price) | `C1` = 1 bar ago, `C5` = 5 bars ago |
| `O` | Open | `O1`, `O2`, etc. |
| `H` | High | `H1`, `H2`, etc. |
| `L` | Low | `L1`, `L2`, etc. |
| `V` | Volume | `V1`, `V2`, etc. |

Function syntax also works: `C(z)` where z=offset. `C(0)` = current bar = `C`.

**Offsets are always backward-looking.** `C1` = yesterday's close. `C50` = close 50 bars ago.

---

## 3. ROLLING WINDOW FUNCTIONS

These look across multiple bars and CANNOT be computed from a single bar's data. They are the core building blocks.

### 3a. Moving Averages

| PCF | Function syntax | Description |
|-----|----------------|-------------|
| `AVGwx.z` | `AVG(w, x)` | Simple Moving Average of w over x periods |
| `XAVGwx.z` | `XAVG(w, x)` | Exponential Moving Average of w over x periods |
| `FAVGwx.z` | `FAVG(w, x)` | Front-Weighted Moving Average of w over x periods |
| `HAVGwx.z` | `HAVG(w, x)` | Hull Moving Average of w over x periods |

**w parameter:**
- In shorthand: `O`, `H`, `L`, `C`, or `V` only (e.g. `AVGC50` = 50-period SMA of close)
- In function syntax: any numeric expression (e.g. `AVG(C-O, 10)` = avg of body size over 10 bars)

**z = offset** (optional, default 0). `AVGC50.1` = the 50 SMA value from 1 bar ago.

**KEY RULE FOR OUR PROJECT:** EMA in TC2000 = `XAVGC` not `EAVG`. So EMA21 = `XAVGC21`.

### 3b. Rolling Max / Min

| PCF | Function syntax | Description |
|-----|----------------|-------------|
| `MAXwx.z` | `MAX(w, x)` | Highest value of w over most recent x bars |
| `MINwx.z` | `MIN(w, x)` | Lowest value of w over most recent x bars |

Same w rules as moving averages. Shorthand only allows O/H/L/C/V.
Function syntax allows any numeric expression: `MAX(C-AVGC50, 30)` = max extension over 30 bars.

### 3c. SUM

| PCF | Function syntax | Description |
|-----|----------------|-------------|
| — | `SUM(w, x)` | Sum of w over most recent x bars |

No shorthand form. `SUM(V, 20)` = total volume over 20 bars.

---

## 4. BUILT-IN INDICATORS

These are recursive or complex multi-bar calculations built into TC2000. They each have both function and shorthand syntax.

### 4a. Average True Range

| PCF | Function syntax | Params |
|-----|----------------|--------|
| `ATRx.z` | `ATR(x, z)` | x=Period, z=Offset |

Uses **simple moving average** for smoothing (not Wilder's). This is important — TC2000's ATR is SMA-based.

**KEY RULE FOR OUR PROJECT:** ATR in TC2000 = `ATR14`, NOT `AVGT14` or `AVG14` or `AVGT`.

### 4b. RSI (two variants)

| PCF | Function syntax | Params | Description |
|-----|----------------|--------|-------------|
| `RSIx.y.z` | `RSI(x, y, z)` | x=Period, y=SMA smoothing, z=Offset | Standard RSI (not Wilder's) |
| `WRSIx.z` | `WRSI(x, z)` | x=Period, z=Offset | Wilder's smoothed RSI |

RSI has an extra SMA smoothing parameter `y`. WRSI does not.

### 4c. MACD

| PCF | Function syntax | Params |
|-----|----------------|--------|
| `MACDs.l.z` | `MACD(s, l, z)` | s=Short EMA, l=Long EMA, z=Offset |

Returns the MACD oscillator value (difference between short and long EMA of close).

### 4d. Stochastics

| PCF | Function syntax | Params | Description |
|-----|----------------|--------|-------------|
| `STOCx.y.z` | `STOC(x, y, z)` | x=Period, y=SMA, z=Offset | Simple %K stochastic |
| `WSTOCx.y.z` | `WSTOC(x, y, z)` | x=Period, y=SMA, z=Offset | Worden Stochastic (proprietary) |

### 4e. Directional Movement System

| PCF | Function syntax | Params |
|-----|----------------|--------|
| `DIPLUSx.z` | `DIPLUS(x, z)` | x=Period, z=Offset |
| `DIMINUSx.z` | `DIMINUS(x, z)` | x=Period, z=Offset |
| `ADXd.s.z` | `ADX(d, s, z)` | d=DI Period, s=Smoothing, z=Offset |

### 4f. Commodity Channel Index

| PCF | Function syntax | Params |
|-----|----------------|--------|
| `CCIx.z` | `CCI(x, z)` | x=Period, z=Offset |

### 4g. Bollinger Bands

| PCF | Function syntax | Params |
|-----|----------------|--------|
| `BBTOPd.x.z` | `BBTOP(d, x, z)` | d=StdDev multiplier (integer only in shorthand), x=Period, z=Offset |
| `BBBOTd.x.z` | `BBBOT(d, x, z)` | Same |

### 4h. Standard Deviation

| PCF | Function syntax | Params |
|-----|----------------|--------|
| `STDDEVx.z` | `STDDEV(x, z)` | x=Period, z=Offset |

Of closing prices.

### 4i. Aroon

| PCF | Function syntax | Params |
|-----|----------------|--------|
| `AROONUPx.z` | `AROONUP(x, z)` | x=Period, z=Offset |
| `AROONDOWNx.z` | `AROONDOWN(x, z)` | x=Period, z=Offset |

### 4j. Volume-Based Indicators

| PCF | Function syntax | Params | Description |
|-----|----------------|--------|-------------|
| `OBVy.z` | `OBV(y, z)` | y=SMA smoothing, z=Offset | On Balance Volume |
| `MSy.z` | `MS(y, z)` | y=SMA smoothing, z=Offset | MoneyStream (proprietary, = Cumulative MoneyStream) |
| `TSVy.z` | `TSV(y, z)` | y=SMA smoothing, z=Offset | Time Segmented Volume (proprietary) |

### 4k. Balance of Power

| PCF | Function syntax | Params |
|-----|----------------|--------|
| `BOPy.z` | `BOP(y, z)` | y=SMA smoothing, z=Offset |

---

## 5. MATHEMATICAL OPERATORS & FUNCTIONS

These transform values at query time — no precomputation needed.

### 5a. Arithmetic Operators

| Operator | Description | Notes |
|----------|-------------|-------|
| `v * w` | Multiply | |
| `v / w` | Divide | |
| `v + w` | Add | `v ++ w` and `v -- w` also add (handle sign edge cases) |
| `v - w` | Subtract | `v +- w` and `v -+ w` also subtract |
| `v \ w` | Integer divide | Rounds to nearest integer |
| `v ^ w` | Power | |
| `v MOD w` | Modulo | Returns remainder. `C MOD 1` = decimal portion of price |

### 5b. Math Functions

| Function | Description |
|----------|-------------|
| `ABS(w)` | Absolute value |
| `SQR(w)` | Square root |
| `LOG(w)` | Natural log (ln) |
| `CLG(w)` | Log base 10 |
| `EXP(w)` | Natural exponent (e^w) |
| `SGN(w)` | Sign: returns -1, 0, or 1 |
| `GREATEST(v, w, ...)` | Max of unlimited comma-separated args |
| `LEAST(v, w, ...)` | Min of unlimited comma-separated args |
| `()` | Parentheses for order of operations |

### 5c. Trigonometric Functions

| Function | Description |
|----------|-------------|
| `SIN(w)` | Sine |
| `COS(w)` | Cosine |
| `TAN(w)` | Tangent |
| `SEC(w)` | Secant |
| `CSC(w)` | Cosecant |
| `ATN(w)` | Inverse tangent (also `ARCTAN(w)`) |
| `ARCSIN(w)` | Inverse sine |
| `ARCCOS(w)` | Inverse cosine |
| `ARCCOT(w)` | Inverse cotangent |
| `ARCSEC(w)` | Inverse secant |
| `ARCCSC(w)` | Inverse cosecant |

### 5d. Hyperbolic Functions

| Function | Description |
|----------|-------------|
| `SINH(w)` | Hyperbolic sine |
| `COTH(w)` | Hyperbolic cotangent |
| `SECH(w)` | Hyperbolic secant |
| `CSCH(w)` | Hyperbolic cosecant |
| `ARCSINH(w)` | Inverse hyperbolic sine |
| `ARCCOSH(w)` | Inverse hyperbolic cosine |
| `ARCTANH(w)` | Inverse hyperbolic tangent |
| `ARCCOTH(w)` | Inverse hyperbolic cotangent |
| `ARCSECH(w)` | Inverse hyperbolic secant |
| `ARCCSCH(w)` | Inverse hyperbolic cosecant |

---

## 6. BOOLEAN (CONDITION) OPERATORS

### 6a. Relational Operators

| Operator | Description |
|----------|-------------|
| `v > w` | Greater than |
| `v >= w` | Greater than or equal |
| `v < w` | Less than |
| `v <= w` | Less than or equal |
| `v = w` | Equal |
| `v <> w` | Not equal |

### 6b. Logical Operators

| Operator | Description | Truth |
|----------|-------------|-------|
| `a AND b` | Both must be true | T AND T = T, all else F |
| `a OR b` | Either can be true | F OR F = F, all else T |
| `NOT(b)` | Reverses result | NOT(T) = F, NOT(F) = T |
| `a NAND b` | NOT(a AND b) | Same as NOT(a AND b) |
| `a NOR b` | NOT(a OR b) | Same as NOT(a OR b) |
| `a XOR b` | Exclusive or | True when exactly one is true |
| `a XNOR b` | NOT(a XOR b) | True when both same |

### 6c. Crossover Functions

| Function | Params | Description |
|----------|--------|-------------|
| `XUP(v, w, x)` | v=Numeric, w=Numeric, x=Period (default 1) | True when v crosses ABOVE w. Equivalent to: `v >= w AND v.x < w.x` |
| `XDOWN(v, w, x)` | v=Numeric, w=Numeric, x=Period (default 1) | True when v crosses BELOW w. Equivalent to: `v <= w AND v.x > w.x` |

Example: `XUP(C, AVGC10)` = close crossed above 10 SMA today (was below yesterday).

---

## 7. BOOLEAN-TO-NUMERIC CONVERSION

These functions convert true/false conditions into numbers. Critical for complex formulas.

| Function | Params | Description |
|----------|--------|-------------|
| `IIF(b, t, f)` | b=Boolean, t=Numeric if true, f=Numeric if false | Inline IF. Can be nested. |
| `CountTrue(b, x)` | b=Boolean, x=Period | Number of times b was true in last x bars |
| `SinceTrue(b, x)` | b=Boolean, x=Period | Bars since b was last true (0=current bar). Returns -1 if never true in period. |
| `TrueInRow(b, x)` | b=Boolean, x=Period | Consecutive true count from current bar back (0 to x) |
| `(b)` | b=Boolean | Returns -1 if true, 0 if false |
| `ABS(b)` | b=Boolean | Returns 1 if true, 0 if false |

**Examples:**
- `IIF(C > O, C, O)` — returns close if bullish candle, open if bearish
- `CountTrue(C > C1, 10)` — how many of last 10 bars were up days
- `SinceTrue(C < AVGC50, 20)` — how many bars since price was below 50 SMA (within 20 bar lookback)
- `TrueInRow(C > C1, 10)` — consecutive up days (max 10)
- `TrueInRow(NOT(b), x)` — alternative to SinceTrue that returns x instead of -1 when not found

---

## 8. NET & PERCENT DIFFERENCE PATTERNS

Common patterns for rate of change calculations:

**Net difference (Rate of Change):**
```
C - C50          // 50-bar price change
C1 - C51         // 50-bar price change from 1 bar ago
```

**Percent difference (Rate of Change %):**
```
100 * (C / C50 - 1)          // 50-bar % change
100 * (C1 / C51 - 1)         // 50-bar % change from 1 bar ago
100 * (C / C50 - 1) < 3      // condition: less than 3% gain over 50 bars
```

General pattern: `100 * (Cz / Cy - 1)` where y = period + z.

---

## 9. INDICATOR FORMULA TEMPLATES

These are all constructible from the primitives above. The 67 templates are convenience formulas TC2000 provides. Key ones for our work:

### Commonly Used

| Indicator | PCF Construction | Notes |
|-----------|-----------------|-------|
| **SMA** | `AVGC50` | Direct built-in |
| **EMA** | `XAVGC21` | Direct built-in |
| **ATR** | `ATR14` | Direct built-in, SMA-smoothed |
| **Donchian Upper** | `MAXH20` | Highest high of 20 bars |
| **Donchian Lower** | `MINL20` | Lowest low of 20 bars |
| **Dollar Volume** | `C * V` | Simple math |
| **Momentum** | `C - Cx` | Net change over x bars |
| **ROC %** | `100 * (C / Cx - 1)` | Percent change over x bars |
| **Bollinger %b** | `(C - BBBOT(2,20,0)) / (BBTOP(2,20,0) - BBBOT(2,20,0))` | Position within bands |
| **Bollinger BW** | `(BBTOP(2,20,0) - BBBOT(2,20,0)) / AVGC20` | Bandwidth as % of SMA |
| **DEMA** | `2 * XAVG(C,x) - XAVG(XAVG(C,x), x)` | Double EMA |
| **TEMA** | `3*XAVG(C,x) - 3*XAVG(XAVG(C,x),x) + XAVG(XAVG(XAVG(C,x),x),x)` | Triple EMA |
| **Keltner Upper** | `XAVGC20 + 1.5 * ATR10` | EMA + ATR multiple |
| **Keltner Lower** | `XAVGC20 - 1.5 * ATR10` | EMA - ATR multiple |
| **Envelope Upper** | `AVGC20 * 1.05` | SMA + percentage |
| **Envelope Lower** | `AVGC20 * 0.95` | SMA - percentage |
| **Williams %R** | `(MAXH14 - C) / (MAXH14 - MINL14) * -100` | |
| **Stochastic %K** | `STOC(14, 3, 0)` | Direct built-in |
| **Elder Force** | `(C - C1) * V` | Price change × volume |
| **Elder Bull Power** | `H - XAVGC13` | High minus EMA |
| **Elder Bear Power** | `L - XAVGC13` | Low minus EMA |
| **Elliott Wave Osc** | `AVGC5 - AVGC35` | Difference of two SMAs |
| **PPO** | `(XAVGC12 - XAVGC26) / XAVGC26 * 100` | Price Percent Oscillator |
| **PVO** | `(XAVG(V,12) - XAVG(V,26)) / XAVG(V,26) * 100` | Volume Percent Oscillator |
| **MACD Histogram** | `MACD(12,26,0) - XAVG(MACD(12,26,0), 9)` | MACD minus signal |
| **Kaufman Efficiency** | `ABS(C - Cx) / SUM(ABS(C - C1), x)` | Direction / volatility |
| **Money Flow Index** | Requires IIF + SUM (complex) | Volume-weighted RSI |
| **Chaikin Money Flow** | `SUM(((C-L)-(H-C))/(H-L)*V, 20) / SUM(V, 20)` | |
| **Pivot Point** | `(H1 + L1 + C1) / 3` | Previous bar's typical price |
| **Historical Vol** | `STDDEV(x,0) / AVGC(x) * SQR(252) * 100` | Annualized std dev |
| **ADR (Avg Daily Range)** | `AVG(H-L, x)` or in ATR terms | Average bar range |

### Heiken-Ashi Candles
```
HA Close = (O + H + L + C) / 4
HA Open  = (O1 + C1) / 2  (simplified; true HA uses previous HA open)
HA High  = GREATEST(H, HA_Open, HA_Close)
HA Low   = LEAST(L, HA_Open, HA_Close)
```

### VWAP & Moving VWAP
VWAP in TC2000 is a **built-in indicator** (not constructible via PCF for intraday anchoring). Moving VWAP over N bars can be approximated:
```
SUM(C * V, x) / SUM(V, x)
```

### Indicators NOT available in PCF (require Child Indicator)
- **Parabolic SAR (PSAR)**
- **Relative Strength (vs index)**
- **VSTOP**
- **Bid/Ask data**

---

## 10. SYNTAX RULES & GOTCHAS

### Shorthand vs Function syntax
- Shorthand: `AVGC50` — only works with O, H, L, C, V as the data source
- Function: `AVG(C, 50)` — works with ANY numeric expression as data source
- Shorthand with offset: `AVGC50.1` = 50 SMA from 1 bar ago
- Function with offset uses last param: `AVG(C, 50)` can't specify offset directly — use shorthand for offsets

### Offset rules
- `C` = current bar close, `C1` = 1 bar ago, `C2` = 2 bars ago
- `AVGC50.1` = the 50 SMA as calculated 1 bar ago
- `MAXH30.5` = highest high of 30 bars, calculated 5 bars ago
- Offsets shift the entire calculation window backward

### Combining conditions
Multiple conditions join with `AND`:
```
C > AVGC50
AND AVGC50 > AVGC50.1
AND MAXH30 - AVGC50 > 0.30 * C
```
Each condition on its own line with `AND` is clean TC2000 style.

### The ++ -- +- -+ operators
These exist to handle formula concatenation ambiguity:
- `C ++ -5` adds C and -5 (result: C - 5)
- `C -- -5` subtracts -5 from C (result: C + 5)
- `C +- 5` = C - 5
- `C -+ 5` = C - 5

### Comments in PCF Editor
- Use `//` for single-line comments
- Use `/* ... */` for multi-line comments
- Comments are stripped before evaluation

### Parameters
TC2000 supports parameterized formulas. Define params and give defaults. Users can adjust without editing code. Not relevant for our scan conditions.

---

## 11. CONDITION FORMULA STRUCTURE (FOR SCANS)

A scan condition in TC2000 is a single Boolean expression. Multiple conditions are joined with `AND`.

**Our scan pattern:**
```
// Condition 1: description
expression1 > threshold1
AND
// Condition 2: description
expression2 < threshold2
AND
// ... etc
```

**Each individual condition in EasyScan** is a separate PCF condition formula. TC2000 ANDs them all together. You don't need to write one giant formula — each condition is its own entry.

---

## 12. WHAT NEEDS PRECOMPUTATION vs QUERY-TIME

### MUST precompute (require multi-bar lookback or recursive calculation):
- `AVG`, `XAVG`, `FAVG`, `HAVG` (all moving averages)
- `MAX`, `MIN` (rolling windows)
- `SUM` (rolling sum)
- `ATR` (recursive true range averaging)
- `RSI`, `WRSI` (recursive gain/loss averaging)
- `MACD` (built on EMAs)
- `STOC`, `WSTOC` (rolling high/low + SMA)
- `CCI` (mean deviation calculation)
- `ADX`, `DIPLUS`, `DIMINUS` (recursive smoothing)
- `BBTOP`, `BBBOT` (SMA + rolling std dev)
- `STDDEV` (rolling standard deviation)
- `AROONUP`, `AROONDOWN` (position of high/low in window)
- `OBV`, `MS`, `TSV` (cumulative volume indicators)
- `BOP` (SMA-smoothed)
- `CountTrue`, `SinceTrue`, `TrueInRow` (multi-bar boolean lookback)

### Can compute at query time (single-bar math):
- All arithmetic: `+`, `-`, `*`, `/`, `\`, `^`, `MOD`
- All math functions: `ABS`, `SQR`, `LOG`, `CLG`, `EXP`, `SGN`
- All trig/hyperbolic functions
- `GREATEST`, `LEAST` (comparing already-computed values)
- `IIF` (conditional selection of already-computed values)
- All boolean operators: `>`, `<`, `>=`, `<=`, `=`, `<>`, `AND`, `OR`, `NOT`, etc.
- `XUP`, `XDOWN` (comparing current and offset values — need the underlying values precomputed but the cross logic is query-time)
- Price offsets (`C1`, `H5`, etc.) — just looking up a different row

### Special case — offsets of precomputed values:
`AVGC50.1` needs the SMA50 value from 1 bar ago. If we precompute SMA50 for every bar, the offset is just looking up the previous row. No extra precomputation needed beyond computing the indicator for all bars.

---

## 13. OUR PROJECT CONVENTIONS

When writing PCF for this project, always follow these rules:

| Item | Correct | Wrong |
|------|---------|-------|
| ATR 14-period | `ATR14` | `AVGT14`, `AVG14`, `AVGT` |
| EMA 21-period | `XAVGC21` | `EAVG21`, `XAVG21`, `EAVGC21` |
| EMA 8-period | `XAVGC8` | `EAVG8` |
| SMA 50-period | `AVGC50` | `SMA50`, `SMAC50` |
| Highest high 30 bars | `MAXH30` | `HH30`, `HIGHH30` |
| Lowest low 15 bars | `MINL15` | `LL15`, `LOWL15` |

Always present PCF code in a code block for one-click copy.
