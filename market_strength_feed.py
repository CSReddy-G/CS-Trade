"""
Inputs for the Market Strength page, gathered from the bot's existing feeds (nothing new is streamed):

  Nifty 5-min candles   index_chart.candles(): Zerodha historical when a panel user is logged in, else Yahoo.
                        Completed candles only.
  Nifty / VIX price     the panel's live_indices(): the shared Zerodha WebSocket (live.Stream) when it is live,
                        else Kite quotes, else Yahoo — passed in by app.py, never a second connection.
  Constituents          stocks.overview(): the 30 largest Nifty stocks with index weights (Yahoo, delayed).
  Volume proxy          NIFTYBEES 5-min candles (config.VOLUME_SYMBOL), as the strategies use.
  Futures / options     Zerodha quote of the near-month NIFTY future and the nearest-expiry option chain
                        (±OPT_STRIKES strikes) — only with a logged-in Zerodha app that has market-data access.
  Agents                agents.json: global markets (GIFT Nifty, USD/INR), crude, news headlines.
  EMA / FVG signals     index_chart.build() replays each strategy on the same candles the Index chart uses.

Nothing here places or changes orders.
"""
import threading
import time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import market_strength as ms

IST = ZoneInfo("Asia/Kolkata")
OPT_STRIKES = 10
_cache, _lock = {}, threading.Lock()
_oi_base = {}             # (date, tradingsymbol) -> first OI seen today, for "OI added today"


def _cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as e:
        val = dict(error=str(e)[:160])
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def market_open(now=None):
    now = now or datetime.now(IST)
    m = now.hour * 60 + now.minute
    return now.weekday() < 5 and 9 * 60 + 15 <= m <= 15 * 60 + 30


# ------------------------------------------------------------------ sources
def candles(creds):
    def get():
        import index_chart
        cs, source, note = index_chart.candles("NIFTY", 5, creds)
        now = datetime.now(IST)
        done = [c for c in cs if c["t"] + timedelta(minutes=5) <= now]          # completed candles only
        return dict(candles=done, source=source, note=note,
                    as_of=(done[-1]["t"] + timedelta(minutes=5)).timestamp() if done else None)
    return _cached(("candles", bool(creds)), 30, get)


def constituents():
    def get():
        import stocks
        return dict(rows=stocks.overview(), as_of=time.time(), source="Yahoo (delayed ~15 min)")
    return _cached("stocks", 120, get)


def volume_proxy():
    def get():
        import config
        from agents import market
        d = market.chart(config.VOLUME_SYMBOL, "5m", "2d", max_age=60)
        now = time.time()
        rows = [dict(o=o, c=c, v=v or 0, t=t) for t, o, c, v in zip(d["t"], d["o"], d["c"], d.get("v") or [0] * len(d["t"]))
                if t + 300 <= now]
        return dict(candles=rows, as_of=(rows[-1]["t"] + 300) if rows else None, source=f"{config.VOLUME_SYMBOL} (Yahoo)")
    return _cached("proxy", 60, get)


def derivatives(creds):
    """Near-month NIFTY future + nearest-expiry option chain from Zerodha, or dict(error=...)."""
    if not creds:
        return dict(error="not logged in to Zerodha")

    def get():
        from kiteconnect import KiteConnect
        k = KiteConnect(api_key=creds[1])
        k.set_access_token(creds[2])
        today = datetime.now(IST).date()
        inst = _cached(("nfo", today), 6 * 3600, lambda: [
            dict(ts=i["tradingsymbol"], token=i["instrument_token"], type=i["instrument_type"], strike=i["strike"],
                 expiry=i["expiry"] if isinstance(i["expiry"], date) else None)
            for i in k.instruments("NFO") if i.get("name") == "NIFTY"])
        if isinstance(inst, dict):
            raise RuntimeError(inst["error"])
        futs = sorted((i for i in inst if i["type"] == "FUT" and i["expiry"] and i["expiry"] >= today), key=lambda i: i["expiry"])
        if not futs:
            raise RuntimeError("no NIFTY future in the instrument list")
        fut = futs[0]
        fq = k.quote([f"NFO:{fut['ts']}"]).get(f"NFO:{fut['ts']}") or {}
        spot = fq.get("last_price")
        opts = [i for i in inst if i["type"] in ("CE", "PE") and i["expiry"] and i["expiry"] >= today]
        exp = min(i["expiry"] for i in opts)
        strikes = sorted({i["strike"] for i in opts if i["expiry"] == exp})
        atm = min(strikes, key=lambda s: abs(s - (spot or strikes[len(strikes) // 2])))
        near = strikes[max(0, strikes.index(atm) - OPT_STRIKES): strikes.index(atm) + OPT_STRIKES + 1]
        chain = [i for i in opts if i["expiry"] == exp and i["strike"] in near]
        q = k.quote([f"NFO:{i['ts']}" for i in chain])
        rows = []
        for i in chain:
            d = q.get(f"NFO:{i['ts']}") or {}
            oi = d.get("oi")
            base = _oi_base.setdefault((today, i["ts"]), oi)
            rows.append(dict(strike=i["strike"], kind=i["type"], oi=oi, d_oi=(oi - base) if oi is not None and base is not None else None))
        prev_oi = _cached(("fut_prev_oi", today, fut["token"]), 6 * 3600, lambda: _prev_oi(k, fut["token"], today))
        return dict(future=dict(symbol=fut["ts"], price=spot, prev_close=(fq.get("ohlc") or {}).get("close"), oi=fq.get("oi"),
                                prev_oi=prev_oi if isinstance(prev_oi, (int, float)) else None,
                                buy_qty=fq.get("buy_quantity"), sell_qty=fq.get("sell_quantity")),
                    chain=rows, expiry=str(exp), as_of=time.time(), source="Zerodha (quote)")
    return _cached(("deriv", creds[2]), 20, get)


def _prev_oi(k, token, today):
    rows = k.historical_data(token, today - timedelta(days=10), today, "day", oi=True)
    prev = [r for r in rows if r["date"].date() < today]
    return prev[-1].get("oi") if prev else None


def signals(creds, cfg):
    """Latest EMA cross and FVG signals, each replayed alone on the Nifty 5-min chart."""
    def get():
        import index_chart
        out = {}
        for sid, name in (("ema", "EMA cross"), ("fvg", "Fair Value Gap")):
            try:
                j = index_chart.build("NIFTY", 5, dict(cfg, STRATEGIES=[sid]), creds)
                t = j["candles"]["t"]
                last_day = datetime.fromtimestamp(t[-1], IST).date()
                today = [s for s in j["signals"] if datetime.fromtimestamp(t[s["i"]], IST).date() == last_day]
                s = today[-1] if today else None
                out[sid] = dict(name=name, side=s["side"] if s else None, outcome=s["outcome"] if s else None,
                                entry=s["entry"] if s else None, sl=s["sl"] if s else None, target=s["target"] if s else None,
                                at=datetime.fromtimestamp(t[s["i"]] + 300, IST).strftime("%H:%M") if s else None,
                                why=s["why"] if s else "", active=bool(s and s["outcome"] == "open"),
                                day=str(last_day), count_today=len(today))
            except Exception as e:
                out[sid] = dict(name=name, side=None, error=str(e)[:140])
        return out
    return _cached(("signals", bool(creds), tuple(cfg.get(k) for k in ("EMA_FAST", "EMA_SLOW", "FVG_ENTRY_MODE"))), 60, get)


# ------------------------------------------------------------------ indicators from completed candles
def session_stats(cs):
    """Levels and indicators from completed 5-min candles: yesterday's high/low/close, ATR, VWAP, opening range,
    unfilled FVGs today, ADX/DI, RSI."""
    import indicators
    import strategy
    if len(cs) < 30:
        return {}
    last = cs[-1]["t"].date()
    today = [c for c in cs if c["t"].date() == last]
    prev_days = sorted({c["t"].date() for c in cs if c["t"].date() < last})
    prev = [c for c in cs if prev_days and c["t"].date() == prev_days[-1]]
    closes = [c["c"] for c in cs]
    atr = strategy.atr(cs[-100:], 14)
    tp = [(c["h"] + c["l"] + c["c"]) / 3 for c in today]
    vwap = sum(tp) / len(tp) if tp else None
    vwap_prev = sum(tp[:-3]) / len(tp[:-3]) if len(tp) > 3 else None
    orc = [c for c in today if (c["t"].hour, c["t"].minute) < (9, 30)]
    or_done = len(orc) >= 3 and any((c["t"].hour, c["t"].minute) >= (9, 30) for c in today)
    gaps = []
    for i in range(len(today) - 2):
        a, c3 = today[i], today[i + 2]
        later = today[i + 3:]
        if a["h"] < c3["l"] and not any(x["l"] <= a["h"] for x in later):
            gaps.append(1)
        elif a["l"] > c3["h"] and not any(x["h"] >= a["l"] for x in later):
            gaps.append(-1)
    dmi = indicators.dmi_series(cs[-300:], 14)[-1]
    return dict(day=str(last), atr=atr, vwap=vwap, vwap_prev=vwap_prev, closes=closes, recent=today[-6:],
                pdh=max(c["h"] for c in prev) if prev else None, pdl=min(c["l"] for c in prev) if prev else None,
                pdc=prev[-1]["c"] if prev else None,
                or_hi=max(c["h"] for c in orc) if or_done else None, or_lo=min(c["l"] for c in orc) if or_done else None,
                gaps=gaps, adx=dmi[0] if dmi else None, pdi=dmi[1] if dmi else None, mdi=dmi[2] if dmi else None,
                rsi=strategy.rsi(closes[-200:], 14), close=closes[-1])


# ------------------------------------------------------------------ assemble
def build_params(inp, now):
    """inp: everything gathered (see gather). Returns the 16 technical parameters, in table order."""
    opn = inp.get("market_open", False)
    fr = lambda as_of: ms.freshness(as_of, now, opn)
    out = {}

    def put(key, res, source, as_of, why_missing=None):
        score, raw, why = res
        if score is None:
            out[key] = ms.missing(key, why_missing or why, source)
        else:
            out[key] = ms.param(key, score, raw, source, as_of, fr(as_of), why)

    st, cmeta = inp.get("stats") or {}, inp.get("candles_meta") or {}
    q = inp.get("nifty") or {}
    price = q.get("price") or st.get("close")
    price_src = q.get("source") or cmeta.get("source")
    price_at = q.get("as_of") or cmeta.get("as_of")
    stocks = inp.get("stocks") or {}
    srows = stocks.get("rows") or []
    put("participation", ms.score_participation(srows), stocks.get("source", "Yahoo"), stocks.get("as_of"), stocks.get("error"))
    put("breadth", ms.score_breadth(srows), stocks.get("source", "Yahoo"), stocks.get("as_of"), stocks.get("error"))

    der = inp.get("derivatives") or {}
    fut = der.get("future") or {}
    prox = inp.get("proxy") or {}
    if fut.get("buy_qty") or fut.get("sell_qty"):
        put("order_flow", ms.score_order_flow(fut=fut), "Zerodha futures depth", der.get("as_of"))
    else:
        put("order_flow", ms.score_order_flow(proxy=prox.get("candles")), prox.get("source", "volume proxy"), prox.get("as_of"),
            prox.get("error"))
    if fut.get("price") and fut.get("prev_close") and fut.get("oi") and fut.get("prev_oi"):
        pchg = (fut["price"] - fut["prev_close"]) / fut["prev_close"] * 100
        oichg = (fut["oi"] - fut["prev_oi"]) / fut["prev_oi"] * 100
        put("futures_oi", ms.score_futures_oi(pchg, oichg), f"Zerodha {fut.get('symbol', 'future')}", der.get("as_of"))
    else:
        out["futures_oi"] = ms.missing("futures_oi", der.get("error") and f"Zerodha futures unavailable: {der['error']}"
                                       or "previous-day futures OI unavailable", "Zerodha")
    chain = der.get("chain") or []
    ce = [r for r in chain if r["kind"] == "CE" and r.get("oi")]
    pe = [r for r in chain if r["kind"] == "PE" and r.get("oi")]
    if ce and pe:
        pcr = sum(r["oi"] for r in pe) / sum(r["oi"] for r in ce)
        put("pcr", ms.score_pcr(pcr), f"Zerodha option chain ({der.get('expiry')})", der.get("as_of"))
        sup, res = max(pe, key=lambda r: r["oi"])["strike"], max(ce, key=lambda r: r["oi"])["strike"]
        dpe = sum(r["d_oi"] for r in pe if r.get("d_oi") is not None) if any(r.get("d_oi") for r in pe + ce) else None
        dce = sum(r["d_oi"] for r in ce if r.get("d_oi") is not None) if dpe is not None else None
        put("oi_positioning", ms.score_oi_positioning(price, sup, res, dpe, dce), f"Zerodha option chain ({der.get('expiry')})",
            der.get("as_of"))
        inp["oi_walls"] = dict(support=sup, resistance=res)
    else:
        why = f"option chain unavailable: {der.get('error')}" if der.get("error") else "option chain unavailable"
        out["pcr"] = ms.missing("pcr", why, "Zerodha")
        out["oi_positioning"] = ms.missing("oi_positioning", why, "Zerodha")

    csrc, cat = cmeta.get("source", "candles"), cmeta.get("as_of")
    put("levels", ms.score_levels(price, st.get("atr"), st.get("pdh"), st.get("pdl"), st.get("pdc"), st.get("recent")),
        f"{csrc} + {price_src}", min(filter(None, [cat, price_at])) if cat or price_at else None, cmeta.get("error"))
    put("ema", ms.score_ema(st.get("closes") or [], st.get("atr")), csrc, cat, cmeta.get("error"))
    put("vwap", ms.score_vwap(price, st.get("vwap"), st.get("vwap_prev"), st.get("atr")), f"{csrc} + {price_src}", price_at or cat,
        cmeta.get("error"))
    put("structure", ms.score_structure(price, st.get("or_hi"), st.get("or_lo"), st.get("gaps")), csrc, cat, cmeta.get("error"))
    put("adx_rsi", ms.score_adx_rsi(st.get("adx"), st.get("pdi"), st.get("mdi"), st.get("rsi")), csrc, cat, cmeta.get("error"))

    ag = inp.get("agents") or {}
    cr, gl = ag.get("crude") or {}, ag.get("global") or {}
    put("crude", ms.score_crude(cr.get("change_pct") if cr.get("ok") else None), f"{cr.get('name') or 'Brent'} (crude agent)",
        cr.get("fetched_at"), cr.get("note"))
    usd = next((m for m in gl.get("macro") or [] if m.get("key") == "usdinr"), None)
    put("usdinr", ms.score_usdinr(usd.get("change_pct") if usd else None), "USD/INR (global agent, Yahoo)", gl.get("fetched_at"))
    put("global", ms.score_global(gl.get("score") if gl.get("ok") else None, gl.get("gift_premium")), "Global markets agent",
        gl.get("fetched_at"), gl.get("note"))
    out["fii_dii"] = ms.missing("fii_dii", "No FII/DII feed is connected (NSE publishes it after the close; not fetched).", "—")
    v = inp.get("vix") or {}
    put("vix", ms.score_vix(v.get("price"), v.get("change_pct"), q.get("change_pct")), v.get("source") or "India VIX",
        v.get("as_of"), v.get("error"))
    return [out[k] for k, _, _ in ms.TECHNICAL]


def gather(creds, cfg, quotes):
    """Everything the page needs, from the cached sources above. quotes: live_indices() rows by key."""
    c = candles(creds)
    cs = c.get("candles") or []
    from agents import load_cache
    return dict(candles_meta=dict(source=c.get("source"), as_of=c.get("as_of"), error=c.get("error"), note=c.get("note")),
                stats=session_stats(cs) if cs else {}, nifty=quotes.get("nifty") or {}, vix=quotes.get("vix") or {},
                stocks=constituents(), proxy=volume_proxy(), derivatives=derivatives(creds), agents=load_cache(),
                market_open=market_open())


def evaluate(inp, conf, now=None, companies=()):
    """Gauges, parameter table, news breakdown and levels for one configuration (used for the active view and for
    Apply). Pure given its inputs."""
    now = now or time.time()
    params = ms.contributions(build_params(inp, now), conf["technical"])
    tech, tcov = ms.weighted({p["key"]: p["score"] for p in params}, conf["technical"])
    news = (inp.get("agents") or {}).get("news") or {}
    items = news.get("items") or [                       # a cache written before "items" existed: its top headlines
        dict(h, ts=(news.get("fetched_at") or now) - 60 * (h.get("age_min") or 0)) for h in news.get("headlines") or []]
    scored = ms.news_items(items, now, companies)
    nscore, ncov, cats = ms.news_score(scored, conf["news"])
    news_at = news.get("fetched_at")
    comb, mode = ms.combined(tech, tcov, nscore, ncov, conf["ratio"])
    st = inp.get("stats") or {}
    walls = inp.get("oi_walls") or {}
    levels = [dict(name=n, value=round(v, 1)) for n, v in (
        ("Yesterday's high", st.get("pdh")), ("Yesterday's low", st.get("pdl")), ("Yesterday's close", st.get("pdc")),
        ("Opening range high", st.get("or_hi")), ("Opening range low", st.get("or_lo")), ("Session VWAP", st.get("vwap")),
        ("Call-OI wall (resistance)", walls.get("resistance")), ("Put-OI wall (support)", walls.get("support"))) if v]
    nstatus = ms.freshness(news_at, now, inp.get("market_open", False), live_s=900, delayed_s=3600)
    return dict(
        technical=ms.gauge(tech, tcov), news=dict(ms.gauge(nscore, ncov), status=nstatus, as_of=news_at,
                                                  headlines=len(scored), note=news.get("note")),
        combined=dict(ms.gauge(comb, 100.0 if comb is not None else 0.0, mode), mode=mode),
        params=params, news_categories=list(cats.values()), levels=levels,
        price=(inp.get("nifty") or {}).get("price") or st.get("close"))
