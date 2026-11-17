"""
VWAP + Volume strategy: trend continuation after a pullback to VWAP or EMA 20, confirmed by volume.
Shared by bot.py (live/paper) and the backtests, so both use identical rules. Directional logic runs on the
INDEX candles only (never on option premiums). Indices have no volume of their own: each candle carries the
volume of a proxy ("v", or a ready-made relative volume "rvol" — see volume_stats).

ENTRY  on a closed candle, CE (PE is the mirror image):
  1 close above session VWAP                         5 bullish confirmation candle: body >= VWAP_MIN_BODY_PCT
  2 VWAP slope up (>= VWAP_MIN_SLOPE_ATR x ATR           of its range, closes above the previous high and above
    over VWAP_SLOPE_BARS candles)                        the level the pullback touched
  3 EMA 20 above EMA 50                              6 volume >= VWAP_VOLUME_MULT x the previous 20 candles
  4 pullback: within the last VWAP_PULLBACK_BARS     7 RSI within [VWAP_RSI_CE_MIN, VWAP_RSI_CE_MAX]
    candles (this one included) a low came within    8 close not more than VWAP_MAX_EXT_ATR x ATR above VWAP
    VWAP_TOUCH_ATR x ATR of VWAP or EMA 20, without  9 completed 15-min candles trend up (VWAP_REQUIRE_HTF)
    closing below VWAP
AVOID  flat VWAP, price crossing VWAP VWAP_MAX_CROSSES+ times in VWAP_CROSS_LOOKBACK candles, dead market
       (MIN_ATR_PCT), too few candles in the session, missing volume (VWAP_REQUIRE_VOLUME).
EXITS  stop beyond the pullback swing + VWAP_SL_BUFFER_ATR; a stop tighter than VWAP_MIN_RISK_ATR is set to it
       BEFORE entry, a wider one than VWAP_MAX_RISK_ATR skips the trade. Target VWAP_RR x risk. Stop to cost at
       VWAP_BREAKEVEN_AT_R, trailing from VWAP_TRAIL_START_R (strategy.manage: a stop is never lowered).
       invalidation(): close back across VWAP, or VWAP slope turned against the trade.

The confidence score ranks how cleanly the checklist was met. It is NOT a probability of winning.
"""
import fvg
import orderblock
import strategy
import volume_stats

CFG_DEFAULTS = dict(
    VWAP_ALLOW_LIVE=False,
    VWAP_EMA_FAST=20, VWAP_EMA_SLOW=50, VWAP_RSI_PERIOD=14, VWAP_ATR_PERIOD=14,
    VWAP_RSI_CE_MIN=50, VWAP_RSI_CE_MAX=70, VWAP_RSI_PE_MIN=30, VWAP_RSI_PE_MAX=50,
    VWAP_VOLUME_MULT=1.5, VWAP_VOLUME_AVG_N=20, VWAP_REQUIRE_VOLUME=True,
    VWAP_VOLUME_BASIS="rolling", VWAP_VOLUME_SLOT_DAYS=5,
    VWAP_SLOPE_BARS=6, VWAP_MIN_SLOPE_ATR=0.1,
    VWAP_CROSS_LOOKBACK=12, VWAP_MAX_CROSSES=3,
    VWAP_PULLBACK_BARS=4, VWAP_TOUCH_ATR=0.25, VWAP_MIN_BODY_PCT=0.5,
    VWAP_MAX_EXT_ATR=1.5, VWAP_MIN_SESSION_BARS=3,
    VWAP_HTF_MINUTES=15, VWAP_HTF_EMA=20, VWAP_REQUIRE_HTF=True,
    VWAP_SL_BUFFER_ATR=0.1, VWAP_MIN_RISK_ATR=0.5, VWAP_MAX_RISK_ATR=2.0,
    VWAP_RR=2.0, VWAP_MIN_RR=2.0,
    VWAP_BREAKEVEN_AT_R=1.0, VWAP_BE_LOCK_PCT=0, VWAP_TRAIL_STEP_POINTS=0,
    VWAP_TRAIL_START_R=1.5, VWAP_TRAIL_R=1.0,
    VWAP_EXIT_ON_INVALIDATION=True, VWAP_INVALIDATION_BUFFER_ATR=0.1,
    VWAP_MAX_SIGNALS_PER_DAY=2, VWAP_COOLDOWN_BARS=6,
    MIN_ATR_PCT=0.05,
)


def _getter(cfg):
    if isinstance(cfg, dict):
        return lambda k: cfg.get(k, CFG_DEFAULTS.get(k))
    return lambda k: getattr(cfg, k, CFG_DEFAULTS.get(k))


_day = fvg._day


# ------------------------------------------------------------------ indicators (series forms of existing ones)
def session_vwap_series(candles):
    """Today's VWAP after each of today's candles, oldest first. Same weighting as orderblock.session_vwap:
    proxy volume "v" when every candle today has it, otherwise an equal-weight average of typical prices.
    Its last value equals orderblock.session_vwap(candles)."""
    day = _day(candles[-1])
    today = [c for c in candles if _day(c) == day]
    weighted = all(c.get("v") for c in today)
    out, pv, vv = [], 0.0, 0.0
    for c in today:
        w = c["v"] if weighted else 1.0
        pv += (c["h"] + c["l"] + c["c"]) / 3 * w
        vv += w
        out.append(pv / vv)
    return out


def vwap_crosses(closes, vwaps):
    """How many times the close switched sides of VWAP."""
    sides = [1 if c > v else -1 if c < v else 0 for c, v in zip(closes, vwaps)]
    sides = [s for s in sides if s]
    return sum(1 for a, b in zip(sides, sides[1:]) if a != b)


def features(candles, g, tf=5):
    """Everything the rules and the dashboard need, from closed candles only. None without enough history."""
    slow = int(g("VWAP_EMA_SLOW"))
    if len(candles) < slow + 5:
        return None
    window = candles[-max(250, slow * 4):]
    closes = [c["c"] for c in window]
    a = strategy.atr(candles[-100:], int(g("VWAP_ATR_PERIOD")))
    r = strategy.rsi(closes[-200:], int(g("VWAP_RSI_PERIOD")))
    ef = strategy.ema_series(closes, int(g("VWAP_EMA_FAST")))
    es = strategy.ema_series(closes, slow)
    if not a or r is None or not ef or not es:
        return None
    vw = session_vwap_series(candles)
    k = int(g("VWAP_SLOPE_BARS"))
    slope = (vw[-1] - vw[-1 - k]) / a if len(vw) > k else None
    look = int(g("VWAP_CROSS_LOOKBACK"))
    today = [c for c in candles if _day(c) == _day(candles[-1])]
    crosses = vwap_crosses([c["c"] for c in today][-look:], vw[-look:])
    rv = volume_stats.relative(candles, len(candles) - 1, g("VWAP_VOLUME_BASIS"), int(g("VWAP_VOLUME_AVG_N")),
                               int(g("VWAP_VOLUME_SLOT_DAYS")))
    htf = orderblock.htf_trend(candles, tf, int(g("VWAP_HTF_MINUTES")), int(g("VWAP_HTF_EMA")))
    price = candles[-1]["c"]
    trend = "up" if ef[-1] > es[-1] and price > vw[-1] else "down" if ef[-1] < es[-1] and price < vw[-1] else "mixed"
    return dict(price=price, vwap=vw[-1], vwaps=vw, slope=slope, ema_fast=ef[-1], ema_slow=es[-1], emas_fast=ef,
                rsi=r, atr=a, rvol=rv, htf=htf, crosses=crosses, session_bars=len(today), trend=trend,
                ext=(price - vw[-1]) / a)


# ------------------------------------------------------------------ the rules
def pullback(candles, f, side, g):
    """The pullback that this candle confirms, or None: (touched level, swing extreme of the pullback window).
    Looks at the last VWAP_PULLBACK_BARS candles of today (this one included) for a low (high) that came within
    VWAP_TOUCH_ATR x ATR of VWAP or EMA 20 at that candle, while no close went beyond VWAP."""
    n = int(g("VWAP_PULLBACK_BARS"))
    vw, ef = f["vwaps"], f["emas_fast"]
    n = min(n, len(vw), len(ef))
    win = candles[-n:]
    tol = g("VWAP_TOUCH_ATR") * f["atr"]
    bull = side == "CE"
    touched = None
    for i, c in enumerate(win):
        j = i - n                                         # same position in the VWAP / EMA series
        v, e = vw[j], ef[j]
        if (c["c"] < v) if bull else (c["c"] > v):
            return None                                   # a close beyond VWAP is a break, not a pullback
        for level in (v, e):
            if (bull and c["l"] <= level + tol and c["h"] >= level - tol) or \
               (not bull and c["h"] >= level - tol and c["l"] <= level + tol):
                touched = level
    if touched is None:
        return None
    extreme = min(c["l"] for c in win) if bull else max(c["h"] for c in win)
    return touched, extreme


def confidence(f, side, g, vol_ok, htf_ok):
    """0-100 checklist quality: 50 for passing every rule, the rest for how clearly each was passed.
    Ranks setups; it is not a chance of winning."""
    d = 1 if side == "CE" else -1
    pts = 50.0
    if f["rvol"] is not None and vol_ok:
        pts += 15 * min(1.0, (f["rvol"] - g("VWAP_VOLUME_MULT")) / g("VWAP_VOLUME_MULT") + 0.3)
    if f["slope"] is not None:
        pts += 10 * min(1.0, abs(f["slope"]) / (3 * g("VWAP_MIN_SLOPE_ATR")))
    lo, hi = (g("VWAP_RSI_CE_MIN"), g("VWAP_RSI_CE_MAX")) if side == "CE" else (g("VWAP_RSI_PE_MIN"), g("VWAP_RSI_PE_MAX"))
    mid, half = (lo + hi) / 2, (hi - lo) / 2 or 1
    pts += 10 * max(0.0, 1 - abs(f["rsi"] - mid) / half)
    pts += 10 * max(0.0, 1 - max(0.0, f["ext"] * d) / g("VWAP_MAX_EXT_ATR"))
    pts += 5 if htf_ok else 0
    pts -= 5 * f["crosses"]
    return int(max(0, min(100, round(pts))))


def evaluate(candles, f, side, g):
    """Run every CE (or PE) rule on the last closed candle. Returns (plan dict, None) or (None, (stage, reason))."""
    bull = side == "CE"
    d = 1 if bull else -1
    c, prev = candles[-1], candles[-2]
    price, a = f["price"], f["atr"]
    if f["session_bars"] < g("VWAP_MIN_SESSION_BARS"):
        return None, ("session", f"only {f['session_bars']} candles today")
    if a < price * g("MIN_ATR_PCT") / 100:
        return None, ("volatility", f"ATR {a:.1f} below {g('MIN_ATR_PCT'):g}% of price")
    if (price - f["vwap"]) * d <= 0:
        return None, ("VWAP side", f"close {price:.1f} {'below' if bull else 'above'} VWAP {f['vwap']:.1f}")
    if f["slope"] is None or f["slope"] * d < g("VWAP_MIN_SLOPE_ATR"):
        return None, ("VWAP slope", "VWAP flat or against the trade" if f["slope"] is None
                      else f"VWAP slope {f['slope']:+.2f} ATR, need {d * g('VWAP_MIN_SLOPE_ATR'):+g}")
    if f["crosses"] >= g("VWAP_MAX_CROSSES"):
        return None, ("chop", f"price crossed VWAP {f['crosses']} times recently")
    if (f["ema_fast"] - f["ema_slow"]) * d <= 0:
        return None, ("EMA", f"EMA{g('VWAP_EMA_FAST')} {f['ema_fast']:.1f} {'below' if bull else 'above'} "
                             f"EMA{g('VWAP_EMA_SLOW')} {f['ema_slow']:.1f}")
    lo, hi = (g("VWAP_RSI_CE_MIN"), g("VWAP_RSI_CE_MAX")) if bull else (g("VWAP_RSI_PE_MIN"), g("VWAP_RSI_PE_MAX"))
    if not lo <= f["rsi"] <= hi:
        return None, ("RSI", f"RSI {f['rsi']:.0f} outside {lo:g}-{hi:g}")
    if f["ext"] * d > g("VWAP_MAX_EXT_ATR"):
        return None, ("extended", f"{f['ext'] * d:.2f} ATR from VWAP, max {g('VWAP_MAX_EXT_ATR'):g}")
    pb = pullback(candles, f, side, g)
    if not pb:
        return None, ("pullback", "no pullback to VWAP / EMA recently")
    level, extreme = pb
    rng = c["h"] - c["l"]
    body = (c["c"] - c["o"]) * d
    if rng <= 0 or body <= 0 or body / rng < g("VWAP_MIN_BODY_PCT"):
        return None, ("confirmation", "no strong candle in the trade direction")
    if (c["c"] - (prev["h"] if bull else prev["l"])) * d <= 0 or (c["c"] - level) * d <= 0:
        return None, ("confirmation", "candle did not close beyond the previous candle and the pullback level")
    vol_ok = f["rvol"] is not None and f["rvol"] >= g("VWAP_VOLUME_MULT")
    if not vol_ok and (g("VWAP_REQUIRE_VOLUME") or f["rvol"] is not None):
        return None, ("volume", "volume unavailable" if f["rvol"] is None
                      else f"volume {f['rvol']:.2f}x, need {g('VWAP_VOLUME_MULT'):g}x")
    htf_ok = f["htf"] == ("up" if bull else "down")
    if g("VWAP_REQUIRE_HTF") and not htf_ok:
        return None, ("HTF trend", f"{g('VWAP_HTF_MINUTES')}-min trend is {f['htf'] or 'unknown'}")
    # stop beyond the pullback swing; widened to the minimum before entry, never after; too wide = no trade
    risk = (price - extreme) * d + g("VWAP_SL_BUFFER_ATR") * a
    risk = max(risk, g("VWAP_MIN_RISK_ATR") * a)
    if risk > g("VWAP_MAX_RISK_ATR") * a:
        return None, ("risk", f"stop {risk / a:.2f} ATR away, max {g('VWAP_MAX_RISK_ATR'):g}")
    rr = max(g("VWAP_RR"), g("VWAP_MIN_RR"))
    return dict(side=side, entry=price, sl=price - d * risk, target=price + d * rr * risk, risk=risk, rr=rr,
                level=level, extreme=extreme, vol_ok=vol_ok, htf_ok=htf_ok), None


class Engine:
    """VWAP strategy for one instrument. Call update() exactly once per closed candle with the closed candles so
    far (oldest first). It never looks past candles[-1], so no future data can leak in."""

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
        if self.day_signals >= g("VWAP_MAX_SIGNALS_PER_DAY"):
            self.status = f"done for today ({self.day_signals} signals)"
            return None
        side = "CE" if f["price"] > f["vwap"] else "PE" if f["price"] < f["vwap"] else None
        if side is None:
            self.status = "price on VWAP"
            return None
        if self.bar - self.last_bar[side] <= g("VWAP_COOLDOWN_BARS"):
            self.status = f"{side} cooldown after the last signal"
            return None
        plan, why_not = evaluate(candles, f, side, g)
        if not plan:
            self.status = f"{side} side: waiting — {why_not[1]}"
            if why_not[0] in ("confirmation", "volume", "HTF trend", "risk", "extended", "RSI"):
                self._reject(c, side, *why_not)          # near-misses only; "wrong side" every candle is noise
            return None
        score = confidence(f, side, g, plan["vol_ok"], plan["htf_ok"])
        rv = f"{f['rvol']:.1f}x" if f["rvol"] is not None else "n/a"
        why = (f"{'bullish' if side == 'CE' else 'bearish'} VWAP pullback to {plan['level']:.1f}, volume {rv}, "
               f"RSI {f['rsi']:.0f}, VWAP slope {f['slope']:+.2f} ATR, {g('VWAP_HTF_MINUTES')}-min {f['htf'] or 'n/a'}, "
               f"1:{plan['rr']:g}, ATR {f['atr']:.1f}")
        ts = c.get("t")
        sig = dict(side=side, entry=plan["entry"], sl=plan["sl"], target=plan["target"], risk=plan["risk"],
                   rr=plan["rr"], why=why, strategy="vwap", score=score, confidence=score,
                   volume_ok=plan["vol_ok"], htf_ok=plan["htf_ok"], rvol=f["rvol"], vwap=round(f["vwap"], 2),
                   ts=ts.isoformat() if hasattr(ts, "isoformat") else None)
        self.day_signals += 1
        self.last_bar[side] = self.bar
        self.status = f"{side} signal"
        self.signals = (self.signals + [dict(sig, at=str(ts or ""))])[-10:]
        return sig

    def replay(self, candles):
        """Rebuild state from history (bot warm-up); signals found there are spent, never traded on restart."""
        for i in range(1, len(candles) + 1):
            self.update(candles[max(0, i - 400):i], allow_entry=True)
        self.signals, self.rejected = [], []

    def snapshot(self):
        """Everything the dashboard card shows."""
        f, last = self.f, (self.signals[-1] if self.signals else None)
        r = lambda x, n=2: None if x is None else round(x, n)
        keep = ("side", "entry", "sl", "target", "rr", "confidence", "volume_ok", "htf_ok", "at", "why")
        return dict(vwap=r(f and f["vwap"]), slope=r(f and f["slope"], 3), price=r(f and f["price"]),
                    ema_fast=r(f and f["ema_fast"]), ema_slow=r(f and f["ema_slow"]), rsi=r(f and f["rsi"], 1),
                    rvol=r(f and f["rvol"]), trend=f and f["trend"], htf=f and f["htf"], atr=r(f and f["atr"]),
                    crosses=f and f["crosses"], status=self.status, signals_today=self.day_signals,
                    last_signal={k: last.get(k) for k in keep} if last else None,
                    rejected=self.rejected[-8:])


# ------------------------------------------------------------------ exits
def exit_cfg(cfg):
    """strategy.manage reads RR / BREAKEVEN_AT_R / TRAIL_*; map the VWAP settings onto them."""
    g = _getter(cfg)
    return dict(RR=max(g("VWAP_RR"), g("VWAP_MIN_RR")), BREAKEVEN_AT_R=g("VWAP_BREAKEVEN_AT_R"),
                TRAIL_START_R=g("VWAP_TRAIL_START_R"), TRAIL_R=g("VWAP_TRAIL_R"), TIME_STOP_MIN=0,
                BE_LOCK_PCT=g("VWAP_BE_LOCK_PCT"), TRAIL_STEP_POINTS=g("VWAP_TRAIL_STEP_POINTS"))


def manage(pos, hi, lo, cfg, now=None, close=None):
    return strategy.manage(pos, hi, lo, exit_cfg(cfg), now=now, close=close)


def invalidation(pos, candles, cfg):
    """On a closed candle while holding: a reason to exit because the VWAP thesis broke, else None."""
    g = _getter(cfg)
    if not g("VWAP_EXIT_ON_INVALIDATION") or len(candles) < 3:
        return None
    a = strategy.atr(candles[-100:], int(g("VWAP_ATR_PERIOD")))
    vw = session_vwap_series(candles)
    if not a or not vw:
        return None
    d = 1 if pos["side"] == "CE" else -1
    c = candles[-1]["c"]
    if (c - vw[-1]) * d < -g("VWAP_INVALIDATION_BUFFER_ATR") * a:
        return f"closed {'below' if d > 0 else 'above'} VWAP {vw[-1]:.1f}"
    k = int(g("VWAP_SLOPE_BARS"))
    if len(vw) > k and (vw[-1] - vw[-1 - k]) / a * d <= -g("VWAP_MIN_SLOPE_ATR"):
        return f"VWAP turned {'down' if d > 0 else 'up'}"
    return None
