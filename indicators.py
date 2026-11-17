"""
Indicators the bot did not have yet, in series form. Existing ones stay where they are (strategy.ema_series, rsi,
atr, adx) and are NOT re-implemented here; tests check that the series below end on exactly the values of
strategy.atr and strategy.adx, so the bot keeps one definition of each.

  atr_series(candles, n)              Wilder ATR after each candle (None until there is enough history)
  dmi_series(candles, n)              (ADX, +DI, -DI) after each candle; ADX is None until 2n candles
  supertrend(candles, n, mult)        (line, direction +1 / -1) after each candle

Every value at position i uses candles[:i+1] only.
"""


def atr_series(candles, n=14):
    out = [None] * len(candles)
    if len(candles) < n + 1:
        return out
    trs = [None]
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(candles)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def dmi_series(candles, n=14):
    """Wilder's directional movement, the same arithmetic as strategy.adx. Returns a list aligned to candles of
    (adx or None, +DI, -DI), or None where there is not enough history for the DIs."""
    out = [None] * len(candles)
    if len(candles) < n + 1:
        return out
    tr, pdm, mdm = [], [], []
    for a, b in zip(candles[:-1], candles[1:]):
        up, dn = b["h"] - a["h"], a["l"] - b["l"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])))
    atr_, p, m = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
    dxs, adx = [], None
    for i in range(n, len(tr) + 1):
        if i > n:
            atr_ = atr_ - atr_ / n + tr[i - 1]
            p = p - p / n + pdm[i - 1]
            m = m - m / n + mdm[i - 1]
        pdi, mdi = (100 * p / atr_, 100 * m / atr_) if atr_ else (0.0, 0.0)
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0)
        if len(dxs) == n:
            adx = sum(dxs) / n
        elif len(dxs) > n:
            adx = (adx * (n - 1) + dxs[-1]) / n
        out[i] = (adx if i >= 2 * n else None, pdi, mdi)   # tr[i-1] belongs to candles[i]; ADX from 2n+1 candles, as strategy.adx
    return out


def supertrend(candles, n=10, mult=3.0):
    """Standard Supertrend on Wilder ATR: final bands carry forward until price closes through them.
    Returns a list aligned to candles of (line, direction) — direction +1 bullish (line below price, a support),
    -1 bearish (line above price) — or None while ATR is warming up."""
    atrs = atr_series(candles, n)
    out = [None] * len(candles)
    ub = lb = None
    d = 1
    for i, c in enumerate(candles):
        a = atrs[i]
        if a is None:
            continue
        mid = (c["h"] + c["l"]) / 2
        bub, blb = mid + mult * a, mid - mult * a
        pc = candles[i - 1]["c"]
        if ub is None:
            ub, lb = bub, blb
            d = 1 if c["c"] >= mid else -1
        else:                                     # TradingView's definition
            pub, plb = ub, lb
            lb = max(blb, plb) if pc > plb else blb
            ub = min(bub, pub) if pc < pub else bub
            if d == -1 and c["c"] > pub:
                d = 1
            elif d == 1 and c["c"] < plb:
                d = -1
        out[i] = (lb if d == 1 else ub, d)
    return out
