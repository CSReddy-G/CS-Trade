"""
Supertrend + ADX strategy: trend continuation after a pullback, when the trend is strong (ADX) and directional
(+DI vs -DI). Shared by bot.py and the backtests. Index candles only, closed candles only.

ENTRY  CE (PE is the mirror image):
  1 Supertrend(ST_ATR_PERIOD 10, ST_MULT 2) bullish for the whole pullback window (close above the line)
  2 ADX(14) >= ST_ADX_MIN (20), and higher than ST_ADX_RISING_BARS (3) candles ago (0 = rising not required)
  3 +DI above -DI by at least ST_DI_GAP
  4 close above EMA ST_EMA_TREND (50)
  5 RSI(14) within [ST_RSI_CE_MIN, ST_RSI_CE_MAX]
  6 pullback: within the last ST_PULLBACK_BARS candles (this one included) a low came within ST_TOUCH_ATR x ATR
    of the Supertrend line or EMA ST_EMA_PULLBACK (20) and that candle still closed above the level
  7 confirmation: body >= ST_MIN_BODY_PCT of the range, bullish, close above the previous candle's high
  8 optional: completed 15-min candles trend up (ST_REQUIRE_HTF)
EXITS  ATR stop: ST_SL_ATR_MULT x ATR(10) from entry; with ST_SL_MODE "wider", beyond the Supertrend line instead
       when that is further. A stop tighter than ST_MIN_RISK_ATR is widened before entry; wider than ST_MAX_RISK_ATR
       skips the trade. Target ST_RR x risk; stop to cost at 1R; trailing from 1.5R (strategy.manage never lowers
       a stop). invalidation(): Supertrend flips against the trade.

V2 (research/select_st.py, development sessions 24 Jul – 29 Sep 2026 only): with all 8 checks it took 13 trades in
47 sessions on both indexes and won 1. Defaults now: ADX >= 20 (rising not required), RSI band / EMA-50 / close beyond
the previous candle OFF (each still a setting), and at 1R the stop locks 25% of the risk. Development: 99 trades,
55% wins, PF 1.18. Tested once on the last 30 sessions: reports/strategy_review/RESULTS.md.

V3 (research/select_st_90d.py, 15 Nov 2026): over the last 90 days (17 Aug – 13 Nov) the v2 defaults won 56% (PF 1.47)
and the saved ADX >= 25 + rising setup 48% (PF 1.17). Grid chosen on 17 Aug – 30 Sep only, then checked unchanged on
1 Oct – 13 Nov: Supertrend multiplier 2, ADX >= 20 and rising over 3 candles, close beyond the previous candle, no
trailing, no exit on a Supertrend flip. First half 22 trades 73% wins PF 3.04; second half 30 trades 67% PF 2.55
(1 lot, each index alone, Backtest engine). Small samples: paper trade it before trusting it.

The confidence score ranks how cleanly the checklist was met. It is NOT a probability of winning.
"""
import fvg
import indicators
import orb_retest
import orderblock
import strategy

CFG_DEFAULTS = dict(
    ST_ALLOW_LIVE=False,
    ST_ATR_PERIOD=10, ST_MULT=2.0, ST_ADX_PERIOD=14, ST_ADX_MIN=20, ST_ADX_RISING_BARS=3, ST_DI_GAP=0.0,
    ST_EMA_TREND=50, ST_EMA_PULLBACK=20, ST_RSI_PERIOD=14,
    ST_RSI_CE_MIN=50, ST_RSI_CE_MAX=70, ST_RSI_PE_MIN=30, ST_RSI_PE_MAX=50,
    ST_PULLBACK_BARS=4, ST_TOUCH_ATR=0.3, ST_MIN_BODY_PCT=0.5,
    ST_REQUIRE_HTF=False, ST_HTF_MINUTES=15, ST_HTF_EMA=20,
    ST_SL_MODE="atr", ST_SL_ATR_MULT=1.5, ST_SL_BUFFER_ATR=0.1, ST_MIN_RISK_ATR=0.5, ST_MAX_RISK_ATR=2.5,
    ST_RR=2.0, ST_MIN_RR=2.0,
    ST_BREAKEVEN_AT_R=1.0, ST_BE_LOCK_PCT=25, ST_TRAIL_STEP_POINTS=0, ST_TRAIL_START_R=0, ST_TRAIL_R=1.0,
    ST_EXIT_ON_FLIP=False, ST_MAX_SIGNALS_PER_DAY=2, ST_COOLDOWN_BARS=6,
    ST_RSI_FILTER=False, ST_EMA_FILTER=False, ST_VWAP_FILTER=False, ST_CONFIRM="break",
    ST_ENTRY_FROM="", ST_ENTRY_UNTIL="",
    MIN_ATR_PCT=0.05,
)
WINDOW = 300                      # candles every indicator is computed on, in the bot and the backtest alike


def _getter(cfg):
    if isinstance(cfg, dict):
        return lambda k: cfg.get(k, CFG_DEFAULTS.get(k))
    return lambda k: getattr(cfg, k, CFG_DEFAULTS.get(k))


_day = fvg._day


def features(candles, g, tf=5):
    trend_n = int(g("ST_EMA_TREND"))
    if len(candles) < max(trend_n, 2 * int(g("ST_ADX_PERIOD"))) + 10:
        return None
    w = candles[-WINDOW:]
    closes = [c["c"] for c in w]
    st = indicators.supertrend(w, int(g("ST_ATR_PERIOD")), float(g("ST_MULT")))
    dmi = indicators.dmi_series(w, int(g("ST_ADX_PERIOD")))
    atrs = indicators.atr_series(w, int(g("ST_ATR_PERIOD")))
    et = strategy.ema_series(closes, trend_n)
    ep = strategy.ema_series(closes, int(g("ST_EMA_PULLBACK")))
    r = strategy.rsi(closes[-200:], int(g("ST_RSI_PERIOD")))
    if st[-1] is None or dmi[-1] is None or dmi[-1][0] is None or not et or not ep or r is None or not atrs[-1]:
        return None
    k = int(g("ST_ADX_RISING_BARS"))
    prev_adx = dmi[-1 - k][0] if k and len(dmi) > k and dmi[-1 - k] else None
    htf = orderblock.htf_trend(candles, tf, int(g("ST_HTF_MINUTES")), int(g("ST_HTF_EMA")))
    adx, pdi, mdi = dmi[-1]
    day = _day(candles[-1])
    tp = [(c["h"] + c["l"] + c["c"]) / 3 for c in candles if _day(c) == day]
    return dict(vwap=sum(tp) / len(tp), price=closes[-1], st=st, line=st[-1][0], dir=st[-1][1], adx=adx, adx_prev=prev_adx, pdi=pdi,
                mdi=mdi, ema_trend=et[-1], emas_pb=ep, rsi=r, atr=atrs[-1], htf=htf)


def evaluate(candles, f, side, g):
    """Every CE (or PE) rule on the last closed candle: (plan, None) or (None, (stage, reason))."""
    bull = side == "CE"
    d = 1 if bull else -1
    c, prev = candles[-1], candles[-2]
    price, a = f["price"], f["atr"]
    if a < price * g("MIN_ATR_PCT") / 100:
        return None, ("volatility", f"ATR {a:.1f} below {g('MIN_ATR_PCT'):g}% of price")
    n = min(int(g("ST_PULLBACK_BARS")), len(f["emas_pb"]))
    if any(s is None or s[1] != d for s in f["st"][-n:]):
        return None, ("supertrend", f"Supertrend not {'bullish' if bull else 'bearish'} through the pullback")
    if f["adx"] < g("ST_ADX_MIN"):
        return None, ("ADX", f"ADX {f['adx']:.1f} below {g('ST_ADX_MIN'):g}")
    if f["adx_prev"] is not None and f["adx"] <= f["adx_prev"]:
        return None, ("ADX", f"ADX falling ({f['adx_prev']:.1f} → {f['adx']:.1f})")
    di = (f["pdi"] - f["mdi"]) * d
    if di <= g("ST_DI_GAP"):
        return None, ("DI", f"+DI {f['pdi']:.1f} vs -DI {f['mdi']:.1f}")
    if g("ST_EMA_FILTER") and (price - f["ema_trend"]) * d <= 0:
        return None, ("EMA", f"close {'below' if bull else 'above'} EMA{g('ST_EMA_TREND')} {f['ema_trend']:.1f}")
    if g("ST_VWAP_FILTER") and (price - f["vwap"]) * d <= 0:
        return None, ("VWAP", f"close {'below' if bull else 'above'} session VWAP {f['vwap']:.1f}")
    lo, hi = (g("ST_RSI_CE_MIN"), g("ST_RSI_CE_MAX")) if bull else (g("ST_RSI_PE_MIN"), g("ST_RSI_PE_MAX"))
    if g("ST_RSI_FILTER") and not lo <= f["rsi"] <= hi:
        return None, ("RSI", f"RSI {f['rsi']:.0f} outside {lo:g}-{hi:g}")
    tol = g("ST_TOUCH_ATR") * a
    touched = None
    for i, x in enumerate(candles[-n:]):
        j = i - n
        for level in (f["st"][j][0], f["emas_pb"][j]):
            near = (x["l"] - level <= tol and x["c"] > level) if bull else (level - x["h"] <= tol and x["c"] < level)
            if near:                                       # dipped to the level and closed back on the trend side
                touched = level
    if touched is None:
        return None, ("pullback", "no pullback to the Supertrend line or EMA recently")
    rng = c["h"] - c["l"]
    body = (c["c"] - c["o"]) * d
    if rng <= 0 or body <= 0 or body / rng < g("ST_MIN_BODY_PCT"):
        return None, ("confirmation", "no strong candle in the trade direction")
    if g("ST_CONFIRM") == "break" and (c["c"] - (prev["h"] if bull else prev["l"])) * d <= 0:
        return None, ("confirmation", "did not close beyond the previous candle")
    htf_ok = f["htf"] == ("up" if bull else "down")
    if g("ST_REQUIRE_HTF") and not htf_ok:
        return None, ("HTF trend", f"{g('ST_HTF_MINUTES')}-min trend is {f['htf'] or 'unknown'}")
    risk = g("ST_SL_ATR_MULT") * a
    if g("ST_SL_MODE") == "wider":
        risk = max(risk, (price - f["line"]) * d + g("ST_SL_BUFFER_ATR") * a)
    risk = max(risk, g("ST_MIN_RISK_ATR") * a)
    if risk > g("ST_MAX_RISK_ATR") * a:
        return None, ("risk", f"stop {risk / a:.2f} ATR away, max {g('ST_MAX_RISK_ATR'):g}")
    rr = max(g("ST_RR"), g("ST_MIN_RR"))
    return dict(side=side, entry=price, sl=price - d * risk, target=price + d * rr * risk, risk=risk, rr=rr,
                level=touched, htf_ok=htf_ok), None


def confidence(f, side, g, htf_ok):
    """0-100 checklist quality (not a chance of winning)."""
    d = 1 if side == "CE" else -1
    pts = 50.0
    pts += 15 * min(1.0, max(0.0, (f["adx"] - g("ST_ADX_MIN")) / 15))
    pts += 10 * min(1.0, max(0.0, (f["pdi"] - f["mdi"]) * d / 20))
    lo, hi = (g("ST_RSI_CE_MIN"), g("ST_RSI_CE_MAX")) if side == "CE" else (g("ST_RSI_PE_MIN"), g("ST_RSI_PE_MAX"))
    mid, half = (lo + hi) / 2, (hi - lo) / 2 or 1
    pts += 10 * max(0.0, 1 - abs(f["rsi"] - mid) / half)
    pts += 10 * max(0.0, 1 - abs(f["price"] - f["line"]) / (3 * float(g("ST_MULT")) * f["atr"]))
    pts += 5 if htf_ok else 0
    return int(max(0, min(100, round(pts))))


class Engine:
    """Supertrend + ADX for one instrument. update() once per closed candle; never looks past candles[-1]."""

    def __init__(self, instrument, timeframe, cfg):
        self.instrument, self.tf, self.cfg, self.g = instrument, timeframe, cfg, _getter(cfg)
        self.bar, self.day, self.day_signals = 0, None, 0
        self.last_bar = dict(CE=-10 ** 9, PE=-10 ** 9)
        self.f, self.status, self.signals, self.rejected = None, "warming up", [], []

    def _reject(self, c, side, stage, reason):
        self.rejected = (self.rejected + [dict(at=str(c.get("t") or ""), side=side, stage=stage, reason=reason)])[-20:]

    def update(self, candles, allow_entry=True):
        self.bar += 1
        g = self.g
        if len(candles) < 3:
            return None
        c = candles[-1]
        if _day(c) != self.day:
            self.day, self.day_signals = _day(c), 0
        f = self.f = features(candles, g, self.tf)
        if f is None:
            self.status = "warming up"
            return None
        if not allow_entry:
            self.status = "watching (entries paused)"
            return None
        tm = orb_retest._tmins(c)
        close_t = None if tm is None else tm + self.tf
        if close_t is not None and ((g("ST_ENTRY_FROM") and close_t < orb_retest._mins(g("ST_ENTRY_FROM")))
                                    or (g("ST_ENTRY_UNTIL") and close_t > orb_retest._mins(g("ST_ENTRY_UNTIL")))):
            self.status = "outside this strategy's entry window"
            return None
        if self.day_signals >= g("ST_MAX_SIGNALS_PER_DAY"):
            self.status = f"done for today ({self.day_signals} signals)"
            return None
        side = "CE" if f["dir"] == 1 else "PE"
        if self.bar - self.last_bar[side] <= g("ST_COOLDOWN_BARS"):
            self.status = f"{side} cooldown after the last signal"
            return None
        plan, why_not = evaluate(candles, f, side, g)
        if not plan:
            self.status = f"{side} side: waiting — {why_not[1]}"
            if why_not[0] in ("confirmation", "RSI", "risk", "HTF trend", "DI"):
                self._reject(c, side, *why_not)
            return None
        score = confidence(f, side, g, plan["htf_ok"])
        why = (f"Supertrend {'up' if side == 'CE' else 'down'} pullback to {plan['level']:.1f}, ADX {f['adx']:.0f}, "
               f"+DI {f['pdi']:.0f} / -DI {f['mdi']:.0f}, RSI {f['rsi']:.0f}, 1:{plan['rr']:g}, ATR {f['atr']:.1f}")
        ts = c.get("t")
        sig = dict(side=side, entry=plan["entry"], sl=plan["sl"], target=plan["target"], risk=plan["risk"],
                   rr=plan["rr"], why=why, strategy="supertrend", score=score, confidence=score, volume_ok=None,
                   htf_ok=plan["htf_ok"], st_line=round(f["line"], 2), adx=round(f["adx"], 1),
                   ts=ts.isoformat() if hasattr(ts, "isoformat") else None)
        self.day_signals += 1
        self.last_bar[side] = self.bar
        self.status = f"{side} signal"
        self.signals = (self.signals + [dict(sig, at=str(ts or ""))])[-10:]
        return sig

    def replay(self, candles):
        for i in range(1, len(candles) + 1):
            self.update(candles[max(0, i - 400):i], allow_entry=True)
        self.signals, self.rejected = [], []

    def snapshot(self):
        f, last = self.f, (self.signals[-1] if self.signals else None)
        r = lambda x, n=2: None if x is None else round(x, n)
        keep = ("side", "entry", "sl", "target", "rr", "confidence", "htf_ok", "at", "why")
        return dict(line=r(f and f["line"]), direction=(("up" if f["dir"] == 1 else "down") if f else None),
                    price=r(f and f["price"]), adx=r(f and f["adx"], 1), pdi=r(f and f["pdi"], 1),
                    mdi=r(f and f["mdi"], 1), ema_trend=r(f and f["ema_trend"]), rsi=r(f and f["rsi"], 1),
                    atr=r(f and f["atr"]), htf=f and f["htf"], status=self.status, signals_today=self.day_signals,
                    last_signal={k: last.get(k) for k in keep} if last else None, rejected=self.rejected[-8:])


# ------------------------------------------------------------------ exits
def exit_cfg(cfg):
    g = _getter(cfg)
    return dict(RR=max(g("ST_RR"), g("ST_MIN_RR")), BREAKEVEN_AT_R=g("ST_BREAKEVEN_AT_R"),
                TRAIL_START_R=g("ST_TRAIL_START_R"), TRAIL_R=g("ST_TRAIL_R"), TIME_STOP_MIN=0,
                BE_LOCK_PCT=g("ST_BE_LOCK_PCT"), TRAIL_STEP_POINTS=g("ST_TRAIL_STEP_POINTS"))


def manage(pos, hi, lo, cfg, now=None, close=None):
    return strategy.manage(pos, hi, lo, exit_cfg(cfg), now=now, close=close)


def invalidation(pos, candles, cfg):
    """On a closed candle while holding: exit when Supertrend flips against the trade."""
    g = _getter(cfg)
    if not g("ST_EXIT_ON_FLIP") or len(candles) < int(g("ST_ATR_PERIOD")) + 2:
        return None
    st = indicators.supertrend(candles[-WINDOW:], int(g("ST_ATR_PERIOD")), float(g("ST_MULT")))
    if st[-1] is None:
        return None
    if st[-1][1] != (1 if pos["side"] == "CE" else -1):
        return f"Supertrend flipped {'down' if pos['side'] == 'CE' else 'up'} (line {st[-1][0]:.1f})"
    return None
