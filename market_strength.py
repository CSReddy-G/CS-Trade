"""
Market Strength: technical, news and combined bulls-vs-bears gauges (0 = fully bearish, 50 = neutral,
100 = fully bullish) for the panel's Market Strength page. Pure scoring and configuration only; the data comes
from market_strength_feed.py, which reuses the bot's existing feeds.

RULES
  * Every parameter scores 0-100 from data that actually arrived. A parameter with no data scores None: it is shown
    as missing and its weight is left out (the others are re-scaled), never filled with a made-up value.
  * Coverage = share of the configured weight that has data. Below MIN_COVERAGE the gauge says so instead of
    presenting a number as a reading.
  * Directional readings that are genuinely ambiguous (PCR, crude, VIX, USD/INR) have a neutral zone and are only
    scored away from 50 when the reading is unambiguous; each explanation says how it was read.
  * The thresholds (40 / 60) are experimental bands, not probabilities.
"""
import math
import re
import time

MIN_COVERAGE = 50          # % of weight with data needed for a gauge to count as a reading
BEAR_BELOW, BULL_ABOVE = 40, 60

TECHNICAL = [  # key, label, default weight %
    ("participation", "Nifty 50 weighted participation", 12),
    ("order_flow", "Futures order flow / volume proxy", 10),
    ("futures_oi", "Futures OI and buildup", 8),
    ("pcr", "Options PCR", 7),
    ("oi_positioning", "CE/PE OI positioning", 8),
    ("levels", "Support/Resistance and level reactions", 12),
    ("ema", "EMA 9/21 momentum", 8),
    ("vwap", "VWAP and price position", 7),
    ("structure", "FVG / ORB structure", 5),
    ("adx_rsi", "ADX / RSI confirmation", 4),
    ("breadth", "Market breadth", 4),
    ("crude", "Crude oil", 3),
    ("usdinr", "USD/INR", 3),
    ("global", "Global markets / GIFT Nifty", 3),
    ("fii_dii", "FII/DII positioning", 3),
    ("vix", "India VIX / volatility regime", 3),
]
NEWS = [
    ("india", "Indian market / economic news", 30),
    ("policy", "RBI / government policy", 20),
    ("global", "Global economic news", 15),
    ("geopolitics", "Geopolitical developments", 15),
    ("companies", "Nifty constituent company news", 15),
    ("other", "Other relevant news", 5),
]
LABELS = {k: lab for k, lab, _ in TECHNICAL}
NEWS_LABELS = {k: lab for k, lab, _ in NEWS}


def defaults():
    return dict(technical={k: w for k, _, w in TECHNICAL}, news={k: w for k, _, w in NEWS},
                ratio=dict(technical=75, news=25), version=0, applied_at=None, applied_by=None)


# ------------------------------------------------------------------ configuration
def validate(cfg):
    """(clean config, None) or (None, list of errors). Every group must hold exactly the known keys, each 0-100,
    totalling 100 (to within 0.01)."""
    errors, out = [], {}
    for group, keys in (("technical", [k for k, _, _ in TECHNICAL]), ("news", [k for k, _, _ in NEWS]),
                        ("ratio", ["technical", "news"])):
        got = (cfg or {}).get(group)
        name = {"technical": "Technical weights", "news": "News weights", "ratio": "Technical/News ratio"}[group]
        if not isinstance(got, dict):
            errors.append(f"{name}: missing")
            continue
        clean = {}
        for k in keys:
            v = got.get(k)
            try:
                v = float(v)
            except (TypeError, ValueError):
                errors.append(f"{name}: {k} is not a number")
                continue
            if not math.isfinite(v) or v < 0 or v > 100:
                errors.append(f"{name}: {k} must be between 0 and 100")
                continue
            clean[k] = round(v, 2)
        extra = set(got) - set(keys)
        if extra:
            errors.append(f"{name}: unknown {', '.join(sorted(extra))}")
        if len(clean) == len(keys):
            total = sum(clean.values())
            if abs(total - 100) > 0.01:
                errors.append(f"{name} total {total:g}%, must be 100%")
        out[group] = clean
    return (None, errors) if errors else (out, None)


def load(path, read_json):
    """The active configuration: saved file merged over the defaults (an unreadable or invalid file → defaults)."""
    saved = read_json(path, None)
    base = defaults()
    if not saved:
        return base
    clean, err = validate(saved)
    if err:
        return base
    return dict(base, **clean, version=int(saved.get("version") or 0), applied_at=saved.get("applied_at"),
                applied_by=saved.get("applied_by"))


def apply(path, read_json, write_json, cfg, who=None, now=None):
    """Validate and save as the new active configuration (version + 1). Returns (config, None) or (None, errors)."""
    clean, err = validate(cfg)
    if err:
        return None, err
    cur = load(path, read_json)
    new = dict(clean, version=cur["version"] + 1, applied_at=now or time.time(), applied_by=who)
    write_json(path, new)
    return new, None


# ------------------------------------------------------------------ helpers
def _clip(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _sc(x):
    """Signed strength -1..+1 → 0..100."""
    return round(_clip(50 + 50 * max(-1.0, min(1.0, x))), 1)


def bias(score):
    if score is None:
        return dict(key="none", text="No reading")
    if score < BEAR_BELOW:
        return dict(key="bear", text="Bearish / PE bias")
    if score > BULL_ABOVE:
        return dict(key="bull", text="Bullish / CE bias")
    return dict(key="neutral", text="Neutral")


def param(key, score, raw, source, as_of=None, status=None, why=""):
    """One parameter's result. score None = no data (missing)."""
    return dict(key=key, label=LABELS.get(key, key), score=None if score is None else round(score, 1), raw=raw,
                source=source, as_of=as_of, status=status or ("missing" if score is None else "ok"), why=why)


def missing(key, why, source="—"):
    return param(key, None, None, source, None, "missing", why)


# ------------------------------------------------------------------ technical parameters (pure)
def score_participation(stocks):
    """stocks: [dict(weight, change_pct)] — index-weighted average move of the constituents."""
    rows = [s for s in stocks if s.get("change_pct") is not None and s.get("weight")]
    if not rows:
        return None, None, "no constituent prices"
    tw = sum(s["weight"] for s in rows)
    avg = sum(s["weight"] * s["change_pct"] for s in rows) / tw
    up = sum(1 for s in rows if s["change_pct"] > 0)
    return _sc(math.tanh(avg / 0.8)), f"{avg:+.2f}% weighted ({up} of {len(rows)} up, {tw:.0f}% of index)", \
        "index-weighted average day move of the 30 largest constituents; ±0.8% ≈ strong"


def score_breadth(stocks):
    rows = [s for s in stocks if s.get("change_pct") is not None]
    if not rows:
        return None, None, "no constituent prices"
    up = sum(1 for s in rows if s["change_pct"] > 0)
    down = sum(1 for s in rows if s["change_pct"] < 0)
    return _sc((up - down) / len(rows)), f"{up} up / {down} down of {len(rows)}", "advancers minus decliners, equal weight"


def score_order_flow(fut=None, proxy=None):
    """fut: dict(buy_qty, sell_qty) from the futures quote (pending order book), or
    proxy: [dict(o, c, v)] completed candles of the volume proxy — signed volume over the last 12."""
    if fut and (fut.get("buy_qty") or 0) + (fut.get("sell_qty") or 0) > 0:
        b, s = fut["buy_qty"], fut["sell_qty"]
        imb = (b - s) / (b + s)
        return _sc(imb * 1.5), f"bid qty {b:,} vs ask qty {s:,} ({imb:+.0%})", "futures order-book imbalance (pending buy vs sell quantity)"
    bars = [c for c in (proxy or []) if c.get("v")][-12:]
    if len(bars) < 6:
        return None, None, "no futures depth and too little proxy volume"
    tot = sum(c["v"] for c in bars)
    signed = sum(c["v"] * (1 if c["c"] > c["o"] else -1 if c["c"] < c["o"] else 0) for c in bars)
    x = signed / tot if tot else 0
    return _sc(x * 1.5), f"{x:+.0%} up-minus-down volume, last {len(bars)} candles", "NIFTYBEES volume on up vs down 5-min candles (proxy: indices have no volume)"


def score_futures_oi(price_chg_pct, oi_chg_pct):
    """Price change and OI change of the near-month future → buildup class."""
    if price_chg_pct is None or oi_chg_pct is None:
        return None, None, "no futures OI"
    if abs(price_chg_pct) < 0.05 or abs(oi_chg_pct) < 0.5:
        return 50.0, f"price {price_chg_pct:+.2f}%, OI {oi_chg_pct:+.1f}%", "no clear buildup (price or OI barely moved)"
    up, oi_up = price_chg_pct > 0, oi_chg_pct > 0
    kind, base = {(True, True): ("long buildup", 1.0), (False, True): ("short buildup", -1.0),
                  (True, False): ("short covering", 0.4), (False, False): ("long unwinding", -0.4)}[(up, oi_up)]
    inten = min(1.0, abs(price_chg_pct) / 0.5)
    return round(50 + 45 * base * inten, 1), f"{kind}: price {price_chg_pct:+.2f}%, OI {oi_chg_pct:+.1f}%", \
        "long buildup / short buildup weigh fully; short covering / long unwinding are weaker reads"


def score_pcr(pcr):
    """Put-call OI ratio near the money. Mild readings lean with put writing (support below), but extremes are
    crowded and often reverse, so they are faded back towards neutral rather than pushed further."""
    if pcr is None:
        return None, None, "no option-chain OI"
    if pcr <= 0:
        return None, None, "PCR unavailable"
    d = pcr - 1.0
    if abs(d) <= 0.5:
        s = 50 + 30 * (d / 0.5)                         # 0.5 → 20, 1.0 → 50, 1.5 → 80
    else:
        fade = min(1.0, (abs(d) - 0.5) / 0.5)           # beyond 1.5 / below 0.5: back towards 50 (crowded)
        s = 50 + math.copysign(30 * (1 - 0.6 * fade), d)
    why = "0.5–1.5 read as put/call writing; beyond that treated as crowded and faded towards neutral"
    return round(s, 1), f"PCR {pcr:.2f}", why


def score_oi_positioning(price, support, resistance, d_pe=None, d_ce=None):
    """Where price sits between the biggest put-OI strike (support) and call-OI strike (resistance), plus which side
    has been adding OI since the first reading of the day."""
    if not (price and support and resistance):
        return None, None, "no option-chain OI"
    parts, x = [], 0.0
    # only a break through a wall is directional; between the walls the position itself is not read
    if price > resistance:
        x += 0.5
        parts.append(f"price {price:,.0f} above the call wall {resistance:,.0f} (call writers under pressure)")
    elif price < support:
        x -= 0.5
        parts.append(f"price {price:,.0f} below the put wall {support:,.0f} (put writers under pressure)")
    else:
        parts.append(f"price {price:,.0f} between put wall {support:,.0f} and call wall {resistance:,.0f}")
    if d_pe is not None and d_ce is not None and (abs(d_pe) + abs(d_ce)) > 0:
        w = (d_pe - d_ce) / (abs(d_pe) + abs(d_ce))
        x += 0.5 * w
        parts.append(f"OI added today: PE {d_pe:+,.0f}, CE {d_ce:+,.0f}")
    return _sc(x), "; ".join(parts), "more put writing than call writing = support building (and the reverse)"


def score_levels(price, atr, pdh, pdl, pdc, recent=None):
    """Price against yesterday's high / low / close, plus a rejection at one of them in the last candles."""
    if not (price and atr and pdh and pdl and pdc):
        return None, None, "levels unavailable"
    if price > pdh:
        x, txt = 0.5 + 0.4 * min(1.0, (price - pdh) / (2 * atr)), f"above yesterday's high {pdh:,.0f}"
    elif price < pdl:
        x, txt = -0.5 - 0.4 * min(1.0, (pdl - price) / (2 * atr)), f"below yesterday's low {pdl:,.0f}"
    else:
        x = 0.45 * max(-1.0, min(1.0, (price - pdc) / max(atr, (pdh - pdl) / 2)))
        txt = f"inside yesterday's range, {'above' if price >= pdc else 'below'} its close {pdc:,.0f}"
    react = ""
    for c in (recent or [])[-6:]:
        for lv, name in ((pdh, "high"), (pdl, "low"), (pdc, "close")):
            if c["l"] <= lv + 0.3 * atr and c["c"] > lv + 0.3 * atr and c["l"] >= lv - 0.5 * atr:
                x, react = x + 0.15, f"; held yesterday's {name} as support"
            elif c["h"] >= lv - 0.3 * atr and c["c"] < lv - 0.3 * atr and c["h"] <= lv + 0.5 * atr:
                x, react = x - 0.15, f"; rejected at yesterday's {name}"
    return _sc(x), txt + react, "previous-day high / low / close and reactions at them (completed candles)"


def score_ema(closes, atr):
    from strategy import ema_series
    if len(closes) < 30 or not atr:
        return None, None, "not enough candles"
    f, s = ema_series(closes, 9), ema_series(closes, 21)
    spread = (f[-1] - s[-1]) / atr
    slope = (f[-1] - f[-4]) / atr
    x = 0.7 * max(-1.0, min(1.0, spread / 1.5)) + 0.3 * max(-1.0, min(1.0, slope / 0.8))
    return _sc(x), f"EMA9 {f[-1]:,.1f} vs EMA21 {s[-1]:,.1f} ({spread:+.2f} ATR)", "spread and slope of EMA 9 vs 21, completed 5-min candles"


def score_vwap(price, vwap, vwap_prev, atr):
    if not (price and vwap and atr):
        return None, None, "VWAP unavailable"
    d = (price - vwap) / atr
    slope = ((vwap - vwap_prev) / atr) if vwap_prev else 0
    x = 0.8 * math.tanh(d / 1.2) + 0.2 * max(-1.0, min(1.0, slope / 0.3))
    return _sc(x), f"price {d:+.2f} ATR from VWAP {vwap:,.1f}", "distance from session VWAP and its slope"


def score_structure(price, or_hi, or_lo, gaps):
    """Opening range (09:15–09:30) break and the unfilled fair-value gaps of the session (+1 bullish, -1 bearish)."""
    if not (price and or_hi and or_lo):
        return None, None, "opening range not complete"
    orb = 1 if price > or_hi else -1 if price < or_lo else 0
    net = sum(gaps or [])
    x = 0.6 * orb + 0.4 * max(-1, min(1, net / 2))
    txt = (f"{'above' if orb > 0 else 'below' if orb < 0 else 'inside'} opening range {or_lo:,.0f}–{or_hi:,.0f}; "
           f"unfilled FVGs {sum(1 for g in gaps or [] if g > 0)} bull / {sum(1 for g in gaps or [] if g < 0)} bear")
    return _sc(x), txt, "opening-range break and unfilled 3-candle gaps today"


def score_adx_rsi(adx, pdi, mdi, rsi):
    if adx is None or pdi is None or mdi is None or rsi is None:
        return None, None, "not enough candles"
    d = 1 if pdi > mdi else -1
    strength = max(0.0, min(1.0, (adx - 15) / 20))
    r = max(-1.0, min(1.0, (rsi - 50) / 20))
    x = 0.6 * d * strength + 0.4 * r
    return _sc(x), f"ADX {adx:.0f}, +DI {pdi:.0f} / −DI {mdi:.0f}, RSI {rsi:.0f}", "trend strength in the DI direction, plus RSI momentum"


def score_crude(chg_pct):
    """Oil up tends to weigh on Indian indices, but the link is loose: moves within ±0.3% are read as neutral."""
    if chg_pct is None:
        return None, None, "no crude price"
    m = 0 if abs(chg_pct) <= 0.3 else chg_pct - math.copysign(0.3, chg_pct)
    return _sc(-0.8 * math.tanh(m / 1.0)), f"Brent {chg_pct:+.2f}%", "inverse and loose: ±0.3% counts as neutral"


def score_usdinr(chg_pct):
    if chg_pct is None:
        return None, None, "no USD/INR price"
    m = 0 if abs(chg_pct) <= 0.05 else chg_pct - math.copysign(0.05, chg_pct)
    return _sc(-0.8 * math.tanh(m / 0.3)), f"USD/INR {chg_pct:+.2f}%", "a weaker rupee weighs on foreign flows; ±0.05% is neutral"


def score_global(agent_score, gift_premium=None):
    if agent_score is None:
        return None, None, "global markets unavailable"
    raw = f"global read {agent_score:+.2f}" + (f", GIFT {gift_premium:+.0f} pts vs spot" if gift_premium is not None else "")
    return _sc(agent_score), raw, "the global-markets agent's score (GIFT Nifty, US, Asia, Europe, macro)"


def score_vix(vix, vix_chg_pct, nifty_chg_pct):
    """VIX has no direction of its own: falling VIX with a rising Nifty confirms the rise, rising VIX with a falling
    Nifty confirms the fall; anything else is neutral. A high VIX halves the read (wide, two-way swings)."""
    if vix is None or vix_chg_pct is None or nifty_chg_pct is None:
        return None, None, "no VIX reading"
    if vix_chg_pct < -1 and nifty_chg_pct > 0:
        x = min(1.0, -vix_chg_pct / 8) * 0.8
        how = "VIX falling while Nifty rises — confirms the rise"
    elif vix_chg_pct > 1 and nifty_chg_pct < 0:
        x = -min(1.0, vix_chg_pct / 8) * 0.8
        how = "VIX rising while Nifty falls — confirms the fall"
    else:
        x, how = 0.0, "VIX and Nifty not confirming each other — neutral"
    if vix >= 20:
        x, how = x * 0.5, how + "; high VIX halves the read"
    return _sc(x), f"VIX {vix:.2f} ({vix_chg_pct:+.1f}%), Nifty {nifty_chg_pct:+.2f}%", how


# ------------------------------------------------------------------ news
SOURCE_TIER = re.compile(r"reuters|bloomberg|economic times|livemint|mint|moneycontrol|business standard|cnbc|"
                         r"financial express|hindu ?businessline|ndtv profit|business today|press trust|pti|rbi|sebi", re.I)
CATEGORY_RULES = [
    ("policy", r"\b(rbi|repo|monetary policy|mpc|sebi|government|govt|ministry|minister|budget|gst|tax|cabinet|"
               r"finance ministry|policy|regulat\w*|subsid\w*)\b"),
    ("geopolitics", r"\b(war|ceasefire|sanction\w*|tariff\w*|border|missile|conflict|attack|strike on|military|"
                    r"tension\w*|israel|iran|russia|ukraine|pakistan|china.*(border|taiwan)|geopolit\w*)\b"),
    ("global", r"\b(fed|fomc|powell|wall street|dow|nasdaq|s&p|treasury|us economy|us jobs|ecb|bank of japan|boj|"
               r"europe\w*|china|global|asian markets|dollar index|recession)\b"),
    ("india", r"\b(nifty|sensex|dalal|stock market|markets?|equit\w*|fii|fpi|dii|rupee|india'?s? (gdp|cpi|inflation|"
              r"economy|growth|pmi|iip|exports|imports|trade)|gdp|cpi|wpi|pmi|iip|inflation)\b"),
]


def categorise(title, companies=()):
    t = title.lower()
    if any(re.search(r"\b" + re.escape(c.lower()) + r"\b", t) for c in companies):
        return "companies"
    for cat, pat in CATEGORY_RULES:
        if re.search(pat, t):
            return cat
    return "other"


def news_items(items, now, companies=(), half_life_h=6.0, score_fn=None):
    """Score each headline: direction (the news agent's word list, −3..+3) x relevance x source reliability x
    impact x time decay. Duplicates (same leading words) and items older than 24 h are dropped."""
    if score_fn is None:
        from agents.news import score_headline as score_fn
    seen, out = set(), []
    for it in sorted(items, key=lambda x: -(x.get("ts") or 0)):
        title = (it.get("title") or "").strip()
        key = re.sub(r"\W+", "", title.lower())[:60]
        age_h = (now - (it.get("ts") or now)) / 3600
        if not title or key in seen or age_h > 24 or age_h < -1:
            continue
        seen.add(key)
        s = score_fn(title)
        cat = categorise(title, companies)
        relevance = 0.5 if cat == "other" else 1.0
        reliability = 1.0 if SOURCE_TIER.search(it.get("source") or "") else 0.6
        impact = 1.3 if abs(s) >= 2 else 1.0
        decay = 0.5 ** (max(0.0, age_h) / half_life_h)
        out.append(dict(title=title, source=it.get("source") or "", link=it.get("link") or "", category=cat,
                        score=s, weight=round(relevance * reliability * impact * decay, 3), age_min=int(max(0, age_h) * 60)))
    return out


def news_score(scored, weights):
    """Per-category score 0-100 (None when a category has no headlines) and the weighted news gauge."""
    cats = {}
    for k, _, _ in NEWS:
        rows = [r for r in scored if r["category"] == k]
        den = sum(3 * r["weight"] for r in rows)
        val = _sc(sum(r["score"] * r["weight"] for r in rows) / den) if den else None
        cats[k] = dict(key=k, label=NEWS_LABELS[k], score=val, count=len(rows), weight=weights.get(k, 0),
                       top=sorted(rows, key=lambda r: -abs(r["score"]) * r["weight"])[:3])
    total, cov = weighted({k: c["score"] for k, c in cats.items()}, weights)
    return total, cov, cats


# ------------------------------------------------------------------ aggregation
def weighted(scores, weights):
    """Weighted average over the keys that have a score; (score or None, coverage % of weight with data)."""
    have = {k: w for k, w in weights.items() if w and scores.get(k) is not None}
    tw, all_w = sum(have.values()), sum(w for w in weights.values() if w)
    if not tw:
        return None, 0.0
    return round(sum(scores[k] * w for k, w in have.items()) / tw, 1), round(100 * tw / all_w, 1) if all_w else 0.0


def contributions(params, weights):
    """Each parameter's points in the technical gauge (weight re-scaled over the parameters with data)."""
    tw = sum(weights.get(p["key"], 0) for p in params if p["score"] is not None)
    for p in params:
        w = weights.get(p["key"], 0)
        p["weight"] = w
        p["contribution"] = round(p["score"] * w / tw, 2) if tw and p["score"] is not None else None
    return params


def combined(tech, tech_cov, news, news_cov, ratio):
    """Technical x ratio + News x ratio. A side without a reading drops out (the other carries the whole gauge),
    and the result is flagged so it isn't presented as the full blend."""
    rt, rn = ratio.get("technical", 75), ratio.get("news", 25)
    t_ok = tech is not None and tech_cov >= MIN_COVERAGE
    n_ok = news is not None and news_cov >= MIN_COVERAGE
    if t_ok and n_ok:
        return round((tech * rt + news * rn) / (rt + rn), 1) if rt + rn else None, "full"
    if t_ok:
        return round(tech, 1), "technical only"
    if n_ok:
        return round(news, 1), "news only"
    return None, "insufficient data"


def gauge(score, coverage, note=""):
    ok = score is not None and coverage >= MIN_COVERAGE
    return dict(score=score if ok else None, preview_score=score, coverage=coverage, bias=bias(score if ok else None),
                ok=ok, note=note if ok else (note or f"only {coverage:.0f}% of the weight has data (needs {MIN_COVERAGE}%)"))


# ------------------------------------------------------------------ strategy alignment
def alignment(side, score):
    """How a strategy's CE / PE suggestion compares with the combined gauge. Information only; never trades."""
    if side not in ("CE", "PE"):
        return dict(key="none", text="No active signal", why="The strategy has no CE/PE suggestion right now.")
    if score is None:
        return dict(key="unknown", text="Can't compare", why="The combined gauge has no reading.")
    bull = side == "CE"
    if BEAR_BELOW <= score <= BULL_ABOVE:
        return dict(key="weak", text="Weak confirmation", why=f"Combined {score:.0f} is neutral: the market isn't leaning either way.")
    if (score > BULL_ABOVE) == bull:
        strong = score >= 70 if bull else score <= 30
        return dict(key="strong" if strong else "aligned",
                    text=f"{'Strong ' if strong else ''}{'Bullish' if bull else 'Bearish'} Alignment",
                    why=f"{side} signal with combined {score:.0f} ({'bullish' if bull else 'bearish'}).")
    return dict(key="conflict", text="Signal Conflict",
                why=f"{side} signal but combined {score:.0f} leans {'bearish' if bull else 'bullish'}.")


# ------------------------------------------------------------------ freshness
def freshness(as_of, now, market_open, live_s=180, delayed_s=1200):
    """'live' / 'delayed' / 'stale' / 'closed' (market shut: the reading is the last session's) / 'missing'."""
    if not as_of:
        return "missing"
    age = now - as_of
    if not market_open:
        return "closed"
    return "live" if age <= live_s else "delayed" if age <= delayed_s else "stale"
