"""
Trend Continuation (opening range + VWAP) — strategy 8. Shared by bot.py and the backtests; closed 5-min index
candles only.

WHY  In the research data (research/explore.py, 17 Jul – 29 Sep 2026), once price had closed beyond the 30-minute
     opening range AND on the same side of the session VWAP after the first hour, it more often than not kept going
     into the afternoon, while its typical pullback on the way (~1.3–1.5 x 5-min ATR) was wider than the stops the
     other strategies use. This rule trades exactly that, with a stop outside that pullback.

SETUP  opening range = high / low of the candles that open in [TC_OR_START, TC_OR_END) (09:15–09:45).
ENTRY  the FIRST candle of the day that closes between TC_ENTRY_FROM and TC_ENTRY_UNTIL (10:15–12:00) with
         CE: close > opening-range high + TC_MIN_BREAK_ATR x ATR AND close > session VWAP
         PE: close < opening-range low  - TC_MIN_BREAK_ATR x ATR AND close < session VWAP
       (a close only just past the edge doesn't count; the engine keeps waiting for a clear one.)
       One signal per index per day. Session VWAP is the equal-weight average of typical prices: the live bot's
       candles carry no volume, so the backtest uses the same measure and both agree.
EXITS  stop TC_SL_ATR x ATR(14) from entry; target TC_RR x risk (>= 2); at TC_BREAKEVEN_AT_R the stop moves to
       entry + TC_BE_LOCK_PCT% of the risk (a trade that reached +1R can't end a loser); optional
       trailing from TC_TRAIL_START_R; optional TC_EXIT_ON_FAIL (off: in development it cut trades that recovered).
       Anything still open is squared off at the bot's square-off time.
CHOSEN on development sessions only (research/select_tc.py, 24 Jul – 29 Sep 2026): stop 2 ATR, 1:2, cost at 1R,
       no setup-failed exit. v2 (research/improve_tc.py, same development sessions): lock 25% of the risk at 1R
       (58% of trades reached 1R but only 23% reached 2R) and a 0.4 ATR minimum break (closes within ~0.15 ATR of
       the edge won 29%; 0.4–0.6 ATR all did better). Test results: reports/strategy_review/.
"""
import fvg
import orb_retest
import strategy

CFG_DEFAULTS = dict(
    TC_ALLOW_LIVE=False,
    TC_OR_START="09:15", TC_OR_END="09:45", TC_ENTRY_FROM="10:15", TC_ENTRY_UNTIL="12:00",
    TC_SL_ATR=2.0, TC_RR=2.0, TC_MIN_RR=2.0,
    TC_BREAKEVEN_AT_R=1.0, TC_BE_LOCK_PCT=25, TC_TRAIL_STEP_POINTS=0, TC_TRAIL_START_R=0, TC_TRAIL_R=1.0,
    TC_EXIT_ON_FAIL=False, TC_MIN_BREAK_ATR=0.4,
    MIN_ATR_PCT=0.05,
)


def _getter(cfg):
    if isinstance(cfg, dict):
        return lambda k: cfg.get(k, CFG_DEFAULTS.get(k))
    return lambda k: getattr(cfg, k, CFG_DEFAULTS.get(k))


_day = fvg._day
_mins = orb_retest._mins
_tmins = orb_retest._tmins


def session_vwap(candles):
    """Equal-weight session average of typical prices through candles[-1] (see the module note)."""
    day = _day(candles[-1])
    tp = [(c["h"] + c["l"] + c["c"]) / 3 for c in candles if _day(c) == day]
    return sum(tp) / len(tp)


class Engine:
    """One instrument. update() once per closed candle with the closed candles so far; never looks ahead."""

    def __init__(self, instrument, timeframe, cfg):
        self.instrument, self.tf, self.cfg, self.g = instrument, timeframe, cfg, _getter(cfg)
        self.bar, self.day, self.done_today = 0, None, False
        self.state, self.range, self.vwap, self.price = "waiting", None, None, None
        self.signals, self.rejected = [], []

    def _reject(self, c, side, stage, reason):
        self.rejected = (self.rejected + [dict(at=str(c.get("t") or ""), side=side, stage=stage, reason=reason)])[-20:]

    def update(self, candles, allow_entry=True):
        self.bar += 1
        g = self.g
        if len(candles) < 20:
            return None
        c = candles[-1]
        if _day(c) != self.day:
            self.day, self.done_today, self.range, self.state = _day(c), False, None, "building the opening range"
        tm = _tmins(c)
        if tm is None:
            return None
        self.price, self.vwap = c["c"], session_vwap(candles)
        close_t = tm + self.tf
        if close_t < _mins(g("TC_OR_END")):
            return None
        if self.range is None:
            r = orb_retest.opening_range(candles, g("TC_OR_START"), g("TC_OR_END"), self.tf)
            if not r or not r[2]:
                self.state, self.done_today = "skipped: opening range incomplete", True
                self.range = dict(high=None, low=None)
                return None
            self.range = dict(high=r[0], low=r[1])
            self.state = "waiting for the entry window"
        if self.done_today:
            return None
        if close_t < _mins(g("TC_ENTRY_FROM")):
            return None
        if close_t > _mins(g("TC_ENTRY_UNTIL")):
            self.state, self.done_today = "no aligned close in the entry window", True
            return None
        hi, lo, px, vw = self.range["high"], self.range["low"], c["c"], self.vwap
        side = "CE" if px > hi and px > vw else "PE" if px < lo and px < vw else None
        if side is None:
            self.state = "waiting: price not beyond the opening range on the VWAP side"
            return None
        a = strategy.atr(candles[-100:], 14)
        if not a or a < px * g("MIN_ATR_PCT") / 100:
            self._reject(c, side, "volatility", "ATR too low")
            return None
        past = (px - hi) if side == "CE" else (lo - px)
        if past < (g("TC_MIN_BREAK_ATR") or 0) * a:      # a close just past the edge: keep waiting for a clear one
            self.state = f"waiting: {side} close only {past / a:.2f} ATR past the opening range"
            self._reject(c, side, "break", self.state)
            return None
        self.done_today = True                         # the first aligned close is the trade, taken or not
        if not allow_entry:
            self.state = f"{side} setup came while entries were not allowed"
            self._reject(c, side, "entry", self.state)
            return None
        d = 1 if side == "CE" else -1
        risk = g("TC_SL_ATR") * a
        rr = max(g("TC_RR"), g("TC_MIN_RR"))
        ts = c.get("t")
        why = (f"{'above' if d > 0 else 'below'} the opening range {lo:.1f}–{hi:.1f} and VWAP {vw:.1f} after the first "
               f"hour; stop {g('TC_SL_ATR'):g} ATR, 1:{rr:g}, ATR {a:.1f}")
        sig = dict(side=side, entry=px, sl=px - d * risk, target=px + d * rr * risk, risk=risk, rr=rr, why=why,
                   strategy="tc", score=None, confidence=None, volume_ok=None, htf_ok=None,
                   orb_high=hi, orb_low=lo, ts=ts.isoformat() if hasattr(ts, "isoformat") else None)
        self.state = f"{side} signal"
        self.signals = (self.signals + [dict(sig, at=str(ts or ""))])[-10:]
        return sig

    def replay(self, candles):
        for i in range(1, len(candles) + 1):
            self.update(candles[max(0, i - 400):i], allow_entry=True)
        self.signals, self.rejected = [], []

    def snapshot(self):
        r = self.range or {}
        last = self.signals[-1] if self.signals else None
        keep = ("side", "entry", "sl", "target", "rr", "at", "why")
        return dict(state=self.state, or_high=r.get("high"), or_low=r.get("low"), vwap=self.vwap and round(self.vwap, 2),
                    price=self.price, last_signal={k: last.get(k) for k in keep} if last else None,
                    rejected=self.rejected[-8:])


def exit_cfg(cfg):
    g = _getter(cfg)
    return dict(RR=max(g("TC_RR"), g("TC_MIN_RR")), BREAKEVEN_AT_R=g("TC_BREAKEVEN_AT_R"),
                TRAIL_START_R=g("TC_TRAIL_START_R"), TRAIL_R=g("TC_TRAIL_R"), TIME_STOP_MIN=0,
                BE_LOCK_PCT=g("TC_BE_LOCK_PCT"), TRAIL_STEP_POINTS=g("TC_TRAIL_STEP_POINTS"))


def manage(pos, hi, lo, cfg, now=None, close=None):
    return strategy.manage(pos, hi, lo, exit_cfg(cfg), now=now, close=close)


def invalidation(pos, candles, cfg):
    """Exit when the trend thesis breaks: a close back inside the opening range AND through VWAP."""
    g = _getter(cfg)
    if not g("TC_EXIT_ON_FAIL") or not candles or pos.get("orb_high") is None:
        return None
    c = candles[-1]["c"]
    vw = session_vwap(candles)
    if pos["side"] == "CE" and c < pos["orb_high"] and c < vw:
        return f"back inside the opening range and below VWAP {vw:.1f}"
    if pos["side"] == "PE" and c > pos["orb_low"] and c > vw:
        return f"back inside the opening range and above VWAP {vw:.1f}"
    return None
