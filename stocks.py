"""
Stock analysis for the panel's "Stock trades" page — the 30 largest Nifty-50 companies.

For one stock it reads a year of daily candles (Yahoo) and yfinance's company summary, then reports:
  * technicals: moving averages, RSI, MACD, Bollinger Bands, ATR, volume, returns, support/resistance
  * fundamentals: valuation, growth, profitability, debt, analyst view
  * chart patterns: breakouts, double tops/bottoms, swing structure, MA crosses, candlesticks
  * a stock-option setup in the same shape as the index "Next trade" card

The setup shown on this page is an estimate: premiums use Black-Scholes with IV = the stock's
recent realised volatility, and lot sizes are approximate NSE values. The separately guarded
stock-option manager uses current instruments and quotes for live orders.
"""
import copy, math, threading, time, warnings
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import strategy
from agents import clip, label, market
from agents.fundamental import FINANCIALS, stock_view
from agents.technical import bollinger, macd
from feed import bs_price

IST = ZoneInfo("Asia/Kolkata")

# (NSE symbol, short name, approximate Nifty-50 weight %, approximate F&O lot size)
TOP15 = [
    ("HDFCBANK", "HDFC Bank", 12.5, 550), ("RELIANCE", "Reliance", 9.0, 500), ("ICICIBANK", "ICICI Bank", 8.5, 700),
    ("INFY", "Infosys", 5.0, 400), ("BHARTIARTL", "Bharti Airtel", 4.7, 475), ("LT", "Larsen & Toubro", 4.0, 175),
    ("TCS", "TCS", 3.3, 175), ("ITC", "ITC", 3.3, 1600), ("SBIN", "SBI", 3.2, 750),
    ("AXISBANK", "Axis Bank", 3.0, 625), ("KOTAKBANK", "Kotak Bank", 2.8, 2000), ("M&M", "Mahindra & Mahindra", 2.6, 200),
    ("BAJFINANCE", "Bajaj Finance", 2.3, 750), ("HINDUNILVR", "Hindustan Unilever", 2.0, 300), ("SUNPHARMA", "Sun Pharma", 1.7, 350),
    # next 15 by weight
    ("MARUTI", "Maruti Suzuki", 1.6, 50), ("HCLTECH", "HCL Technologies", 1.6, 350), ("NTPC", "NTPC", 1.5, 1500),
    ("ETERNAL", "Eternal (Zomato)", 1.5, 2425), ("TITAN", "Titan", 1.3, 175), ("BEL", "Bharat Electronics", 1.3, 1425),
    ("ULTRACEMCO", "UltraTech Cement", 1.2, 50), ("TATASTEEL", "Tata Steel", 1.2, 5500), ("POWERGRID", "Power Grid", 1.1, 1900),
    ("BAJAJFINSV", "Bajaj Finserv", 1.0, 250), ("ADANIPORTS", "Adani Ports", 1.0, 475), ("ONGC", "ONGC", 0.9, 2250),
    ("ASIANPAINT", "Asian Paints", 0.9, 250), ("JSWSTEEL", "JSW Steel", 0.9, 675), ("COALINDIA", "Coal India", 0.8, 1350),
]
TOP30 = TOP15  # name kept for existing imports; the list now holds 30 stocks
BY_SYM = {s: (s, n, w, lot) for s, n, w, lot in TOP15}

_lock = threading.Lock()
_info = {}          # sym -> (fetched_at, yfinance info dict)
INFO_TTL = 6 * 3600
_analysis_cache = {}
_analysis_locks = {}
_analysis_lock = threading.Lock()
ANALYSIS_TTL = 60


def yahoo(sym):
    return sym + ".NS"


# ------------------------------------------------------------------ list
def overview():
    """Last price and day change for all listed stocks, from one batched Yahoo request (cached 5 min)."""
    syms = [yahoo(s) for s, *_ in TOP15]
    try:
        closes = market.closes(syms, rng="5d", max_age=300)
    except Exception:
        closes = {}
    out = []
    for s, name, wt, _ in TOP15:
        c = closes.get(yahoo(s)) or []
        price = c[-1] if c else None
        prev = c[-2] if len(c) > 1 else None
        chg = round((price - prev) / prev * 100, 2) if price and prev else None
        out.append(dict(symbol=s, name=name, weight=wt, price=round(price, 2) if price else None,
                        change_pct=chg, bucket=move_bucket(chg)))
    # strongest movers first inside each bucket: biggest gain on the bull side, biggest fall on the bear side
    out.sort(key=lambda r: (BUCKETS.index(r["bucket"]),
                            r["change_pct"] is None,
                            -(r["change_pct"] or 0) if r["bucket"] in ("bullish", "mild bullish") else (r["change_pct"] or 0)))
    return out


BUCKETS = ("bullish", "mild bullish", "mild bearish", "bearish")
STRONG_SCORE = 0.4      # combined score at least this (either way) is "bullish"/"bearish" (agents.label's strong cut)
_combined = {}          # settings key -> (built at, rows)
COMBINED_TTL = 300
_combined_lock = threading.Lock()


def score_bucket(score):
    """Bucket a stock by its combined score: ≥ +0.4 bullish, 0..+0.4 mild bullish, −0.4..0 mild bearish, ≤ −0.4 bearish."""
    if score is None:
        return "mild bullish"
    if score >= STRONG_SCORE:
        return "bullish"
    if score >= 0:
        return "mild bullish"
    if score > -STRONG_SCORE:
        return "mild bearish"
    return "bearish"


def combined_overview(cfg):
    """Every stock with its combined score (technical + patterns + fundamental), bucketed by that score instead of
    today's move. Runs the full analysis for all stocks in parallel; the list is cached for 5 minutes."""
    from concurrent.futures import ThreadPoolExecutor
    key = tuple(cfg.get(k) for k in ("RR", "ITM_STRIKES", "MAX_PREMIUM_PER_TRADE", "NO_ENTRY_BEFORE", "NO_ENTRY_AFTER"))
    with _combined_lock:
        hit = _combined.get(key)
        if hit and time.time() - hit[0] < COMBINED_TTL:
            return copy.deepcopy(hit[1])
        day = {r["symbol"]: r for r in overview()}

        def one(sym):
            try:
                a = analyse(sym, cfg)
                return sym, a["score"], a["label"]
            except Exception:
                return sym, None, None

        with ThreadPoolExecutor(max_workers=8) as ex:
            scores = {sym: (sc, lab) for sym, sc, lab in ex.map(one, [s for s, *_ in TOP15])}
        out = []
        for s, *_ in TOP15:
            sc, lab = scores[s]
            out.append(dict(day[s], score=sc, label=lab, bucket=score_bucket(sc)))
        # strongest score first inside each bucket; stocks that couldn't be scored go last
        out.sort(key=lambda r: (BUCKETS.index(r["bucket"]), r["score"] is None,
                                -(r["score"] or 0) if r["bucket"] in ("bullish", "mild bullish") else (r["score"] or 0)))
        if sum(r["score"] is not None for r in out):
            _combined[key] = (time.time(), out)
        return copy.deepcopy(out)


STRONG_MOVE_PCT = 1.0   # a day move of at least this much (either way) is "bullish"/"bearish", smaller is "mild"


def move_bucket(change_pct):
    """Bucket a stock by today's move: ≥ +1% bullish, 0..+1% mild bullish, −1..0% mild bearish, ≤ −1% bearish."""
    if change_pct is None:   # no price yet: park it as flat
        return "mild bullish"
    if change_pct >= STRONG_MOVE_PCT:
        return "bullish"
    if change_pct >= 0:
        return "mild bullish"
    if change_pct > -STRONG_MOVE_PCT:
        return "mild bearish"
    return "bearish"


# ------------------------------------------------------------------ helpers
def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def sma_series(vals, n):
    out, s = [None] * len(vals), 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def pivots(h, l, k=5):
    """Swing highs/lows: a bar whose high (low) is the extreme of the k bars either side.
    Returns ([(index, price) highs], [(index, price) lows])."""
    hi, lo = [], []
    for i in range(k, len(h) - k):
        if h[i] == max(h[i - k:i + k + 1]):
            hi.append((i, h[i]))
        if l[i] == min(l[i - k:i + k + 1]):
            lo.append((i, l[i]))
    return hi, lo


def realised_vol(c, n=20):
    r = [math.log(c[i] / c[i - 1]) for i in range(len(c) - n, len(c)) if c[i - 1] > 0]
    if len(r) < 5:
        return None
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(252)


def strike_step(price):
    """Approximate NSE strike interval for a stock at this price."""
    for cap, step in ((250, 2.5), (500, 5), (1500, 10), (3000, 20), (6000, 50)):
        if price < cap:
            return step
    return 100


def monthly_expiry(now):
    """NSE stock options expire on the last Tuesday of the month (holidays ignored)."""
    def last_tue(y, m):
        nxt = datetime(y + (m == 12), m % 12 + 1, 1)
        d = nxt - timedelta(days=1)
        return (d - timedelta(days=(d.weekday() - 1) % 7)).date()
    d = last_tue(now.year, now.month)
    if d < now.date() or (d == now.date() and now.time() > dtime(15, 30)):
        y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        d = last_tue(y, m)
    return datetime.combine(d, dtime(15, 30), tzinfo=IST)


def company_info(sym):
    with _lock:
        hit = _info.get(sym)
    if hit and time.time() - hit[0] < INFO_TTL:
        return hit[1]
    import yfinance as yf
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            info = yf.Ticker(yahoo(sym)).info or {}
    except Exception:
        if hit:
            return hit[1]
        raise
    with _lock:
        _info[sym] = (time.time(), info)
    return info


# ------------------------------------------------------------------ technicals
def technicals(d):
    o, h, l, c, v = d["o"], d["h"], d["l"], d["c"], d["v"]
    price, pts = c[-1], []
    s20, s50, s200 = sma(c, 20), sma(c, 50), sma(c, 200)
    if s50 and s200:
        if price > s50 > s200:
            pts.append(("Price above the 50-day and 200-day averages, 50 above 200 (uptrend)", 0.3))
        elif price < s50 < s200:
            pts.append(("Price below the 50-day and 200-day averages, 50 below 200 (downtrend)", -0.3))
        elif price > s200:
            pts.append(("Above the 200-day average (long-term uptrend intact)", 0.1))
        else:
            pts.append(("Below the 200-day average (long-term trend weak)", -0.1))
    if s20:
        pts.append(("Above the 20-day average", 0.1) if price > s20 else ("Below the 20-day average", -0.1))

    r = strategy.rsi(c[-250:], 14)
    if r is not None:
        if r >= 70:
            pts.append((f"RSI {r:.0f}: overbought, pullback risk", -0.15))
        elif r >= 55:
            pts.append((f"RSI {r:.0f}: bullish momentum", 0.15))
        elif r <= 30:
            pts.append((f"RSI {r:.0f}: oversold, bounce possible", 0.1))
        elif r <= 45:
            pts.append((f"RSI {r:.0f}: bearish momentum", -0.15))

    m = macd(c)
    if m:
        line, sig, hist = m
        if hist[-1] > 0 and min(hist[-4:-1]) <= 0:
            pts.append(("MACD just crossed above its signal line", 0.2))
        elif hist[-1] < 0 and max(hist[-4:-1]) >= 0:
            pts.append(("MACD just crossed below its signal line", -0.2))
        elif hist[-1] > 0:
            pts.append(("MACD above signal" + (", rising" if hist[-1] > hist[-2] else ", fading"), 0.1 if hist[-1] > hist[-2] else 0.03))
        else:
            pts.append(("MACD below signal" + (", falling" if hist[-1] < hist[-2] else ", recovering"), -0.1 if hist[-1] < hist[-2] else -0.03))

    bl, bm, bu = bollinger(c)
    pb = (price - bl) / (bu - bl) if bu > bl else 0.5
    if pb > 1:
        pts.append(("Closed above the upper Bollinger Band (stretched)", -0.1))
    elif pb < 0:
        pts.append(("Closed below the lower Bollinger Band (stretched)", 0.1))

    vavg = sma(v[:-1], 20)
    vratio = v[-1] / vavg if vavg else None
    if vratio and vratio >= 1.5:
        up = c[-1] >= c[-2]
        pts.append((f"Volume {vratio:.1f}× its 20-day average on an {'up' if up else 'down'} day", 0.1 if up else -0.1))

    a = strategy.atr([dict(h=x, l=y, c=z) for x, y, z in zip(h, l, c)][-100:], 14)
    hi52, lo52 = max(h[-250:]), min(l[-250:])
    ph, pl = pivots(h, l, 5)
    gap = max(0.4 * (a or 0), price * 0.005)                       # ignore swings sitting right on the price
    res = _levels(sorted(p for _, p in ph[-15:] if p > price + gap), price)
    sup = _levels(sorted((p for _, p in pl[-15:] if p < price - gap), reverse=True), price)
    ret = lambda n: round((price / c[-1 - n] - 1) * 100, 1) if len(c) > n else None
    rep = _report(pts)
    rep.update(price=round(price, 2), prev_close=round(c[-2], 2), change=round(price - c[-2], 2),
               change_pct=round((price / c[-2] - 1) * 100, 2), day_high=round(h[-1], 2), day_low=round(l[-1], 2),
               sma20=_r(s20), sma50=_r(s50), sma200=_r(s200), rsi=_r(r, 1),
               macd=_r(m[0]) if m else None, macd_signal=_r(m[1]) if m else None,
               bb_lower=_r(bl), bb_upper=_r(bu), pct_b=_r(pb), atr=_r(a), atr_pct=_r(a / price * 100) if a else None,
               hi52=_r(hi52), lo52=_r(lo52), from_hi52=_r((price / hi52 - 1) * 100, 1), from_lo52=_r((price / lo52 - 1) * 100, 1),
               volume=int(v[-1]), vol_ratio=_r(vratio), returns=dict(w1=ret(5), m1=ret(21), m3=ret(63), m6=ret(126), y1=ret(249)),
               support=sup, resistance=res)
    return rep


# ------------------------------------------------------------------ chart patterns
def patterns(d):
    """Patterns seen on the daily chart. Each: name, bias (+1 bull / -1 bear / 0), detail, and when known
    the bar index it applies to so the chart can mark it."""
    o, h, l, c = d["o"], d["h"], d["l"], d["c"]
    n, price, out = len(c), c[-1], []
    add = lambda name, bias, detail, at=None, level=None: out.append(dict(name=name, bias=bias, detail=detail, at=at, level=level))

    # 20-day breakout / breakdown
    if n > 21:
        top, bot = max(h[-21:-1]), min(l[-21:-1])
        if price > top:
            add("20-day breakout", 1, f"Closed above the 20-day high of {top:,.1f}", n - 1, top)
        elif price < bot:
            add("20-day breakdown", -1, f"Closed below the 20-day low of {bot:,.1f}", n - 1, bot)
        rng = (top - bot) / price
        if rng < 0.06 and not (price > top or price < bot):
            add("Tight range", 0, f"20-day range is only {rng:.1%}: a move out of {bot:,.0f}–{top:,.0f} often runs", None, None)

    # 50/200-day cross in the last 15 sessions
    s50, s200 = sma_series(c, 50), sma_series(c, 200)
    for i in range(max(201, n - 15), n):
        if s50[i - 1] is None or s200[i - 1] is None:
            continue
        if s50[i - 1] <= s200[i - 1] and s50[i] > s200[i]:
            add("Golden cross", 1, f"50-day average crossed above the 200-day {n - 1 - i} sessions ago", i)
        elif s50[i - 1] >= s200[i - 1] and s50[i] < s200[i]:
            add("Death cross", -1, f"50-day average crossed below the 200-day {n - 1 - i} sessions ago", i)

    # Swing structure + double top / bottom over ~6 months
    ph, pl = pivots(h, l, 5)
    ph, pl = [p for p in ph if p[0] >= n - 130], [p for p in pl if p[0] >= n - 130]
    if len(ph) >= 3 and len(pl) >= 3:
        hh = ph[-1][1] > ph[-2][1] > ph[-3][1]
        hl = pl[-1][1] > pl[-2][1] > pl[-3][1]
        lh = ph[-1][1] < ph[-2][1] < ph[-3][1]
        ll = pl[-1][1] < pl[-2][1] < pl[-3][1]
        if hh and hl:
            add("Higher highs & higher lows", 1, "The last three swings each topped and bottomed higher: an uptrend structure")
        elif lh and ll:
            add("Lower highs & lower lows", -1, "The last three swings each topped and bottomed lower: a downtrend structure")
    dbl = []
    if len(ph) >= 2:
        (i1, p1), (i2, p2) = ph[-2], ph[-1]
        trough = min(l[i1:i2 + 1])
        if i2 - i1 >= 10 and abs(p1 - p2) / p1 < 0.03 and trough < min(p1, p2) * 0.96 and i2 >= n - 40:
            done = price < trough
            dbl.append((i2, dict(name="Double top" + ("" if done else " (forming)"), bias=-1, at=i2, level=trough,
                detail=f"Two peaks near {max(p1, p2):,.0f}" + (f"; broke the {trough:,.0f} neckline" if done else f"; confirms below {trough:,.0f}"))))
    if len(pl) >= 2:
        (i1, p1), (i2, p2) = pl[-2], pl[-1]
        peak = max(h[i1:i2 + 1])
        if i2 - i1 >= 10 and abs(p1 - p2) / p1 < 0.03 and peak > max(p1, p2) * 1.04 and i2 >= n - 40:
            done = price > peak
            dbl.append((i2, dict(name="Double bottom" + ("" if done else " (forming)"), bias=1, at=i2, level=peak,
                detail=f"Two lows near {min(p1, p2):,.0f}" + (f"; broke the {peak:,.0f} neckline" if done else f"; confirms above {peak:,.0f}"))))
    if dbl:                                    # in a range both can match; the more recent second swing wins
        out.append(max(dbl, key=lambda x: x[0])[1])

    # Bollinger squeeze
    widths = [(b[2] - b[0]) / b[1] for b in (bollinger(c[:i]) for i in range(max(20, n - 120), n + 1))]
    if len(widths) > 40 and widths[-1] <= sorted(widths)[len(widths) // 10]:
        add("Bollinger squeeze", 0, "Bands are the narrowest in ~6 months: volatility usually expands next")

    # Candlesticks on the last bars
    body, rng = abs(c[-1] - o[-1]), h[-1] - l[-1]
    up_w, lo_w = h[-1] - max(o[-1], c[-1]), min(o[-1], c[-1]) - l[-1]
    prior_down, prior_up = c[-2] < c[-6] if n > 6 else False, c[-2] > c[-6] if n > 6 else False
    if c[-2] < o[-2] and c[-1] > o[-1] and c[-1] >= o[-2] and o[-1] <= c[-2]:
        add("Bullish engulfing", 1, "Today's up candle swallowed yesterday's down candle", n - 1)
    elif c[-2] > o[-2] and c[-1] < o[-1] and c[-1] <= o[-2] and o[-1] >= c[-2]:
        add("Bearish engulfing", -1, "Today's down candle swallowed yesterday's up candle", n - 1)
    elif rng and lo_w >= 2 * body and up_w <= body and prior_down:
        add("Hammer", 1, "Long lower wick after a fall: buyers stepped in", n - 1)
    elif rng and up_w >= 2 * body and lo_w <= body and prior_up:
        add("Shooting star", -1, "Long upper wick after a rise: sellers stepped in", n - 1)
    elif rng and body <= 0.1 * rng:
        add("Doji", 0, "Open and close almost equal: indecision", n - 1)
    if h[-1] < h[-2] and l[-1] > l[-2]:
        add("Inside bar", 0, "Today's range sits inside yesterday's: a break of either side sets direction", n - 1)
    if l[-1] > h[-2]:
        add("Gap up", 1, f"Opened above yesterday's high ({h[-2]:,.1f}) and never filled", n - 1)
    elif h[-1] < l[-2]:
        add("Gap down", -1, f"Opened below yesterday's low ({l[-2]:,.1f}) and never filled", n - 1)
    return out


# ------------------------------------------------------------------ fundamentals
def fundamentals(sym):
    try:
        i = company_info(sym)
    except Exception as e:
        return dict(ok=False, score=0, label="unknown", bull=[], bear=[], note=f"Fundamentals unavailable ({str(e)[:80]}).")
    if not i:
        return dict(ok=False, score=0, label="unknown", bull=[], bear=[], note="Fundamentals unavailable.")
    pts = []
    eg, rg = i.get("earningsGrowth"), i.get("revenueGrowth")
    if eg is not None:
        pts.append((f"Earnings {eg:+.0%} vs a year ago", 0.4 * clip(eg / 0.2)))
    if rg is not None:
        pts.append((f"Revenue {rg:+.0%} vs a year ago", 0.2 * clip(rg / 0.15)))
    tpe, fpe = i.get("trailingPE"), i.get("forwardPE")
    if tpe and fpe and tpe > 0 and fpe > 0:
        if fpe < tpe * 0.95:
            pts.append((f"Forward P/E {fpe:.1f} below trailing {tpe:.1f}: earnings expected to rise", 0.15))
        elif fpe > tpe * 1.05:
            pts.append((f"Forward P/E {fpe:.1f} above trailing {tpe:.1f}: earnings expected to fall", -0.15))
    de = i.get("debtToEquity")
    if de is not None and yahoo(sym) not in FINANCIALS and de > 150:
        pts.append((f"High debt: debt/equity {de:.0f}%", -0.15))
    roe = i.get("returnOnEquity")
    if roe is not None and roe >= 0.18:
        pts.append((f"Return on equity {roe:.0%}", 0.1))
    elif roe is not None and roe < 0.08:
        pts.append((f"Low return on equity {roe:.0%}", -0.1))
    rm, tgt, px = i.get("recommendationMean"), i.get("targetMeanPrice"), i.get("currentPrice") or i.get("regularMarketPrice")
    if rm:
        pts.append((f"Analysts: {(i.get('recommendationKey') or '').replace('_', ' ') or 'consensus'} ({rm:.1f} on 1–5)", 0.1 * clip((3 - rm) / 2)))
    if tgt and px:
        up = tgt / px - 1
        if abs(up) >= 0.05:
            pts.append((f"Average analyst target {tgt:,.0f} ({up:+.0%})", 0.1 if up > 0 else -0.1))
    score, _ = stock_view(yahoo(sym), i)
    et = i.get("earningsTimestamp") or i.get("earningsTimestampStart")
    edate = datetime.fromtimestamp(et, IST).date() if et else None
    today = datetime.now(IST).date()
    rep = _report(pts)
    rep.update(score=round(clip(score), 2), label=label(score),
               name=i.get("longName") or i.get("shortName"), sector=i.get("sector"), industry=i.get("industry"),
               market_cap=i.get("marketCap"), pe=_r(tpe, 1), forward_pe=_r(fpe, 1), pb=_r(i.get("priceToBook"), 1),
               eps=_r(i.get("trailingEps")), div_yield=_r(_div_yield(i), 2), roe=_r(_pct(roe), 1),
               margin=_r(_pct(i.get("profitMargins")), 1), debt_equity=_r(de, 0), earnings_growth=_r(_pct(eg), 1),
               revenue_growth=_r(_pct(rg), 1), beta=_r(i.get("beta")), rec=i.get("recommendationKey"),
               analysts=i.get("numberOfAnalystOpinions"), target=_r(tgt), target_up=_r(_pct(tgt / px - 1), 1) if tgt and px else None,
               results_date=edate.strftime("%d %b %Y") if edate and edate >= today else None,
               results_soon=bool(edate) and 0 <= (edate - today).days <= 7)
    return rep


def _pct(x):
    """yfinance ratio (a fraction) as a percent."""
    return None if x is None else x * 100


def _div_yield(i):
    """trailingAnnualDividendYield is a fraction; newer yfinance gives dividendYield already in percent."""
    t = i.get("trailingAnnualDividendYield")
    return t * 100 if t else i.get("dividendYield")


# ------------------------------------------------------------------ option setup
def option_setup(sym, lot, tech, lean, cfg, now):
    price, a = tech["price"], tech.get("atr")
    exp = monthly_expiry(now)
    t = (exp - now).total_seconds() / (365 * 24 * 3600)
    iv = max(0.15, tech.get("iv") or 0.25)
    step = strike_step(price)
    rr = cfg.get("RR", 2)
    sides = []
    for side in ("CE", "PE"):
        strike = strategy.itm_strike(price, side, step, cfg.get("ITM_STRIKES", 1))
        prem = round(max(0.05, bs_price(price, strike, t, iv, side)), 2)
        cost = prem * lot
        stop = round(price - a, 1) if side == "CE" and a else round(price + a, 1) if a else None
        target = round(price + rr * a, 1) if side == "CE" and a else round(price - rr * a, 1) if a else None
        sides.append(dict(side=side, strike=strike, symbol=f"{sym} {exp:%d %b} {strike:g} {side}", premium=prem, cost=round(cost),
                          over_cap=bool(cfg.get("MAX_PREMIUM_PER_TRADE")) and cost > cfg["MAX_PREMIUM_PER_TRADE"],
                          stop=stop, target=target))
    return dict(expiry=exp.strftime("%a %d %b"), lot=lot, step=step, iv=round(iv * 100, 1), rr=rr, sides=sides, lean=lean)


# ------------------------------------------------------------------ one stock
def _analyse(sym, cfg):
    s, name, wt, lot = BY_SYM[sym]
    d = market.chart(yahoo(sym), "1d", "2y", max_age=600)
    if len(d["c"]) < 60:
        raise market.Unavailable(f"only {len(d['c'])} days of data")
    now = datetime.now(IST)
    tech = technicals(d)
    vols = [v for v in (realised_vol(d["c"], 20), realised_vol(d["c"], 60)) if v]
    tech["iv"] = sum(vols) / len(vols) if vols else None             # blend: a 20-day spike alone over-prices
    pats = patterns(d)
    fund = fundamentals(sym)
    pscore = clip(sum(0.2 * p["bias"] for p in pats))
    score = clip(0.5 * tech["score"] + 0.3 * pscore + 0.2 * fund.get("score", 0))
    lean = "CE" if score >= 0.15 else "PE" if score <= -0.15 else None
    setup = option_setup(sym, lot, tech, lean, cfg, now)
    s20, s50 = sma_series(d["c"], 20), sma_series(d["c"], 50)
    keep = 250                                                       # ~1 year on the chart
    cut = max(0, len(d["c"]) - keep)
    mins = lambda v: int(v[:2]) * 60 + int(v[3:5])
    in_window = now.weekday() < 5 and mins(cfg.get("NO_ENTRY_BEFORE", "09:20")) <= now.hour * 60 + now.minute <= mins(cfg.get("NO_ENTRY_AFTER", "14:45"))
    side = lean or "CE"
    o = next(x for x in setup["sides"] if x["side"] == side)
    r = tech.get("rsi")
    checks = [
        dict(name="Entry window", st="ok" if in_window else "no", note=f"{cfg.get('NO_ENTRY_BEFORE', '09:20')}–{cfg.get('NO_ENTRY_AFTER', '14:45')} IST, weekdays"),
        dict(name="Analysis has a lean", st="ok" if lean else "wait", note=f"Combined score {score:+.2f} ({label(score)}); needs ±0.15"),
        dict(name="Trend agrees", st="ok" if (side == "CE") == (tech["price"] > (tech.get("sma50") or 0)) else "no",
             note=f"Price {'above' if tech['price'] > (tech.get('sma50') or 0) else 'below'} the 50-day average {tech.get('sma50') or '—'}"),
        dict(name="RSI not stretched", st="no" if r and ((side == "CE" and r >= 75) or (side == "PE" and r <= 25)) else "ok",
             note=f"RSI {r if r is not None else '—'}"),
        dict(name="No results this week", st="no" if fund.get("results_soon") else "ok",
             note=f"Results on {fund['results_date']}: premiums jump around results" if fund.get("results_soon") else
             (f"Next results {fund['results_date']}" if fund.get("results_date") else "No date in the next week")),
        dict(name="Within capital cap", st="no" if o["over_cap"] else "ok",
             note=f"₹{o['cost']:,} of ₹{cfg['MAX_PREMIUM_PER_TRADE']:,} per trade" if cfg.get("MAX_PREMIUM_PER_TRADE") else "No cap set"),
    ]
    return dict(symbol=sym, name=name, weight=wt, at=now.strftime("%H:%M:%S"),
                data_at=d["t"][-1], data_source="Yahoo Finance daily", stale=bool(d.get("stale")),
                score=round(score, 2), label=label(score), lean=lean, pattern_score=round(pscore, 2),
                technical=tech, fundamental=fund, patterns=[dict(p, at=p["at"] - cut if p["at"] is not None and p["at"] >= cut else None)
                                                            for p in pats],
                setup=setup, checks=checks,
                chart=dict(t=d["t"][cut:], o=_rl(d["o"][cut:]), h=_rl(d["h"][cut:]), l=_rl(d["l"][cut:]), c=_rl(d["c"][cut:]),
                           sma20=_rl(s20[cut:]), sma50=_rl(s50[cut:])))


def analyse(sym, cfg):
    """Share recent stock analysis across users and the scanner without duplicating vendor calls."""
    settings = tuple(cfg.get(k) for k in ("RR", "ITM_STRIKES", "MAX_PREMIUM_PER_TRADE",
                                          "NO_ENTRY_BEFORE", "NO_ENTRY_AFTER"))
    key = (sym, settings)
    with _analysis_lock:
        hit = _analysis_cache.get(key)
        if hit and time.time() - hit[0] < ANALYSIS_TTL:
            return copy.deepcopy(hit[1])
        lock = _analysis_locks.setdefault(key, threading.Lock())
    with lock:
        with _analysis_lock:
            hit = _analysis_cache.get(key)
            if hit and time.time() - hit[0] < ANALYSIS_TTL:
                return copy.deepcopy(hit[1])
        result = _analyse(sym, cfg)
        with _analysis_lock:
            _analysis_cache[key] = (time.time(), result)
            if len(_analysis_cache) > len(TOP15) * 8:
                cutoff = time.time() - ANALYSIS_TTL
                for cache_key, (fetched, _) in list(_analysis_cache.items()):
                    if fetched < cutoff:
                        _analysis_cache.pop(cache_key, None)
        return copy.deepcopy(result)


def _levels(ps, price, n=2):
    """Nearest n distinct levels (at least 1% apart)."""
    out = []
    for p in ps:
        if all(abs(p - q) > price * 0.01 for q in out):
            out.append(round(p, 1))
        if len(out) == n:
            break
    return out


def _report(pts):
    score = clip(sum(w for _, w in pts))
    return dict(ok=True, score=round(score, 2), label=label(score),
                bull=[[t, round(w, 2)] for t, w in pts if w > 0], bear=[[t, round(-w, 2)] for t, w in pts if w < 0])


def _r(x, d=2):
    return None if x is None else round(x, d)


def _rl(vals):
    return [None if v is None else round(v, 2) for v in vals]
